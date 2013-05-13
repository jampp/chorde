# -*- coding: utf-8 -*-
import random
import threading
import functools
import weakref
import itertools
import json
import zmq

from . import ipsub

try:
    import cStringIO
except ImportError:
    import StringIO as cStringIO

from chorde.clients import CacheMissError

P2P_HWM = 10
INPROC_HWM = 1 # Just for wakeup signals

class OOB_UPDATE:
    pass

DEFAULT_P2P_BINDHOSTS = (
    "ipc:///tmp/coherence-%(identity)s-%(randport)s", 
    "tcp://localhost"
)

class NoopWaiter(object):
    __slots__ = ()
    def __init__(self, manager, txid):
        pass
    def wait(self):
        pass

class SyncWaiter(object):
    __slots__ = ('manager','txid')
    def __init__(self, manager, txid):
        self.manager = manager
        self.txid = txid
        manager.listen_decode(manager.ackprefix, ipsub.EVENT_INCOMING_UPDATE, self)
    def wait(self):
        raise NotImplementedError
    def __call__(self, prefix, event, payload):
        return False
    def __del__(self):
        manager = self.manager
        manager.unlisten(manager.ackprefix, ipsub.EVENT_INCOMING_UPDATE, self)

def _psufix(x):
    return x[:16] + x[-16:]

HASHES = {
    int : lambda x : hash(x) & 0xFFFFFFFF, # hash(x:int) = x, but must be 64/32-bit compatible
    long : lambda x : hash(x) & 0xFFFFFFFF, # must be 64/32-bit compatible
    str : _psufix, # prefix+suffix is ok for most
    unicode : lambda x : _psufix(x.encode("utf8")), 
    list : len,
    set : len,
    dict : len,
    tuple : len,
    None : type,
}

def stable_hash(x):
    HASHES.get(type(x), HASHES.get(None))(x)

def _weak_callback(f):
    @functools.wraps(f)
    def rv(wself, *p, **kw):
        self = wself()
        if self is None:
            return False
        else:
            return f(self, *p, **kw)
    return staticmethod(rv)

def _bound_weak_callback(self, f):
    return functools.partial(f, weakref.ref(self))

def _swallow_connrefused(onerror):
    def decor(f):
        @functools.wraps(f)
        def rv(*p, **kw):
            try:
                return f(*p, **kw)
            except zmq.ZMQError:
                return onerror(*p, **kw)
        return rv
    return decor

def _noop(*p, **kw):
    return

class CoherenceManager(object):
    def __init__(self, namespace, private, shared, ipsub_, 
            p2p_pub_bindhosts = DEFAULT_P2P_BINDHOSTS, 
            encoding = 'pyobj',
            synchronous = False,
            stable_hash = None,
            value_pickler = None):
        """
        Params
            namespace: A namespace that will use to identify events in subscription
                chatter. If granular enough, it will curb chatter considerably.
            
            private: A client to a private cache

            shared: (optional) A client to a shared cache. If not given,
                p2p transfer of serialized values will be required. If not given,
                a value_pickler must be given.

            ipsub_: An Inter-Process Subscription manager, already
                bound to some endpoint.

            p2p_pub_bindhosts: A list of bindhosts for the P2P pair publisher.

            encoding: The encoding to be used on messages. Default is pyobj, which
                is full-featured, but can be slow or unsafe if the default pickler
                is used (see IPSub.register_pyobj). This only applies to keys,
                values will be encoded independently with value_picker.

            synchronous: If True, fire_X events will be allowed to wait. It will
                involve a big chatter overhead, requiring N:1 confirmations to be
                routed around the IPSub, which requires 2 roundtrips but N bandwidth,
                and monitoring and transmission of heartbeats to know about all peers.
                If False, calls to fire_X().wait will be no-ops.

            stable_hash: If provided, it must be a callable that computes stable
                key hashes, used to subscribe to specific key pending notifications.
                If not provided, the default will be used, which can only handle
                basic types. It should be fast.
        """
        assert value_pickler or shared
        
        self.private = private
        self.shared = shared
        self.ipsub = ipsub_
        self.local = threading.local()
        self.namespace = namespace
        self.synchronous = synchronous
        self.stable_hash = stable_hash
        self.encoding = encoding
        self._txid = itertools.cycle(xrange(0x7FFFFFFF))

        # Key -> hash
        self.pending = dict()
        self.group_pending = dict()

        if synchronous:
            self.waiter = SyncWaiter
        else:
            self.waiter = NoopWaiter

        bindargs = dict(
            randport = 50000 + int(random.random() * 20000),
            identity = ipsub_.identity
        )
        self.p2p_pub_bindhosts = [ bindhost % bindargs for bindhost in p2p_pub_bindhosts ]
        self.p2p_pub_binds = [ipsub_.identity] # default contact is ipsub identity

        # Active broadcasts
        self.delprefix = namespace + '|c|del|'
        self.delackprefix = namespace + '|c|delack|'

        # Listener -> Broker requests
        self.pendprefix = namespace + '|c|pend|'
        self.pendqprefix = namespace + '|c|pendq|'
        self.doneprefix = namespace + '|c|done|'

        # Broker -> Listener requests
        self.listpendqprefix = namespace + '|c|listpendq|'

        self.bound_pending = _bound_weak_callback(self, self._on_pending)
        self.bound_done = _bound_weak_callback(self, self._on_done)
        self.bound_pending_query = _bound_weak_callback(self, self._on_pending_query)
        self.bound_deletion = _bound_weak_callback(self, self._on_deletion)
        self.encoded_pending = self.encoded_done = self.encoded_pending_query = None

        ipsub_.listen_decode(self.delprefix, ipsub.EVENT_INCOMING_UPDATE, 
            self.bound_deletion )
        ipsub_.listen('', ipsub.EVENT_ENTER_BROKER, 
            _bound_weak_callback(self, self._on_enter_broker) )
        ipsub_.listen('', ipsub.EVENT_LEAVE_BROKER, 
            _bound_weak_callback(self, self._on_leave_broker) )

    @property
    def txid(self):
        """
        Generate a new transaction id. No two reads will be the same... often.
        """
        # Iterator is atomic, no need for locks
        return self._txid.next()

    @_swallow_connrefused(_noop)
    def fire_deletion(self, key):
        txid = self.txid
        waiter = self.waiter(self, txid) # subscribe before publishing, or we'll miss it
        self.ipsub.publish_encode(self.delprefix, self.encoding, (txid, key))
        return waiter

    @_weak_callback
    def _on_deletion(self, prefix, event, payload):
        txid, key = payload
        try:
            self.private.delete(key)
        except CacheMissError:
            pass
        
        if self.synchronous:
            self.ipsub.publish_encode(self.delackprefix, self.encoding, txid)

        return True

    @_weak_callback
    def _on_enter_broker(self, prefix, event, payload):
        ipsub_ = self.ipsub
        self.encoded_pending = ipsub_.listen_decode(self.pendprefix, ipsub.EVENT_INCOMING_UPDATE, 
            self.bound_pending )
        self.encoded_done = ipsub_.listen_decode(self.doneprefix, ipsub.EVENT_INCOMING_UPDATE, 
            self.bound_done )
        self.encoded_pending_query = ipsub_.listen_decode(self.pendqprefix, ipsub.EVENT_INCOMING_UPDATE, 
            self.bound_pending_query )
        ipsub_.publish_encode(self.listpendqprefix, self.encoding, None)

    @_weak_callback
    def _on_leave_broker(self, prefix, event, payload):
        ipsub_ = self.ipsub
        ipsub_.unlisten(self.pendprefix, ipsub.EVENT_INCOMING_UPDATE, 
            self.encoded_pending )
        ipsub_.unlisten(self.doneprefix, ipsub.EVENT_INCOMING_UPDATE, 
            self.encoded_done )
        ipsub_.unlisten(self.pendqprefix, ipsub.EVENT_INCOMING_UPDATE, 
            self.encoded_pending_query )

    def _query_pending_locally(self, key, expired, timeout = 2000, optimistic_lock = False):
        rv = self.group_pending.get(key)
        if rv is not None:
            return rv[-1]
        else:
            rv = self.pending.get(key)
            if rv is not None:
                return self.p2p_pub_binds
            elif expired():
                if optimistic_lock:
                    txid = self.txid
                    self.group_pending[key] = (txid, self.p2p_pub_binds)
                    self.pending[key] = txid
                return None
            else:
                return OOB_UPDATE
        return rv

    @_swallow_connrefused(_query_pending_locally)
    def query_pending(self, key, expired, timeout = 2000, optimistic_lock = False):
        """
        Queries the cluster about the key's pending status,
        returns contact info of current computation node if any,
        or OOB_UPDATE if an out-of-band update was detected.

        Params
            key: The key to query about
            expired: A callable that reconfirms expired status,
                to detect out-of-band updates.
            timeout: A timeout, if no replies were received by this time, it will
                be assumed no node is computing.
            optimistic_lock: Request the broker to mark the key as pending if
                there is noone currently computing, atomically.
        """
        if self.ipsub.is_broker:
            # Easy peachy
            return self._query_pending_locally(key, expired, timeout, optimistic_lock)

        # Listere... a tad more complex
        ipsub_ = self.ipsub
        txid = self.txid if optimistic_lock else None
        req = ipsub_.encode_payload(self.encoding, (
            key, 
            txid,
            self.p2p_pub_binds, 
            optimistic_lock))
        
        ctx = zmq.Context.instance()
        waiter = ctx.socket(zmq.PAIR)
        waiter_id = "inproc://qpw%x" % id(waiter)
        waiter.bind(waiter_id)
        def signaler(prefix, event, message, req = map(buffer,req)):
            if map(buffer,message[0][2:]) == req:
                # This is our message
                signaler = ctx.socket(zmq.PAIR)
                signaler.connect(waiter_id)
                signaler.send(message[1][-1], copy = False)
                signaler.close()
                return False
            else:
                return True
        ipsub_.listen('', ipsub.EVENT_UPDATE_ACKNOWLEDGED, signaler)
        ipsub_.publish(self.pendqprefix, req)
        for i in xrange(3):
            if waiter.poll(timeout/4):
                break
            elif expired():
                ipsub_.publish(self.pendqprefix, req)
            else:
                break
        else:
            if expired():
                waiter.poll(timeout/4)
        if waiter.poll(1):
            rv = json.load(cStringIO.StringIO(buffer(waiter.recv(copy=False))))
            if rv is not None:
                rv = rv[-1]
            elif not expired():
                rv = OOB_UPDATE
        elif expired():
            rv = None
        else:
            rv = OOB_UPDATE
        ipsub_.unlisten('', ipsub.EVENT_UPDATE_ACKNOWLEDGED, signaler)
        waiter.close()
        if optimistic_lock and rv is None:
            # We acquired it
            self.pending[key] = txid
        return rv

    def _publish_pending(self, keys):
        # Publish pending notification for anyone listening
        payload = (
            # pending data
            self.txid, 
            keys, 
            # contact info data
            self.p2p_pub_binds,
        )
        self.ipsub.publish_encode(self.pendprefix, self.encoding, payload)

    @_weak_callback
    def _on_pending_query(self, prefix, event, payload):
        key, txid, contact, lock = payload
        rv = self.group_pending.get(key)
        if lock and rv is None:
            self.group_pending[key] = (txid, contact)
        return ipsub.BrokerReply(json.dumps(rv))

    @_weak_callback
    def _on_pending(self, prefix, event, payload):
        if self.ipsub.is_broker:
            txid, keys, contact = payload
            self.group_pending.update(itertools.izip(
                keys, itertools.repeat((txid,contact),len(keys))))

    @_weak_callback
    def _on_done(self, prefix, event, payload):
        if self.ipsub.is_broker:
            txid, keys, contact = self.ipsub.decode_payload(payload)
            group_pending = self.group_pending
            ctxid = (txid, contact)
            for key in keys:
                if group_pending.get(key) == ctxid:
                    try:
                        del group_pending[key]
                    except KeyError:
                        pass

    @_swallow_connrefused(_noop)
    def mark_done(self, key):
        txid = self.pending.pop(key)
        if txid is not None:
            self.ipsub.publish_encode(self.doneprefix, self.encoding, 
                (txid, [key], self.p2p_pub_binds))
