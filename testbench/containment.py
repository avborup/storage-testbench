"""Fd-based filesystem containment: the backstop that holds even if pathing.py
has a bug or a symlink is swapped in between a check and an open (spec Security
rules 1, 3, 5). Every path component is opened via openat with O_NOFOLLOW, so
no symlink at any component can redirect a write outside the bucket root."""

import os
import shutil

from testbench import pathing

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

FSYNC = os.environ.get("TESTBENCH_FSYNC") == "1"


def maybe_fsync(fd):
    # No-op unless TESTBENCH_FSYNC=1. fsync changes DURABILITY, not bytes, so the
    # B==C golden and the memory digest are unaffected; this internal guard is the
    # SINGLE gate (callers invoke unconditionally) so the default path adds ZERO
    # syscalls (spec: os.replace ordering is the default durability guarantee).
    # Referenced as a module attribute at call time so a test can flip
    # `containment.FSYNC` without re-import.
    if FSYNC:
        os.fsync(fd)


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
            handle.flush()  # ensure the userspace buffer is on the fd
            maybe_fsync(handle.fileno())  # UNCONDITIONAL; no-op unless FSYNC
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        maybe_fsync(dir_fd)  # UNCONDITIONAL; durably link the new name in
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def open_staging(dir_fd, name):
    """Open a single-component staging file O_CREAT|O_RDWR|O_APPEND|O_NOFOLLOW
    under dir_fd. O_NOFOLLOW refuses a symlink at the final component."""
    if "/" in name:
        raise ValueError("staging name %r is not a single component" % name)
    return os.open(
        name,
        os.O_CREAT | os.O_RDWR | os.O_APPEND | _O_NOFOLLOW,
        0o644,
        dir_fd=dir_fd,
    )


def promote(src_dir_fd, src_name, dst_dir_fd, dst_name):
    """Contained cross-dir os.replace(staging -> dest). Both names are single
    components opened relative to their dir_fds, so a symlink at the destination
    name is replaced in-place inside the dir rather than followed (mirrors
    write_bytes_atomic's fd discipline). Same-filesystem os.replace is O(1); a
    cross-device root raises OSError(EXDEV) rather than silently degrading.

    Containment carve-out: for a *single-component* destination whose directory
    is already a pinned fd, the fd-relative os.replace is an EQUIVALENT MUTANT vs
    a same-dir raw-path os.replace -- rename(2) never dereferences a symlink at
    the final destination component (it replaces the symlink inode in place), so
    both forms are byte-identical (verified: the raw-path Task-3 Step-5 mutation
    does not kill test_promote_is_fd_contained_not_pathname). The load-bearing,
    mutation-killed guard here is the single-component ValueError below (killed
    by test_promote_rejects_slash_bearing_name). The fd-relative discipline is
    kept as defense-in-depth: it is load-bearing only when the *directory* is
    reached via a swappable path -- which the emulator's O_NOFOLLOW walk pins --
    a case a stable-root unit test cannot exercise."""
    if "/" in src_name or "/" in dst_name:
        raise ValueError("promote names must be single components")
    os.replace(src_name, dst_name, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)


def hardlink(src_dir_fd, src_name, dst_dir_fd, dst_name):
    """Contained cross-dir os.link(staging -> dest) for the appendable path: the
    destination becomes a second name for the staging inode, so O_APPEND writes
    to the staging fd are immediately visible at the destination. fd-relative and
    single-component, same containment guarantee as promote()."""
    if "/" in src_name or "/" in dst_name:
        raise ValueError("hardlink names must be single components")
    os.link(src_name, dst_name, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)


def unlink_at(dir_fd, name):
    """Contained single-component unlink (seal / staging cleanup)."""
    if "/" in name:
        raise ValueError("unlink name %r is not a single component" % name)
    os.unlink(name, dir_fd=dir_fd)


def constrained_rmtree(path, root, index_names):
    # Three load-bearing guards, each independently mutation-killable: the
    # direct-child parent check, the islink/isdir real-directory check, and the
    # index-membership check. A caller must satisfy all three before any
    # removal happens.
    #
    # `realpath` (not string `normpath`) on the parent check is intentional
    # defense-in-depth. In this codebase it is an EQUIVALENT MUTANT vs normpath
    # -- FileStore.__init__ pre-canonicalizes its root and the islink guard
    # already rejects a symlinked final component, so no reachable call can make
    # realpath and normpath disagree here (which is why the normpath mutation
    # survives; that is by design, not a vacuous guard). It stays as a backstop
    # against a future caller that passes a non-canonical root or reorders the
    # guards. See the plan's Task-5 Step-5 note.
    if not os.path.lexists(path):
        return  # defensive teardown: already gone (spec Test-isolation)
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
