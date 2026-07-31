# tests/test_grpc_concurrency.py
import concurrent.futures
import hashlib
import os
import shutil
import tempfile
import threading
import unittest

import crc32c
import grpc

import gcs.bucket
import testbench.common
import testbench.database
import testbench.grpc_server
from google.storage.v2 import storage_pb2, storage_pb2_grpc
from testbench.filestore import FileStore

MiB = 1024 * 1024
_BUCKET = "projects/_/buckets/conc-bucket"


def _content(i, size):
    seed = b"obj-%04d-" % i
    return (seed * (size // len(seed) + 1))[:size]


class TestGrpcConcurrency(unittest.TestCase):
    def setUp(self):
        # Reuse test_grpc_threads.py's env save/restore discipline.
        self._saved_threads = os.environ.pop("TESTBENCH_GRPC_THREADS", None)
        self._saved_store = os.environ.get("TESTBENCH_STORE")
        os.environ["TESTBENCH_STORE"] = "file"  # forces loopback bind
        self.root = tempfile.mkdtemp(prefix="testbench-conc-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.n_objects = 12
        self.obj_size = 4 * MiB
        self.expected = {}
        db = testbench.database.Database.init(store=FileStore(self.root))
        req = testbench.common.FakeRequest(args={}, data='{"name": "conc-bucket"}')
        bucket, _ = gcs.bucket.Bucket.init(req, None)
        db.insert_bucket(bucket, None)
        self.db = db

    def tearDown(self):
        if self._saved_threads is None:
            os.environ.pop("TESTBENCH_GRPC_THREADS", None)
        else:
            os.environ["TESTBENCH_GRPC_THREADS"] = self._saved_threads
        if self._saved_store is None:
            os.environ.pop("TESTBENCH_STORE", None)
        else:
            os.environ["TESTBENCH_STORE"] = self._saved_store

    def _mock_context(self):
        import unittest.mock

        ctx = unittest.mock.Mock()
        ctx.invocation_metadata = unittest.mock.Mock(return_value=dict())
        return ctx

    def _seed(self):
        # Seed distinct per-object content via the real WriteObject servicer
        # (the from-existing hydration path), recording expected crc32c so a
        # cross-stream mix-up on a shared worker is detectable, not just
        # completion. Request-generator shape mirrors test_filemedia_restart.py.
        grpc_servicer = testbench.grpc_server.StorageServicer(self.db)
        for i in range(self.n_objects):
            data = _content(i, self.obj_size)
            self.expected["obj-%d" % i] = crc32c.crc32c(data)

            def reqs(name=("obj-%d" % i), payload=data):
                yield storage_pb2.WriteObjectRequest(
                    write_object_spec=storage_pb2.WriteObjectSpec(
                        resource={"name": name, "bucket": _BUCKET},
                    ),
                    write_offset=0,
                    checksummed_data=storage_pb2.ChecksummedData(
                        content=payload, crc32c=crc32c.crc32c(payload)
                    ),
                    finish_write=True,
                )

            grpc_servicer.WriteObject(reqs(), context=self._mock_context())

    def _start_server(self):
        port, server = testbench.grpc_server.run(0, self.db)
        self.addCleanup(server.stop, None)
        channel = grpc.insecure_channel("127.0.0.1:%d" % port)
        self.addCleanup(channel.close)
        return storage_pb2_grpc.StorageStub(channel)

    def test_metadata_not_starved_by_held_streams(self):
        # Pin a healthy-but-small pool so the contrast is sharp and fast: with 8
        # workers, park 4 with held-open BidiWriteObject streams, leaving
        # headroom; an interleaved GetObject with a deadline MUST still complete.
        # At pool=2 (< the 4 parked) every worker is consumed by a parked stream
        # and the GetObject starves -> DeadlineExceeded -> this FAILS.
        os.environ["TESTBENCH_GRPC_THREADS"] = "8"
        self._seed()
        stub = self._start_server()
        release = threading.Event()
        self.addCleanup(release.set)
        parked = 4
        occupied = [threading.Event() for _ in range(parked)]

        def held_stream(idx):
            # First request carries state_lookup=True: the server yields a
            # persisted_size BidiWriteObjectResponse (gcs/upload.py:663-668),
            # which PROVES its worker is occupied. We set occupied[idx] on that
            # first response, THEN the generator blocks on `release`, parking the
            # worker deterministically. BidiWrite has NO 10s auto-cancel (only
            # BidiRead does, grpc_server.py:842), so the park is stable.
            def gen():
                yield storage_pb2.BidiWriteObjectRequest(
                    write_object_spec=storage_pb2.WriteObjectSpec(
                        resource={"name": "held-%d" % idx, "bucket": _BUCKET},
                    ),
                    write_offset=0,
                    state_lookup=True,
                )
                release.wait(30)  # released in the cleanup below

            try:
                for _resp in stub.BidiWriteObject(gen(), timeout=30):
                    occupied[idx].set()  # first response => worker parked
            except grpc.RpcError:
                pass  # cancelled at teardown -- expected

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=parked)
        self.addCleanup(pool.shutdown, False)
        for i in range(parked):
            pool.submit(held_stream, i)
        # Readiness barrier: wait until ALL parked streams have proven occupancy
        # via their first server response -- deterministic regardless of machine
        # speed, with NO fixed sleep.
        for i, ev in enumerate(occupied):
            self.assertTrue(ev.wait(30), "held stream %d never occupied a worker" % i)
        # Interleaved metadata op with a generous client deadline. With headroom
        # (8 - 4 = 4 free) this returns promptly; at pool=2 it raises.
        resp = stub.GetObject(
            storage_pb2.GetObjectRequest(bucket=_BUCKET, object="obj-0"),
            timeout=10,
        )
        self.assertEqual("obj-0", resp.name)
        release.set()

    def test_parallel_streams_and_metadata_all_correct(self):
        # Default-healthy pool. N concurrent streaming ReadObject transfers of
        # distinct multi-MiB objects + interleaved GetObject/ListObjects, each
        # with a per-RPC deadline. Every stream is verified by crc32c over its
        # concatenated bytes so a cross-stream mix-up (wrong object on a shared
        # worker) is caught, not just completion. At pool=2 the streams serialize
        # and the metadata ops queue past the deadline -> DeadlineExceeded.
        os.environ["TESTBENCH_GRPC_THREADS"] = "32"
        self._seed()
        stub = self._start_server()

        def read_stream(i):
            name = "obj-%d" % i
            body = b"".join(
                r.checksummed_data.content
                for r in stub.ReadObject(
                    storage_pb2.ReadObjectRequest(bucket=_BUCKET, object=name),
                    timeout=30,
                )
            )
            return name, crc32c.crc32c(body)

        def meta_op(k):
            if k % 2 == 0:
                r = stub.GetObject(
                    storage_pb2.GetObjectRequest(
                        bucket=_BUCKET, object="obj-%d" % (k % self.n_objects)
                    ),
                    timeout=10,
                )
                return r.name
            r = stub.ListObjects(
                storage_pb2.ListObjectsRequest(parent=_BUCKET), timeout=10
            )
            return len(r.objects)

        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
            read_futs = [pool.submit(read_stream, i) for i in range(self.n_objects)]
            meta_futs = [pool.submit(meta_op, k) for k in range(40)]
            for f in read_futs:
                name, got_crc = f.result(timeout=60)
                self.assertEqual(
                    self.expected[name], got_crc, "stream %r bytes wrong" % name
                )
            for f in meta_futs:
                f.result(timeout=60)  # no DeadlineExceeded == no starvation


if __name__ == "__main__":
    unittest.main()
