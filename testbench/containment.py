"""Fd-based filesystem containment: the backstop that holds even if pathing.py
has a bug or a symlink is swapped in between a check and an open (spec Security
rules 1, 3, 5). Every path component is opened via openat with O_NOFOLLOW, so
no symlink at any component can redirect a write outside the bucket root."""

import os
import shutil

from testbench import pathing

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def assert_posix_support():
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError(
            "file backend requires POSIX openat/O_NOFOLLOW (dir_fd) support"
        )


def assert_within(path, root):
    if not pathing.is_contained(os.path.realpath(path), os.path.realpath(root)):
        raise PermissionError("path %r escapes root %r" % (path, root))


def open_dir_nofollow(dir_fd, name):
    return os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=dir_fd)


def open_bucket_root_fd(root_fd, short):
    # `short` is a validated bucket name: no slash, no '.'/'..' -> one component.
    return open_dir_nofollow(root_fd, short)


def walk_dirs(dir_fd, parts, create):
    """Return a dirfd for the leaf of `parts` relative to `dir_fd`, opening each
    component with O_NOFOLLOW (optionally mkdirat-ing it first). Returns
    `dir_fd` itself when `parts` is empty; otherwise a NEW fd the caller closes."""
    cur = dir_fd
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=cur)
                except FileExistsError:
                    pass
            nxt = open_dir_nofollow(cur, part)
            if cur != dir_fd:
                os.close(cur)
            cur = nxt
        return cur
    except BaseException:
        if cur != dir_fd:
            os.close(cur)
        raise


def safe_open(dir_fd, name, flags, mode=0o644):
    return os.open(name, flags | _O_NOFOLLOW, mode, dir_fd=dir_fd)


def write_bytes_atomic(dir_fd, name, data):
    tmp = ".tmp-%d-%s" % (os.getpid(), name)
    fd = safe_open(dir_fd, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def constrained_rmtree(path, root, index_names):
    if not os.path.lexists(path):
        return  # defensive teardown: already gone (spec Test-isolation)
    # realpath (not string normpath) so a symlinked component cannot make an
    # out-of-root target look like a direct child of root.
    real = os.path.realpath(path)
    if os.path.dirname(real) != os.path.realpath(root):
        raise PermissionError(
            "rmtree target %r is not a direct child of %r" % (path, root)
        )
    if os.path.islink(path) or not os.path.isdir(path):
        raise PermissionError("rmtree target %r is not a real directory" % path)
    if os.path.basename(real) not in index_names:
        raise PermissionError("rmtree target %r not present in index" % path)
    shutil.rmtree(real)
