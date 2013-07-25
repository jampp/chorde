# -*- coding: utf-8 -*-
import time

from .async import Defer
from .base import BaseCacheClient, NONE

from chorde.mq import coherence

class CoherentDefer(Defer):
    """
    Wrap a callable in this, and pass it as a value to an AsyncWriteCacheClient,
    and the evaluation of the given callable will happen asynchronously. The cache
    will return stale entries until the callable is finished computing a new value.
    Computation will be aborted if the coherence protocol shows someone else already
    computing, or if the shared cache is re-validated somehow when this method is
    called.
    """
    
    def __init__(self, callable_, *args, **kwargs):
        """
        Params
            callable_, args, kwargs: see Defer. All others must be passed by keyword.

            manager: A CoherenceManager instance that coordinates the corresponding cache clients.

            expired: A callable of the sort CoherenceManager takes, to re-check expiration status
                against the shared cache.

            key: The key associated with this computation, to be provided to the CoherenceManager.

            timeout: Coherence protocol timeout, in ms, if the peers don't answer in this time, 
                detached operation will be assumed and computation will proceed. 
                Default: whatever the heartbeat timeout is on the underlying IPSub channel

            wait_time: Whether and how long to wait for other node's computation to be done.
                Normally, the default value of 0, which means "no wait", is preferrable so as not
                to stall deferred computation workers. However, in quick computations, it may be
                beneficial to provide a small wait time, to decrease latency in case some node
                goes down. This deferred would then take it upon him to start computing, and the
                whole group could be spared a whole cycle (versus just waiting for the value to
                be needed again), trading thoughput for latency. A value of None will cause
                infinite waits.
        """
        self.manager = kwargs.pop('manager')
        self.expired = kwargs.pop('expired')
        self.key = kwargs.pop('key')
        self.timeout = kwargs.pop('timeout', None)
        if self.timeout is None:
            self.timeout = self.manager.ipsub.heartbeat_push_timeout
        self.wait_time = kwargs.pop('wait_time', 0)
        self.computed = False
        super(CoherentDefer, self).__init__(callable_, *args, **kwargs)

    def undefer(self):
        while True:
            if not self.expired():
                return NONE
            else:
                computer = self.manager.query_pending(self.key, self.expired, self.timeout, True)
                if computer is None:
                    # My turn
                    self.computed = True
                    return self.callable_(*self.args, **self.kwargs)
                elif computer is coherence.OOB_UPDATE and not self.expired():
                    # Skip, tiered caches will read it from the shared cache and push it downstream
                    return NONE
                elif self.wait_time != 0:
                    if self.manager.wait_done(self.key, timeout = self.wait_time):
                        return NONE
                    else:
                        # retry
                        continue
                else:
                    return NONE

    def done(self):
        if self.computed:
            self.manager.mark_done(self.key)

class CoherentWrapperClient(BaseCacheClient):
    """
    Client wrapper that publishes cache activity with the coherence protocol.
    Given a manager, it will invoke its methods to make sure other instances
    on that manager are notified of invalidations.

    It adds another method of putting, put_coherently, that will additionally
    ensure that only one node is working on computing the result (thust taking
    a callable rather than a value, as defers do).

    The regular put method will publish being done, if the manager is configured
    in quick refresh mode, but will not attempt to obtain a computation lock, 
    resulting in less overhead, decent consistency, but some duplication of 
    effort. Therefore, put_coherently should be applied to expensive computations.

    If the underlying client isn't asynchronous, put_coherently will implicitly 
    undefer the values, executing the coherence protocol in the calling thread.
    """
    
    def __init__(self, client, manager, timeout = 2000):
        self.client = client
        self.manager = manager
        self.timeout = timeout
        
    @property
    def async(self):
        return self.client.async

    def wait(self, key, timeout = None):
        # Hey, look at that. Since locally it it all happens on a Defer, 
        # we can just wait on the wrapped client first to wait for ourselves
        if timeout is not None:
            deadline = time.time() + timeout
        else:
            deadline = None
        self.client.wait(key, timeout)

        # But in the end we'll have to ask the manager to talk to the other nodes
        if deadline is not None:
            timeout = int(max(0, deadline - time().time()) * 1000)
        else:
            timeout = None
        self.manager.wait_done(key, timeout=timeout)
    
    def put(self, key, value, ttl):
        manager = self.manager
        if manager.quick_refresh:
            # In quick refresh mode, we publish all puts
            if self.async and isinstance(value, Defer) and not isinstance(value, CoherentDefer):
                callable_ = value.callable_
                def done_after(*p, **kw):
                    rv = callable_(*p, **kw)
                    if rv is not NONE:
                        manager.fire_done([key])
                value.callable_ = done_after
                self.client.put(key, value, ttl)
            else:
                self.client.put(key, value, ttl)
                manager.fire_done([key])
        else:
            self.client.put(key, value, ttl)

    def put_coherently(self, key, ttl, expired, callable_, *args, **kwargs):
        """
        Another method of putting, that will additionally ensure that only one node 
        is working on computing the result. As such, it takes  a callable rather 
        than a value, as defers do.
    
        If the underlying client isn't asynchronous, put_coherently will implicitly 
        undefer the value, executing the coherence protocol in the calling thread.

        Params
            key, ttl: See put
            expired: A callable that will re-check expiration status of the key on
                a shared cache. If this function returns False at mid-execution of
                the coherence protocol, the protocol and the computation will be
                aborted (assuming the underlying client will now instead fetch
                values from the shared cache).
            callable_, args, kwargs: See Defer. In contrast to normal puts, the
                callable may not be invoked if some other node has the computation
                lock.
        """
        wait_time = kwargs.pop('wait_time', 0)
        value = CoherentDefer(
            callable_, 
            key = key,
            manager = self.manager,
            expired = expired,
            timeout = self.timeout,
            wait_time = wait_time,
            *args, **kwargs )
        deferred = None
        try:
            if not self.async:
                # cannot put Defer s, so undefer right now
                deferred = value
                value = value.undefer()
                if value is NONE:
                    # Abort
                    return
            self.client.put(key, value, ttl)
        finally:
            if deferred is not None:
                deferred.done()
    
    def delete(self, key):
        self.client.delete(key)
        
        # Warn others
        self.manager.fire_deletion(key)

    def clear(self):
        self.client.clear()

        # Warn others
        self.manager.fire_deletion(coherence.CLEAR)

    def purge(self):
        self.client.purge()
    
    def getTtl(self, key, default = NONE):
        return self.client.getTtl(key, default)
    
    def contains(self, key, ttl = None):
        return self.client.contains(key, ttl)