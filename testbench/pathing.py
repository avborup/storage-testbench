"""Pure name -> on-disk-name policy for the file backend. No filesystem I/O.

The file backend's bucket-name check is the ONLY check (spec Security rule 4);
gcs/bucket.py's validator has a live bypass and is deliberately left unchanged.
Object names route unrepresentable/hostile cases to a SHA-256 overflow name
that contains no caller bytes, so path traversal is sidestepped by
construction (spec Security rule 2)."""

import hashlib
import os
import re

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]{1,61}[a-z0-9]$")
NAME_MAX = 255
RESERVED_PREFIX = ".gcs/"
RESERVED_SUFFIX = ".gcsmeta"


def validate_bucket_name(name):
    """The file backend's SOLE bucket-name check (spec Security rule 4).

    Load-bearing clauses: `_BUCKET_RE` (the char-class regex) and the `goog`
    reservation. Each is individually mutation-killable (drop the regex and
    "Upper" is wrongly accepted; drop the goog clause and "goog-x" is wrongly
    accepted).

    The earlier explicit clauses (NUL / leading-slash / "."/".." / traversal
    segment / slash / length) are INTENTIONAL defense-in-depth: they give a
    precise error and remain a backstop should `_BUCKET_RE` ever be relaxed.
    They are *equivalent mutants* today -- fully subsumed by `_BUCKET_RE`
    (which rejects any "/", uppercase, out-of-range length via {1,61}+anchors,
    NUL, and leading/trailing dot), so deleting one does not fail a test. That
    is by design, not a vacuous guard; see the plan's Task-4 Step-5 note and the
    "Mutation-check every guard clause" constraint's defense-in-depth carve-out.
    """
    if "\x00" in name or name.startswith("/") or name in (".", ".."):
        raise ValueError("illegal bucket name %r" % name)
    for part in name.split("/"):
        if part in (".", ".."):
            raise ValueError("traversal segment in bucket name %r" % name)
    if "/" in name:
        raise ValueError("slash in bucket name %r" % name)
    if not (3 <= len(name) <= 63) and "." not in name:
        raise ValueError("bucket name length %r" % name)
    if _BUCKET_RE.match(name) is None:
        raise ValueError("bucket name char-class %r" % name)
    if name.startswith("goog") or re.search("g[0o][0o]g[1l][e3]", name):
        raise ValueError("reserved goog bucket name %r" % name)


def _needs_overflow(object_name):
    if object_name.endswith("/"):
        return True
    if object_name.startswith(RESERVED_PREFIX) or object_name.endswith(RESERVED_SUFFIX):
        return True
    if "\x00" in object_name or object_name.startswith("/"):
        return True
    for seg in object_name.split("/"):
        if seg in (".", "..") or len(seg.encode("utf-8")) > NAME_MAX:
            return True
    return False


def escape(object_name):
    # Minimal, reversible: percent-encode only "%" (the escape char) so a
    # natural name survives round-trip; ordinary names appear pristine.
    return object_name.replace("%", "%25")


def unescape(escaped):
    return escaped.replace("%25", "%")


def classify(object_name):
    if _needs_overflow(object_name):
        return "overflow", hashlib.sha256(object_name.encode("utf-8")).hexdigest()
    return "natural", escape(object_name)


def is_contained(candidate_abspath, root_abspath):
    root = os.path.normpath(root_abspath)
    cand = os.path.normpath(candidate_abspath)
    return cand == root or cand.startswith(root + os.sep)
