# tests/test_sigkill_upload.py
import os
import shutil
import tempfile
import unittest

import requests

from tests.conformance.emulator import Emulator

_PROJECT = "test-project"


class TestSigkillMidUpload(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="testbench-sigkill-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bucket = "kill-bucket"

    def test_partial_upload_is_invisible_after_sigkill_restart(self):
        control = b"i-am-fully-committed" * 50
        partial_first = b"A" * 512  # a single NON-final resumable chunk
        with Emulator(store="file", root=self.root) as e1:
            base = e1.rest_url
            requests.post(
                base + "/storage/v1/b",
                params={"project": _PROJECT},
                json={"name": self.bucket},
                timeout=30,
            ).raise_for_status()
            # Control object: fully inserted, must survive the kill intact.
            requests.post(
                base + "/upload/storage/v1/b/%s/o" % self.bucket,
                params={"uploadType": "media", "name": "control.bin"},
                data=control,
                timeout=30,
            ).raise_for_status()
            # Start a resumable upload for the DOOMED object.
            start = requests.post(
                base + "/upload/storage/v1/b/%s/o" % self.bucket,
                params={"uploadType": "resumable", "name": "doomed.bin"},
                json={"name": "doomed.bin"},
                timeout=30,
            )
            start.raise_for_status()
            upload_path = start.headers["Location"]
            if upload_path.startswith(base):
                upload_path = upload_path[len(base) :]
            # Send exactly ONE non-final chunk with an OPEN-ENDED total
            # (Content-Range ".../*" => "more to come"). upload.complete stays
            # False -- neither `total_object_size == len(upload.media)` nor
            # `chunk_last_byte + 1 == total_object_size` holds (rest_server.py:
            # ~1331) -- so the server MUST answer 308 Resume Incomplete: a
            # stable, client-controlled pause, NOT a timer racing the upload.
            chunk = requests.put(
                base + upload_path,
                data=partial_first,
                headers={"Content-Range": "bytes 0-%d/*" % (len(partial_first) - 1)},
                timeout=30,
            )
            self.assertEqual(308, chunk.status_code)  # assert the pause BEFORE killing
            # At this point the bytes are provably staged under
            # .gcs/uploads/<upload_id> and object_inserted has NOT run: there is
            # no doomed.bin at its natural path and no doomed.bin.gcsmeta.
            self.assertFalse(
                os.path.exists(os.path.join(self.root, self.bucket, "doomed.bin"))
            )
            self.assertFalse(
                os.path.exists(
                    os.path.join(self.root, self.bucket, "doomed.bin.gcsmeta")
                )
            )
            e1.kill()  # SIGKILL the whole group; no graceful sidecar flush
        # Restart over the SAME root. rebuild_index is sidecar-driven and skips
        # .gcs/uploads/, so the staged bytes are never adopted. (This relaunch
        # over the same root is also the kill() group-kill observable, Task 1
        # Step 5: a leaked live worker would hold the root lock and fail e2.)
        with Emulator(store="file", root=self.root) as e2:
            base = e2.rest_url
            # NEGATIVE: the partial object is invisible.
            doomed = requests.get(
                base + "/storage/v1/b/%s/o/%s" % (self.bucket, "doomed.bin"),
                timeout=30,
            )
            self.assertEqual(404, doomed.status_code)
            # POSITIVE: the control object is intact, byte-for-byte.
            got = requests.get(
                base + "/storage/v1/b/%s/o/%s" % (self.bucket, "control.bin"),
                params={"alt": "media"},
                timeout=30,
            )
            got.raise_for_status()
            self.assertEqual(control, got.content)
            # The bucket itself survived (bucket.json intact).
            bkt = requests.get(base + "/storage/v1/b/%s" % self.bucket, timeout=30)
            self.assertEqual(200, bkt.status_code)
