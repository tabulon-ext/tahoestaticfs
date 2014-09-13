"""
Cache metadata and data of a directory tree.
"""

import os
import sys
import time
import json
import struct
import errno

from Crypto.Hash import HMAC, SHA256, SHA512
from Crypto import Random
import pbkdf2

from tahoefuse.tahoeio import HTTPError, TahoeConnection
from tahoefuse.crypto import CryptFile, HKDF_SHA256_extract, HKDF_SHA256_expand
from tahoefuse.blockcache import BlockCachedFile


class CacheDB(object):
    def __init__(self, path, rootcap, node_url):
        if not os.path.isdir(path):
            raise IOError(errno.ENOENT, "Cache directory is not an existing directory")

        assert isinstance(rootcap, unicode)

        self.path = path
        self.prk = self._generate_prk(rootcap)

        # List of alive files
        self.alive_files = []

        # Load alive files
        self._load_alive_files()

        # Remove dead files
        self._cleanup()

    def _generate_prk(self, rootcap):
        # Cache master key is derived from hashed rootcap and salt via
        # PBKDF2, with a fixed number of iterations.
        #
        # The master key, combined with a second different salt, are
        # used to generate per-file keys via HKDF-SHA256

        # Get salt
        salt_fn = os.path.join(self.path, 'salt')
        try:
            with open(salt_fn, 'rb') as f:
                salt = f.read(32)
                salt_hkdf = f.read(32)
                if len(salt) != 32 or len(salt_hkdf) != 32:
                    raise ValueError()
        except (IOError, OSError, ValueError):
            # Start with new salt
            salt = Random.new().read(32)
            salt_hkdf = Random.new().read(32)
            with open(salt_fn, 'wb') as f:
                f.write(salt)
                f.write(salt_hkdf)

        # Derive key
        d = pbkdf2.PBKDF2(passphrase=rootcap.encode('ascii'),
                          salt=salt,
                          iterations=100000,
                          digestmodule=SHA256)
        key = d.read(32)

        # HKDF private key material for per-file keys
        return HKDF_SHA256_extract(salt=salt_hkdf, key=key)

    def _load_alive_files(self):
        """
        Walk through the cached directory tree, and record in
        self.alive_files which cache files are reachable from the
        root.
        """
        self.alive_files = []

        stack = []

        # Start from root
        fn, key = self.get_filename_and_key(u"")
        if os.path.isfile(fn):
            stack.append((u"", fn, key))

        # Walk the tree
        while stack:
            upath, fn, key = stack.pop()

            if not os.path.isfile(fn):
                continue

            try:
                with CryptFile(fn, key=key, mode='rb') as f:
                    data = json.load(f)
                    if data[0] != u'dirnode':
                        raise ValueError()
                    children = data[1].get(u'children', {}).items()
            except (IOError, OSError, ValueError):
                continue

            self.alive_files.append((os.path.basename(fn), upath))

            for c_fn, c_info in children:
                c_upath = os.path.join(upath, c_fn)
                if c_info[0] == u'dirnode':
                    c_fn, c_key = self.get_filename_and_key(c_upath)
                    if os.path.isfile(c_fn):
                        stack.append((c_upath, c_fn, c_key))
                elif c_info[0] == u'filenode':
                    for ext in (None, b'state', b'data'):
                        c_fn, c_key = self.get_filename_and_key(c_upath, ext=ext)
                        self.alive_files.append((os.path.basename(c_fn), c_upath))

    def _cleanup(self):
        alive_file_set = set(x[0] for x in self.alive_files)
        for basename in os.listdir(self.path):
            if basename == 'salt':
                continue
            if basename not in alive_file_set:
                fn = os.path.join(self.path, basename)
                os.unlink(fn)

    def get_upath_parent(self, path):
        return self.get_upath(os.path.dirname(os.path.normpath(path)))

    def get_upath(self, path):
        try:
            path = os.path.normpath(path)
            return path.decode(sys.getfilesystemencoding()).lstrip(u'/')
        except UnicodeError:
            raise IOError(errno.ENOENT, "file does not exist")

    def get_filename_and_key(self, upath, ext=None):
        path = upath.encode('utf-8')
        nonpath = b"//\x00" # cannot occur in path, which is normalized

        # Generate per-file key material via HKDF
        info = path
        if ext is not None:
            info += nonpath + ext
        data = HKDF_SHA256_expand(self.prk, info, 3*32)

        # Generate key
        key = data[:32]

        # Generate filename
        fn = HMAC.new(data[32:], msg=info, digestmod=SHA512).hexdigest()
        return os.path.join(self.path, fn), key


class CachedFile(object):
    direct_io = False
    keep_cache = False

    def __init__(self, cachedb, upath, io):
        # Use per-file keys for different files, for safer fallback
        # in the extremely unlikely event of SHA512 hash collisions
        filename, key = cachedb.get_filename_and_key(upath)
        filename_state, key_state = cachedb.get_filename_and_key(upath, b'state')
        filename_data, key_data = cachedb.get_filename_and_key(upath, b'data')

        self.dirty = False
        self.f = None
        self.f_state = None
        self.f_data = None

        try:
            self.f = CryptFile(filename, key=key, mode='rb')
            self.info = json.load(self.f)

            self.f_state = CryptFile(filename_state, key=key_state, mode='r+b')
            self.f_data = CryptFile(filename_data, key=key_data, mode='r+b')
            self.block_cache = BlockCachedFile.restore_state(self.f_data, self.f_state)
        except (IOError, OSError, ValueError):
            self.dirty = True
            if self.f is not None:
                self.f.close()
                self.f = None
            if self.f_state is not None:
                self.f_state.close()
            if self.f_data is not None:
                self.f_data.close()

        if self.dirty:
            self.f = CryptFile(filename, key=key, mode='w+b')

            try:
                self.info = io.get_info(upath)
            except (HTTPError, ValueError):
                os.unlink(filename)
                self.f.close()
                raise IOError(errno.EFAULT, "failed to retrieve information")

            json.dump(self.info, self.f)

            # Create a data file filled with random data
            self.f_data = CryptFile(filename_data, key=key_data, mode='w+b')
            self.f_data.write(RandomString(self.info[1][u'size']))

            # Block cache on top of data file
            self.block_cache = BlockCachedFile(self.f_data, self.info[1][u'size'])

            # Block data state file
            self.f_state = CryptFile(filename_state, key=key_state, mode='w+b')

    def close(self):
        self.f_state.seek(0)
        self.f_state.truncate(0)
        self.block_cache.save_state(self.f_state)
        self.f_state.close()
        self.block_cache.close()
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def _do_rw(self, io, offset, length_or_data, write=False):
        if write:
            data = length_or_data
            length = len(data)
        else:
            length = length_or_data

        stream_f = None
        stream_offset = 0
        stream_data = []

        try:
            while True:
                if write:
                    pos = self.block_cache.pre_write(offset, length)
                else:
                    pos = self.block_cache.pre_read(offset, length)

                if pos is None:
                    # cache ready
                    if write:
                        return self.block_cache.write(offset, data)
                    else:
                        return self.block_cache.read(offset, length)
                else:
                    # cache not ready -- fill it up
                    c_offset, c_length = pos

                    if stream_f is not None and (stream_offset < c_offset or c_offset > stream_offset + 10000):
                        stream_f.close()
                        stream_f = None

                    if stream_f is None:
                        stream_f = io.get_content(self.info[1][u'ro_uri'], c_offset, c_length, iscap=True)
                        stream_offset = c_offset
                        stream_data = []

                    read_offset = stream_offset
                    read_bytes = 0
                    while read_offset + read_bytes < c_length + c_offset:
                        block = stream_f.read(131072)
                        if not block:
                            stream_f.close()
                            stream_f = None
                            break

                        stream_data.append(block)
                        read_bytes += len(block)
                        stream_offset, stream_data = self.block_cache.receive_cached_data(stream_offset, stream_data)

        except HTTPError, e: 
            raise IOError(errno.EFAULT, str(e))
        finally:
            if stream_f is not None:
                stream_f.close()

    def read(self, io, offset, length):
        return self._do_rw(io, offset, length, write=False)


class CachedDir(object):
    def __init__(self, cachedb, upath, io):
        filename, key = cachedb.get_filename_and_key(upath)
        try:
            with CryptFile(filename, key=key, mode='rb') as f:
                self.info = json.load(f)
            return
        except (IOError, OSError, ValueError):
            pass

        f = CryptFile(filename, key=key, mode='w+b')
        try:
            self.info = io.get_info(upath)
            json.dump(self.info, f)
        except (HTTPError, ValueError):
            os.unlink(filename)
            raise IOError(errno.EFAULT, "failed to retrieve information")
        finally:
            f.close()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def listdir(self):
        return list(self.info[1][u'children'].keys())

    def get_attr(self):
        return dict(type='dir')

    def get_child_attr(self, childname):
        assert isinstance(childname, unicode)
        children = self.info[1][u'children']
        if childname not in children:
            raise IOError(errno.ENOENT, "no such entry")

        info = children[childname]
        if info[0] == u'dirnode':
            return dict(type='dir', 
                        ctime=info[1][u'metadata'][u'tahoe'][u'linkcrtime'],
                        mtime=info[1][u'metadata'][u'tahoe'][u'linkcrtime'])
        elif info[0] == u'filenode':
            return dict(type='file', 
                        size=info[1]['size'],
                        ctime=info[1][u'metadata'][u'tahoe'][u'linkcrtime'],
                        mtime=info[1][u'metadata'][u'tahoe'][u'linkcrtime'])
        else:
            raise IOError(errno.EBADF, "invalid entry")


class RandomString(object):
    def __init__(self, size):
        self._random = Random.new()
        self.size = size

    def __len__(self):
        return self.size

    def __getitem__(self, k):
        if isinstance(k, slice):
            return self._random.read(len(xrange(*k.indices(self.size))))
        else:
            raise IndexError("invalid index")
