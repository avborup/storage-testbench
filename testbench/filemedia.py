"""FileMedia: back the Media interface with a real file so the FILE backend
handles multi-GB objects without materialising a buffer. pread reads, O_APPEND
staging writes with incremental rolling crc32c/md5, and finalize/link_into via a
contained os.replace/os.link. The bytes-compat shims (to_bytes/__add__/__radd__/
__eq__/__getitem__) are kept working for the legacy call sites but the streaming
paths never route through them. FILE-backend-only: BytesMedia stays the memory
backend so the memory golden never moves."""

import hashlib
import io
import os

import crc32c

from testbench import containment
from testbench.media import Media

READ_CHUNK = 1024 * 1024  # streamed checksum/parity pass size (not golden-pinned)


class _PreadReader(io.RawIOBase):
    """A read-only, offset-independent raw stream over a FileMedia's backing fd.
    Each instance owns its position and reads via os.pread, so a fresh reader()
    never shares an offset with the backing fd or with sibling readers."""

    def __init__(self, read_fd, size):
        self._read_fd = read_fd
        self._size = size
        self._pos = 0

    def readable(self):
        return True

    def readinto(self, b):
        if self._pos >= self._size:
            return 0
        want = min(len(b), self._size - self._pos)
        data = os.pread(self._read_fd, want, self._pos)
        n = len(data)
        b[:n] = data
        self._pos += n
        return n


class FileMedia(Media):
    def __init__(self, read_fd, size, crc, md5_digest):
        # read_fd: an O_RDONLY|O_NOFOLLOW fd to the backing inode (survives an
        # os.replace/os.link of its name). size/crc/md5_digest are rolling state.
        self._read_fd = read_fd
        self._size = size
        self._crc = crc  # int, crc32c seed-chain accumulator
        self._md5 = md5_digest  # bytes (frozen digest) or a live hashlib.md5
        self._staging = None  # (staging_dir_fd_dup, name, append_fd) or None
        self._closed = False

    # --- lifecycle -------------------------------------------------------
    @property
    def is_finalized(self):
        # True once the staging file has been promoted (finalize) or sealed
        # (appendable). Read-only / hydrated instances are always "finalized".
        return self._staging is None

    def close(self):
        # Idempotent. Releases the read fd and any staging fds (append_fd + the
        # dup'd staging dir_fd). reader()'s dup'd fd is owned by its BufferedReader
        # (closefd=True) and is not touched here.
        if self._closed:
            return
        self._closed = True
        if self._staging is not None:
            sdir, _, afd = self._staging
            for fd in (afd, sdir):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._staging = None
        try:
            os.close(self._read_fd)
        except OSError:
            pass
        self._read_fd = -1

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # --- measurement -----------------------------------------------------
    def __len__(self):
        return self._size

    # --- reads (pread; zero-length special-cased) ------------------------
    def _pread(self, begin, end):
        # `self._size == 0` is defense-in-depth (an intentional equivalent mutant):
        # it is subsumed by `end <= begin` on every current call path (chunks/
        # reader special-case zero-length upstream, and a 0-length slice has
        # end == begin), so no test can kill it in isolation. It is kept as a
        # guard against a future caller passing end > 0 against an empty file.
        if end <= begin or self._size == 0:
            return b""
        length = min(end, self._size) - begin
        return os.pread(self._read_fd, length, begin)

    def __getitem__(self, key):
        if isinstance(key, slice):
            begin, end, step = key.indices(self._size)
            assert step == 1, "FileMedia slicing is contiguous only"
            return self._pread(begin, end)
        idx = key if key >= 0 else self._size + key
        return self._pread(idx, idx + 1)[0]

    def chunks(self, begin, end, size):
        pos = begin
        while pos < end:  # empty slice -> zero iterations
            stop = min(pos + size, end)
            yield self._pread(pos, stop)
            pos = stop

    def reader(self):
        if self._size == 0:
            return io.BytesIO(b"")
        # pread-backed so each reader() carries its OWN offset: os.dup shares the
        # open file description (and thus the file offset) with self._read_fd, so
        # two dup-based readers would step on one another's position. pread never
        # touches the shared offset, keeping reader() genuinely fresh per call.
        return io.BufferedReader(_PreadReader(self._read_fd, self._size))

    # --- checksums (rolling) --------------------------------------------
    def crc32c(self):
        return self._crc

    def md5(self):
        return self._md5 if isinstance(self._md5, bytes) else self._md5.digest()

    # --- materialising compat shims (small/legacy callers only) ----------
    def to_bytes(self):
        return self._pread(0, self._size)

    def __add__(self, data):
        return self.to_bytes() + data

    def __radd__(self, data):
        return data + self.to_bytes()

    def __eq__(self, other):
        if isinstance(other, Media):
            return self.to_bytes() == other.to_bytes()
        if isinstance(other, (bytes, bytearray)):
            return self.to_bytes() == other
        return NotImplemented

    __hash__ = None

    # --- constructors ----------------------------------------------------
    @classmethod
    def _from_open_fd(cls, read_fd):
        size = os.fstat(read_fd).st_size
        crc, md5 = 0, hashlib.md5()
        off = 0
        while off < size:  # single streamed pass -- bounded, linear
            buf = os.pread(read_fd, min(READ_CHUNK, size - off), off)
            crc = crc32c.crc32c(buf, crc)
            md5.update(buf)
            off += len(buf)
        return cls(read_fd, size, crc, md5.digest())

    @classmethod
    def new_staging(cls, dir_fd, name):
        append_fd = containment.open_staging(dir_fd, name)
        read_fd = containment.safe_open(dir_fd, name, os.O_RDONLY)
        self = cls(read_fd, 0, 0, hashlib.md5())
        # dup the dir_fd so the staging tuple owns an fd that outlives the
        # caller's `with ... dfd` context. Released by finalize/seal/close.
        self._staging = (os.dup(dir_fd), name, append_fd)
        return self

    # --- staging writes (O_APPEND; rolling crc32c/md5) -------------------
    def append(self, data):
        if self._staging is None:
            raise RuntimeError("append on a non-staging FileMedia")
        _, _, append_fd = self._staging
        os.write(append_fd, data)  # O_APPEND -> always at EOF
        self._size += len(data)
        self._crc = crc32c.crc32c(data, self._crc)
        self._md5.update(data)

    def __iadd__(self, data):
        self.append(data)
        return self

    def finalize(self, dest):
        # One-shot promote: (dst_dir_fd, dst_name). Contained os.replace, then
        # release the staging fds. The inode survives, so _read_fd stays valid.
        if self._staging is None:
            return None  # already finalized / read-only
        sdir, sname, append_fd = self._staging
        dst_dir_fd, dst_name = dest
        containment.maybe_fsync(append_fd)  # data durable before the rename
        os.close(append_fd)
        containment.promote(sdir, sname, dst_dir_fd, dst_name)
        containment.maybe_fsync(dst_dir_fd)  # rename durable in the dest dir
        os.close(sdir)
        self._md5 = self._md5.digest()
        self._staging = None
        return None

    def link_into(self, dest):
        # Appendable: hardlink staging -> dest, KEEP staging open so appends
        # keep flowing to the shared inode (now visible at dest). Not finalized.
        if self._staging is None:
            raise RuntimeError("link_into on a non-staging FileMedia")
        sdir, sname, _ = self._staging
        dst_dir_fd, dst_name = dest
        containment.hardlink(sdir, sname, dst_dir_fd, dst_name)
        self._dest = dest
        return None

    def seal(self):
        # Appendable terminal: close the append fd, unlink the staging NAME (the
        # destination hardlink + inode survive), freeze md5, drop staging.
        if self._staging is None:
            return None
        sdir, sname, append_fd = self._staging
        containment.maybe_fsync(append_fd)  # appended bytes durable pre-close
        os.close(append_fd)
        containment.unlink_at(sdir, sname)
        os.close(sdir)
        self._md5 = self._md5.digest()
        self._staging = None
        return None

    @classmethod
    def from_existing(cls, dir_fd, name, *, size, crc32c_value, md5_value):
        # Hydration: trust the persisted sidecar checksums; do NOT re-read the
        # whole file at startup (bounded memory on a multi-GB tree).
        fd = containment.safe_open(dir_fd, name, os.O_RDONLY)
        return cls(fd, size, crc32c_value, md5_value)

    @classmethod
    def from_path(cls, dir_fd, name):
        # Test/convenience: open an existing file and compute checksums once.
        fd = containment.safe_open(dir_fd, name, os.O_RDONLY)
        return cls._from_open_fd(fd)
