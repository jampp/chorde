# -*- coding: utf-8 -*-
from abc import ABCMeta, abstractmethod, abstractproperty
import operator
from chorde.serialize import serialize_read, serialize_write, serialize

# Overridden by inproc_cache based on LRUCache availability
CacheMissError = KeyError

class NONE: pass

class BaseCacheClient(object):
    """
    Interface of all backing stores.
    """
    __metaclass__ = ABCMeta

    @abstractproperty
    def async(self):
        return False

    @abstractproperty
    def capacity(self):
        raise NotImplementedError

    @abstractproperty
    def usage(self):
        raise NotImplementedError

    def wait(self, key, timeout = None):
        """
        Only valid for async clients, if there is a write pending, this
        will wait for it to finish. If there is not, it will return immediately.
        """
        pass
    
    @abstractmethod
    def put(self, key, value, ttl):
        raise NotImplementedError

    def add(self, key, value, ttl):
        """
        Like put, sets a value, but add only does so if there wasn't a valid
        value before, and does so as atomically as possible (ie: it tries to
        be atomic, but doesn't guarantee it - see specific client docs).

        Returns True when it effectively stores the item, False when it
        doesn't (there was one before)
        """
        if not self.contains(key):
            self.put(key, value, ttl)
            return True
        else:
            return False

    @abstractmethod
    def delete(self, key):
        raise NotImplementedError

    def expire(self, key):
        """
        If they given key is in the cache, it will set its TTL to 0, so
        getTtl will subsequently return either a miss, or an expired item.
        """
        return self.delete(key)

    @abstractmethod
    def getTtl(self, key, default = NONE):
        """
        Returns: a tuple (value, ttl). If a default is given and the value
            is not in cache, return (default, -1). If a default is not given
            and the value is not in cache, raises CacheMissError. If the value
            is in the cache, but stale, ttl will be < 0, and value will be
            other than NONE. Note that ttl=0 is a valid and non-stale result.
        """
        if default is NONE:
            raise CacheMissError, key
        else:
            return (default, -1)
    
    def get(self, key, default = NONE, **kw):
        rv, ttl = self.getTtl(key, default, **kw)
        if ttl < 0 and default is NONE:
            raise CacheMissError, key
        else:
            return rv

    @abstractmethod
    def clear(self):
        raise NotImplementedError

    @abstractmethod
    def purge(self, timeout = 0):
        """
        Params
            
            timeout: if specified, only items that have been stale for
                this amount of time will be removed. That is, it is added to the
                initial entry's TTL vale.
        """
        raise NotImplementedError

    @abstractmethod
    def contains(self, key, ttl = None):
        """
        Verifies that a key is valid within the cache

        Params

            key: the key to check

            ttl: If provided and not None, a TTL margin. Keys with this or less
                time to live will be considered as stale. Provide if you want
                to check about-to-expire keys.
        """
        return False

class ReadWriteSyncAdapter(BaseCacheClient):
    def __init__(self, client):
        self.client = client

    @property
    def async(self):
        return self.client.async

    @property
    def capacity(self):
        return self.client.capacity

    @property
    def usage(self):
        return self.client.usage

    @serialize_write
    def put(self, key, value, ttl):
        return self.client.put(key, value, ttl)

    @serialize_write
    def add(self, key, value, ttl):
        return self.client.add(key, value, ttl)

    @serialize_write
    def delete(self, key):
        return self.client.delete(key)

    @serialize_write
    def expire(self, key):
        return self.client.expire(key)

    @serialize_read
    def getTtl(self, key, default = NONE, **kw):
        return self.client.getTtl(key, default, **kw)

    @serialize_write
    def clear(self):
        return self.client.clear()

    @serialize_write
    def purge(self, timeout = 0):
        return self.client.purge(timeout)

    @serialize_read
    def contains(self, key, ttl = None, **kw):
        return self.client.contains(key, ttl, **kw)


class SyncAdapter(BaseCacheClient):
    def __init__(self, client):
        self.client = client

    @property
    def async(self):
        return self.client.async

    @property
    def capacity(self):
        return self.client.capacity

    @property
    def usage(self):
        return self.client.usage

    @serialize
    def put(self, key, value, ttl):
        return self.client.put(key, value, ttl)

    @serialize
    def add(self, key, value, ttl):
        return self.client.add(key, value, ttl)

    @serialize
    def delete(self, key):
        return self.client.delete(key)

    @serialize
    def expire(self, key):
        return self.client.expire(key)

    @serialize
    def getTtl(self, key, default = NONE, **kw):
        return self.client.getTtl(key, default, **kw)

    @serialize
    def clear(self):
        return self.client.clear()

    @serialize
    def purge(self, timeout = 0):
        return self.client.purge(timeout)

    @serialize
    def contains(self, key, ttl = None, **kw):
        return self.client.contains(key, ttl, **kw)


class DecoratedWrapper(BaseCacheClient):
    """
    A namespace wrapper client will decorate keys with a namespace, making it possible
    to share one client among many sub-clients without key collisions.
    """
    def __init__(self, client, key_decorator = None, value_decorator = None, value_undecorator = None):
        self.client = client
        self.key_decorator = key_decorator
        self.value_decorator = value_decorator
        self.value_undecorator = value_undecorator

    @property
    def async(self):
        return self.client.async

    @property
    def capacity(self):
        return self.client.capacity

    @property
    def usage(self):
        return self.client.usage

    def wait(self, key, timeout = None):
        if self.key_decorator:
            key = self.key_decorator(key)
        return self.client.wait(key, timeout)
    
    def put(self, key, value, ttl):
        if self.key_decorator:
            key = self.key_decorator(key)
        if self.value_decorator:
            value = self.value_decorator(value)
        return self.client.put(key, value, ttl)

    def add(self, key, value, ttl):
        if self.key_decorator:
            key = self.key_decorator(key)
        if self.value_decorator:
            value = self.value_decorator(value)
        return self.client.add(key, value, ttl)

    def delete(self, key):
        if self.key_decorator:
            key = self.key_decorator(key)
        return self.client.delete(key)

    def expire(self, key):
        if self.key_decorator:
            key = self.key_decorator(key)
        return self.client.expire(key)

    def getTtl(self, key, default = NONE, **kw):
        key_decorator = self.key_decorator
        if key_decorator is not None:
            key = key_decorator(key)
        rv = self.client.getTtl(key, default, **kw)
        if rv is not default and self.value_undecorator is not None:
            rv = self.value_undecorator(rv)
        return rv

    def clear(self):
        return self.client.clear()

    def purge(self, timeout = 0):
        return self.client.purge(timeout)

    def contains(self, key, ttl = None, **kw):
        key_decorator = self.key_decorator
        if key_decorator is not None:
            key = key_decorator(key)
        return self.client.contains(key, ttl, **kw)

class NamespaceWrapper(DecoratedWrapper):
    """
    A namespace wrapper client will decorate keys with a namespace, making it possible
    to share one client among many sub-clients without key collisions.
    """
    def __init__(self, namespace, client):
        super(NamespaceWrapper, self).__init__(client)
        self.namespace = namespace
        self.revision = client.get((namespace,'REVMARK'), 0)

    key_decorator = property(
        operator.attrgetter('_key_decorator'),
        lambda self, value : None)

    def _key_decorator(self, key):
        return (self.namespace, self.revision, key)

    def clear(self):
        # Cannot clear a shared client, so, instead, switch revisions
        self.revision += 1
        self.client.put((self.namespace, 'REVMARK'), self.revision, 3600)
        return self.client.clear()

class NamespaceMirrorWrapper(NamespaceWrapper):
    """
    A namespace wrapper that takes its namespace info from the provided reference (mirrors it).
    It also takes the internal revision number, to inherit namespace changes created by cache clears.
    """
    def __init__(self, reference, client):
        super(NamespaceWrapper, self).__init__(client)
        self.reference = reference

    @property
    def revision(self):
        return self.reference.revision

    @revision.setter
    def revision(self, value):
        pass

    @property
    def namespace(self):
        return self.reference.namespace

    @namespace.setter
    def namespace(self):
        pass
