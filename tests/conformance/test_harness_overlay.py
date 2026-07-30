# tests/conformance/test_harness_overlay.py
import json
import os
import unittest

from tests.conformance import harness

HERE = os.path.dirname(os.path.abspath(__file__))


def _trace(interactions):
    # Same serializer the goldens use -> 4-space-indented "{...}" blocks with a
    # 6-space "label" line, exactly what _INTERACTION_START/_LABEL_LINE match.
    return harness.serialize({"interactions": interactions})


class TestAllowListOverlay(unittest.TestCase):
    def test_allowlist_file_exists_and_is_a_flat_mapping(self):
        with open(os.path.join(HERE, "allowlist.json")) as fh:
            data = json.load(fh)
        self.assertIsInstance(data, dict)
        for label, justification in data.items():
            self.assertIsInstance(label, str)
            self.assertIsInstance(justification, str)
            self.assertTrue(justification.strip(), "empty justification for %r" % label)

    def test_masked_overlay_tolerates_only_listed_labels(self):
        golden = _trace(
            [
                {"label": "create-bucket", "status": 200},
                {"label": "get-bucket", "status": 200},
            ]
        )
        observed = _trace(
            [
                {"label": "create-bucket", "status": 400},
                {"label": "get-bucket", "status": 200},
            ]
        )
        # listed -> masked equal -> empty residual
        self.assertEqual(
            "", harness.diff_with_allowlist(golden, observed, {"create-bucket": "x"})
        )
        # unlisted -> byte-exact masked compare fails
        self.assertNotEqual("", harness.diff_with_allowlist(golden, observed, {}))

    def test_divergence_outside_any_interaction_block_still_fails(self):
        # A change in the JSON wrapper (outside every "{...}" block) must fail
        # even when the differing labels are allow-listed -- the memory leg's
        # byte-for-byte guarantee, preserved.
        golden = _trace([{"label": "create-bucket", "status": 200}])
        observed = golden.replace('"interactions"', '"INTERACTIONS"')
        self.assertNotEqual(
            "", harness.diff_with_allowlist(golden, observed, {"create-bucket": "x"})
        )

    def test_duplicate_or_unlabeled_block_is_a_hard_error(self):
        dup = _trace([{"label": "x", "status": 1}, {"label": "x", "status": 2}])
        with self.assertRaises(ValueError):
            harness.diff_with_allowlist(dup, dup, {})
        nolabel = _trace([{"status": 1}])
        with self.assertRaises(ValueError):
            harness.diff_with_allowlist(nolabel, nolabel, {})

    def test_stale_allowlist_entry_is_reported(self):
        same = _trace([{"label": "create-bucket", "status": 200}])
        self.assertIn(
            "create-bucket",
            harness.stale_allowlist_labels(same, same, {"create-bucket": "x"}),
        )

    def test_committed_goldens_carry_no_volatile_root_token(self):
        for name in ("rest", "grpc", "faults"):
            with open(harness.golden_path(name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("TESTBENCH_ROOT", text)
            self.assertNotIn("testbench-conf-", text)
