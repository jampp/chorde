# This monster makes it all compatible with up to py2.0 :-o
try:
    from hashlib import sha256 as checksum_algo
except ImportError:
    try:
        from hashlib import sha1 as checksum_algo
    except ImportError:
        try:
            from hashlib import md5 as checksum_algo
        except ImportError:
            try:
                from sha import sha as checksum_algo
            except ImportError:
                from md5 import md5 as checksum_algo
checksum_algo_name = checksum_algo.__name__.replace('openssl_','')

import hmac
import struct
import threading

try:
    import cPickle
except ImportError:
    import pickle as cPickle

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

class SecurePickler(object):
    def __init__(self, checksum_key, file, *p, **kw):
        self.file = file
        self.buf = StringIO()
        self.checksum_key = checksum_key
        
        self.backing_class = kw.pop('backing_class', cPickle.Pickler)
        self.backing_args = (p, kw)
        self.local = threading.local()

    @property
    def pickler(self):
        try:
            return self.local.pickler
        except AttributeError:
            p, kw = self.backing_args
            self.local.pickler = pickler = self.backing_class(self.buf, *p, **kw)
            return pickler
        
    @property
    def persistent_id(self):
        return self.pickler.persistent_id
    
    @persistent_id.setter
    def persistent_id(self, value):
        self.pickler.persistent_id = value

    def dump(self,val):
        # dump to underlying pickler, then pick up the results
        self.pickler.dump(val)
        rv = self.buf.getvalue()
        self.buf.reset()
        self.buf.truncate()
        
        # compute HMAC, and prepend to output
        md = hmac.HMAC(self.checksum_key, rv, checksum_algo).hexdigest()
        self.file.write(struct.pack('<L',len(rv)).encode("hex"))
        self.file.write(md)
        self.file.write(rv)

class SecureUnpickler(object):
    def __init__(self, checksum_key, file, *p, **kw):
        self.file = file
        self.buf = StringIO()
        self.checksum_key = checksum_key
        
        self.backing_class = kw.pop('backing_class', cPickle.Unpickler)
        self.backing_args = (p, kw)
        self.local = threading.local()

    @property
    def pickler(self):
        try:
            return self.local.unpickler
        except AttributeError:
            p, kw = self.backing_args
            self.local.unpickler = unpickler = self.backing_class(self.buf, *p, **kw)
            return unpickler

    @property
    def persistent_load(self):
        return self.unpickler.persistent_load
    
    @persistent_load.setter
    def persistent_load(self, value):
        self.unpickler.persistent_load = value

    def load(self, headlen = len(struct.pack('<L',0).encode("hex"))):
        datalen = self.file.read(headlen)
        if not datalen:
            raise EOFError, "Cannot read secure packet header"
        datalen, = struct.unpack('<L', datalen.decode("hex") )
        
        ref_md = hmac.HMAC(self.checksum_key, None, checksum_algo)
        md = self.file.read(ref_md.digest_size*2)
        
        data = self.file.read(datalen)
        ref_md.update(data)
        
        ref_md = ref_md.hexdigest()
        if ref_md != md:
            raise ValueError, "MAC mismatch unpickling"
        
        buf = self.buf
        buf.reset()
        buf.write(data)
        buf.truncate()
        buf.reset()
        rv = self.unpickler.load()
        buf.reset()
        buf.truncate()
        return rv


