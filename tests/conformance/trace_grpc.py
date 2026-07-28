#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""gRPC v2 conformance trace.

Field spellings here were read off the emulator's own handlers in
testbench/grpc_server.py, which is the authority on what it accepts. Step 5
runs the trace; a field name error surfaces there as a ValueError, not as a
silent gap in coverage.
"""

import grpc

from google.iam.v1 import iam_policy_pb2
from google.storage.control.v2 import storage_control_pb2, storage_control_pb2_grpc
from google.storage.v2 import storage_pb2, storage_pb2_grpc
from tests.conformance.recorder import Recorder

BUCKET = "grpc-bucket"
SOFT_DELETE = "grpc-soft-delete"
PROJECT = "projects/test-project"
PAYLOAD = b"The quick brown fox jumps over the lazy dog"
# The minimum the emulator accepts (gcs/bucket.py's `validate_soft_delete_policy`
# rejects anything under 7 days); the zero-value default on an
# otherwise-empty `SoftDeletePolicy()` message fails with INVALID_ARGUMENT.
SOFT_DELETE_RETENTION_SECONDS = 7 * 24 * 3600

# Named so that alphabetical order -- the order `ListObjects` returns them in,
# since `Database.list_object` (testbench/database.py) sorts on `item.name`
# regardless of transport -- matches creation order. The recorder's
# generation-monotonicity invariant tracks every generation value across the
# whole trace by first sighting, so a single `ListObjects` response whose name
# order differs from creation order trips it; see the trace-5 report.
SINGLE = "01-single.txt"
MULTI = "02-multi.txt"
BIDI = "03-bidi.txt"
RESUMABLE = "04-resumable.txt"


def _bucket_path(name):
    return "projects/_/buckets/%s" % name


def run(emulator):
    rec = Recorder("grpc")
    channel = grpc.insecure_channel(emulator.grpc_target)
    storage = storage_pb2_grpc.StorageStub(channel)
    control = storage_control_pb2_grpc.StorageControlStub(channel)

    def call(label, method, request):
        try:
            rec.record_grpc(label, method(request))
        except grpc.RpcError as error:
            rec.record_error(label, error)

    # --- buckets -----------------------------------------------------------
    # `bucket.project` must be left unset when `parent` is already the real
    # project path: `gcs/bucket.py`'s `init_grpc` accepts a project either as
    # `parent` (with `bucket.project` empty) or, when `parent == "projects/_"`,
    # as `bucket.project` instead -- never both. Setting both, as an earlier
    # draft of this trace did, fails every `CreateBucket` call with
    # INVALID_ARGUMENT ("invalid combination of `parent` and `bucket.project`
    # fields"), which -- caught by `call()`'s try/except -- silently turned
    # every downstream Write/Read/List step in this trace into a "bucket does
    # not exist" error instead of real coverage.
    call(
        "create-bucket",
        storage.CreateBucket,
        storage_pb2.CreateBucketRequest(
            parent=PROJECT, bucket_id=BUCKET, bucket=storage_pb2.Bucket()
        ),
    )
    call(
        "create-bucket-duplicate",
        storage.CreateBucket,
        storage_pb2.CreateBucketRequest(
            parent=PROJECT, bucket_id=BUCKET, bucket=storage_pb2.Bucket()
        ),
    )
    call(
        "get-bucket",
        storage.GetBucket,
        storage_pb2.GetBucketRequest(name=_bucket_path(BUCKET)),
    )
    call(
        "get-missing-bucket",
        storage.GetBucket,
        storage_pb2.GetBucketRequest(name=_bucket_path("absent")),
    )
    call(
        "list-buckets",
        storage.ListBuckets,
        storage_pb2.ListBucketsRequest(parent=PROJECT),
    )
    call(
        "get-iam-policy",
        storage.GetIamPolicy,
        iam_policy_pb2.GetIamPolicyRequest(resource=_bucket_path(BUCKET)),
    )

    # --- WriteObject, single message and multi message ---------------------
    def write_spec(name, bucket=BUCKET):
        return storage_pb2.WriteObjectSpec(
            resource=storage_pb2.Object(name=name, bucket=_bucket_path(bucket))
        )

    call(
        "write-object-single",
        storage.WriteObject,
        iter(
            [
                storage_pb2.WriteObjectRequest(
                    write_object_spec=write_spec(SINGLE),
                    write_offset=0,
                    checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD),
                    finish_write=True,
                ),
            ]
        ),
    )

    half = len(PAYLOAD) // 2
    call(
        "write-object-multi",
        storage.WriteObject,
        iter(
            [
                storage_pb2.WriteObjectRequest(
                    write_object_spec=write_spec(MULTI),
                    write_offset=0,
                    checksummed_data=storage_pb2.ChecksummedData(
                        content=PAYLOAD[:half]
                    ),
                ),
                storage_pb2.WriteObjectRequest(
                    write_offset=half,
                    checksummed_data=storage_pb2.ChecksummedData(
                        content=PAYLOAD[half:]
                    ),
                    finish_write=True,
                ),
            ]
        ),
    )

    # --- BidiWriteObject, with a flush and a state lookup -----------------
    def bidi_write():
        yield storage_pb2.BidiWriteObjectRequest(
            write_object_spec=write_spec(BIDI),
            write_offset=0,
            checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD[:half]),
            flush=True,
            state_lookup=True,
        )
        yield storage_pb2.BidiWriteObjectRequest(
            write_offset=half,
            checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD[half:]),
            finish_write=True,
        )

    try:
        for index, response in enumerate(storage.BidiWriteObject(bidi_write())):
            rec.record_grpc("bidi-write-response-%d" % index, response)
    except grpc.RpcError as error:
        rec.record_error("bidi-write", error)

    # --- resumable write --------------------------------------------------
    try:
        started = storage.StartResumableWrite(
            storage_pb2.StartResumableWriteRequest(
                write_object_spec=write_spec(RESUMABLE)
            )
        )
        rec.record_grpc("start-resumable-write", started)
        upload_id = started.upload_id
        call(
            "query-write-status-empty",
            storage.QueryWriteStatus,
            storage_pb2.QueryWriteStatusRequest(upload_id=upload_id),
        )
        call(
            "resume-write",
            storage.WriteObject,
            iter(
                [
                    storage_pb2.WriteObjectRequest(
                        upload_id=upload_id,
                        write_offset=0,
                        checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD),
                        finish_write=True,
                    ),
                ]
            ),
        )
    except grpc.RpcError as error:
        rec.record_error("start-resumable-write", error)

    # --- reads -------------------------------------------------------------
    call(
        "get-object",
        storage.GetObject,
        storage_pb2.GetObjectRequest(bucket=_bucket_path(BUCKET), object=SINGLE),
    )
    call(
        "get-missing-object",
        storage.GetObject,
        storage_pb2.GetObjectRequest(bucket=_bucket_path(BUCKET), object="absent.txt"),
    )
    call(
        "get-object-precondition-mismatch",
        storage.GetObject,
        storage_pb2.GetObjectRequest(
            bucket=_bucket_path(BUCKET), object=SINGLE, if_generation_match=1
        ),
    )

    def read(label, **kwargs):
        request = storage_pb2.ReadObjectRequest(
            bucket=_bucket_path(BUCKET), object=SINGLE, **kwargs
        )
        try:
            chunks = [r.checksummed_data.content for r in storage.ReadObject(request)]
            rec.record_stream(label, chunks)
        except grpc.RpcError as error:
            rec.record_error(label, error)

    read("read-object-full")
    read("read-object-ranged", read_offset=10, read_limit=10)
    read("read-object-negative-offset", read_offset=-10)
    read("read-object-offset-past-end", read_offset=9999)

    def bidi_read(label, ranges):
        request = storage_pb2.BidiReadObjectRequest(
            read_object_spec=storage_pb2.BidiReadObjectSpec(
                bucket=_bucket_path(BUCKET), object=SINGLE
            ),
            read_ranges=ranges,
        )
        try:
            chunks = []
            for response in storage.BidiReadObject(iter([request])):
                for data in response.object_data_ranges:
                    chunks.append(data.checksummed_data.content)
            rec.record_stream(label, chunks)
        except grpc.RpcError as error:
            rec.record_error(label, error)

    bidi_read(
        "bidi-read-two-ranges",
        [
            storage_pb2.ReadRange(read_offset=0, read_length=10, read_id=1),
            storage_pb2.ReadRange(read_offset=20, read_length=10, read_id=2),
        ],
    )

    # --- listing and metadata ---------------------------------------------
    call(
        "list-objects",
        storage.ListObjects,
        storage_pb2.ListObjectsRequest(parent=_bucket_path(BUCKET)),
    )
    call(
        "list-objects-prefix",
        storage.ListObjects,
        storage_pb2.ListObjectsRequest(parent=_bucket_path(BUCKET), prefix=MULTI),
    )
    call(
        "list-objects-delimiter",
        storage.ListObjects,
        storage_pb2.ListObjectsRequest(parent=_bucket_path(BUCKET), delimiter="/"),
    )

    # --- compose, rewrite with continuation, move -------------------------
    call(
        "compose-object",
        storage.ComposeObject,
        storage_pb2.ComposeObjectRequest(
            destination=storage_pb2.Object(
                name="composed.txt", bucket=_bucket_path(BUCKET)
            ),
            source_objects=[
                storage_pb2.ComposeObjectRequest.SourceObject(name=SINGLE),
                storage_pb2.ComposeObjectRequest.SourceObject(name=MULTI),
            ],
        ),
    )

    # A small max_bytes_rewritten_per_call was meant to force a continuation
    # token, exercising the multi-call rewrite path rather than the
    # single-call shortcut. In practice it does not: `Rewrite._normalize_max_bytes`
    # (gcs/rewrite.py) clamps any smaller value up to `MIN_REWRITE_BYTES` (1
    # MiB), and PAYLOAD is 44 bytes, so the very first call always finishes
    # the whole object and `rewrite-step-0` is the only interaction this loop
    # ever produces -- confirmed empirically (`total_bytes_rewritten ==
    # object_size`, `done: true`, on step 0). Genuine multi-call coverage
    # would need a source object over 1 MiB, which is a bigger fixture than
    # this trace's payloads elsewhere; the loop is kept exactly as specified,
    # gracefully handling either outcome (`if not token: break`), and this is
    # reported as a real coverage gap rather than fabricated with a payload
    # the brief did not ask for.
    token = ""
    for step in range(6):
        request = storage_pb2.RewriteObjectRequest(
            source_bucket=_bucket_path(BUCKET),
            source_object=SINGLE,
            destination_bucket=_bucket_path(BUCKET),
            destination_name="rewritten.txt",
            max_bytes_rewritten_per_call=16,
            rewrite_token=token,
        )
        try:
            response = storage.RewriteObject(request)
            rec.record_grpc("rewrite-step-%d" % step, response)
            token = response.rewrite_token
            if not token:
                break
        except grpc.RpcError as error:
            rec.record_error("rewrite-step-%d" % step, error)
            break

    call(
        "move-object",
        storage.MoveObject,
        storage_pb2.MoveObjectRequest(
            bucket=_bucket_path(BUCKET),
            source_object="rewritten.txt",
            destination_object="moved.txt",
        ),
    )
    call(
        "delete-object",
        storage.DeleteObject,
        storage_pb2.DeleteObjectRequest(
            bucket=_bucket_path(BUCKET), object="moved.txt"
        ),
    )
    call(
        "delete-missing-object",
        storage.DeleteObject,
        storage_pb2.DeleteObjectRequest(
            bucket=_bucket_path(BUCKET), object="moved.txt"
        ),
    )

    # --- soft delete and restore ------------------------------------------
    soft_delete_policy = storage_pb2.Bucket.SoftDeletePolicy()
    soft_delete_policy.retention_duration.FromSeconds(SOFT_DELETE_RETENTION_SECONDS)
    call(
        "create-soft-delete-bucket",
        storage.CreateBucket,
        storage_pb2.CreateBucketRequest(
            parent=PROJECT,
            bucket_id=SOFT_DELETE,
            bucket=storage_pb2.Bucket(soft_delete_policy=soft_delete_policy),
        ),
    )
    call(
        "soft-delete-write",
        storage.WriteObject,
        iter(
            [
                storage_pb2.WriteObjectRequest(
                    write_object_spec=write_spec("sd.txt", SOFT_DELETE),
                    write_offset=0,
                    checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD),
                    finish_write=True,
                ),
            ]
        ),
    )
    # The generation to restore is read back from the listing rather than
    # remembered, so this stays valid if generation assignment changes.
    listing = storage.ListObjects(
        storage_pb2.ListObjectsRequest(parent=_bucket_path(SOFT_DELETE))
    )
    generation = listing.objects[0].generation if listing.objects else 0
    call(
        "soft-delete-object",
        storage.DeleteObject,
        storage_pb2.DeleteObjectRequest(
            bucket=_bucket_path(SOFT_DELETE), object="sd.txt"
        ),
    )
    call(
        "restore-object",
        storage.RestoreObject,
        storage_pb2.RestoreObjectRequest(
            bucket=_bucket_path(SOFT_DELETE), object="sd.txt", generation=generation
        ),
    )

    # --- folders and layout, on the control stub --------------------------
    call(
        "get-storage-layout",
        control.GetStorageLayout,
        storage_control_pb2.GetStorageLayoutRequest(
            name="%s/storageLayout" % _bucket_path(BUCKET)
        ),
    )
    call(
        "create-folder",
        control.CreateFolder,
        storage_control_pb2.CreateFolderRequest(
            parent=_bucket_path(BUCKET), folder_id="folder-a/"
        ),
    )
    call(
        "get-folder",
        control.GetFolder,
        storage_control_pb2.GetFolderRequest(
            name="%s/folders/folder-a/" % _bucket_path(BUCKET)
        ),
    )
    call(
        "list-folders",
        control.ListFolders,
        storage_control_pb2.ListFoldersRequest(parent=_bucket_path(BUCKET)),
    )
    # Two confirmed production bugs in `RenameFolder`, both in
    # testbench/grpc_server.py (out of scope to fix here; kept and recorded
    # verbatim so they stay pinned as documented findings rather than
    # silently dropped):
    #
    # 1. The real Storage Control v2 API declares RenameFolder's response as
    #    `google.longrunning.Operation` ("the source and destination folders
    #    are locked until the long running operation completes", per the
    #    generated stub's own docstring), but the handler returns a bare
    #    `Folder` message instead. gRPC deserializes whatever bytes the
    #    handler wrote using the *client's* expected type (`Operation`), so
    #    the recorded interaction shows the resulting mismatch: `done: true`
    #    with an empty `response`.
    # 2. `Database.rename_folder` (testbench/database.py) stores the renamed
    #    folder under the bare `destination_folder_id` ("folder-b/") rather
    #    than a bucket-qualified key like `CreateFolder` uses
    #    (f"{parent}/folders/{folder_id}"). Combined with deleting the
    #    source key outright, this leaves the folder unreachable under
    #    *either* name afterwards: confirmed empirically -- a `GetFolder` for
    #    "folder-a/" 404s (deleted), and so does one for "folder-b/" (stored
    #    under the wrong key), and `list-folders` run right after a rename
    #    returns nothing at all.
    #
    # Because of bug 2, there is no way to reach "folder-a/" or "folder-b/"
    # for a genuine `DeleteFolder` after this rename, so `delete-folder`
    # below targets a second, never-renamed folder instead -- this trace
    # still exercises one real, successful DeleteFolder call rather than a
    # perpetual, uninformative 404.
    call(
        "rename-folder",
        control.RenameFolder,
        storage_control_pb2.RenameFolderRequest(
            name="%s/folders/folder-a/" % _bucket_path(BUCKET),
            destination_folder_id="folder-b/",
        ),
    )
    call(
        "create-folder-for-delete",
        control.CreateFolder,
        storage_control_pb2.CreateFolderRequest(
            parent=_bucket_path(BUCKET), folder_id="folder-c/"
        ),
    )
    call(
        "delete-folder",
        control.DeleteFolder,
        storage_control_pb2.DeleteFolderRequest(
            name="%s/folders/folder-c/" % _bucket_path(BUCKET)
        ),
    )

    return rec.finish()
