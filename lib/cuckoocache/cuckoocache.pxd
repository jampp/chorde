cdef struct _node

from cpython.object cimport PyObject

cdef class LazyCuckooCache:
    cdef unsigned long long _nextprio

    cdef bint _rnd_data[16]
    cdef unsigned char _rnd_pos

    cdef object eviction_callback
    cdef object _hash2seed
    cdef object hash1
    cdef object hash2

    cdef _node *table

    cdef readonly unsigned int size
    cdef readonly unsigned int initial_size
    cdef unsigned int table_size
    cdef readonly unsigned int nitems

    cdef readonly bint touch_on_read

    cdef unsigned int _hash1(LazyCuckooCache self, x) except? 0xFFFFFFFF
    cdef unsigned int _hash2(LazyCuckooCache self, x) except? 0xFFFFFFFF
    cdef unsigned long long _assign_prio(LazyCuckooCache self) except? 0xFFFFFFFFFFFFFFFFULL

    cdef int _add_node(LazyCuckooCache self, _node *table, unsigned int tsize, _node *node,
            unsigned int h1, unsigned int h2, PyObject *key, PyObject *value,
            unsigned long long prio, unsigned int item_diff) except -1
    cdef int _rehash(LazyCuckooCache self) except -1

    cdef int _rnd(LazyCuckooCache self) except -1

