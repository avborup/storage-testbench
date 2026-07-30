# tests/test_pathing.py
import hashlib
import unittest

from hypothesis import given
from hypothesis import strategies as st

from testbench import pathing

# Legal GCS object names are valid UTF-8 (1-1024 bytes). codec="utf-8" excludes
# lone surrogates U+D800..U+DFFF, which are NOT valid UTF-8, cannot arrive as a
# real object name (proto/JSON string fields enforce UTF-8), and would make
# .encode("utf-8") raise -- so they are outside classify()'s real domain, not a
# case to cover. min_codepoint=1 already excludes NUL (codepoint 0).
_LEGAL_NAME = st.text(
    alphabet=st.characters(min_codepoint=1, max_codepoint=0x10FFFF, codec="utf-8"),
    min_size=1,
    max_size=64,
)


class TestEscapeRoundTrip(unittest.TestCase):
    @given(_LEGAL_NAME)
    def test_unescape_inverts_escape(self, name):
        kind, target = pathing.classify(name)
        if kind == "natural":
            self.assertEqual(name, pathing.unescape(pathing.escape(name)))

    def test_overflow_cases_route_to_sha256(self):
        # One case per _needs_overflow clause: trailing slash, reserved prefix,
        # reserved suffix, NUL, leading slash, '.'/'..' segment, NAME_MAX oversize.
        for name in (
            "folder/",
            ".gcs/x",
            "audio/clip.wav.gcsmeta",
            "a\x00b",
            "/etc/passwd",
            "a/../b",
            "a/./b",
            ".",
            "..",
            "a" * 300,
        ):
            kind, target = pathing.classify(name)
            self.assertEqual("overflow", kind, name)
            self.assertEqual(hashlib.sha256(name.encode()).hexdigest(), target)

    def test_ordinary_names_stay_natural_and_pristine(self):
        for name in ("audio/clip.wav", "05-dir/nested.txt", "01-simple.txt"):
            kind, target = pathing.classify(name)
            self.assertEqual("natural", kind)
            self.assertEqual(name, pathing.unescape(target))


class TestValidateBucketName(unittest.TestCase):
    def test_rejects_traversal_and_illegal(self):
        for bad in (
            "../../etc/passwd",
            ".",
            "..",
            "a/b",
            "/etc",
            "a\x00b",
            "foo/../bar",
            "a" * 64,
            ".lead",
            "trail.",
            "Upper",
            "goog-x",
        ):
            with self.assertRaises(ValueError, msg=bad):
                pathing.validate_bucket_name(bad)

    def test_accepts_legal(self):
        for ok in ("my-bucket", "a1x", "abc.def.ghi", "test-uuid-123", "a_b1"):
            pathing.validate_bucket_name(ok)  # must not raise


class TestContainment(unittest.TestCase):
    def test_rejects_escape(self):
        self.assertFalse(pathing.is_contained("/data/../etc/passwd", "/data/b"))
        self.assertTrue(pathing.is_contained("/data/b/audio/clip.wav", "/data/b"))
