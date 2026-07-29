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
import requests
from google.protobuf import field_mask_pb2

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
# generation-monotonicity invariant (`canonicalize.py`'s `_bind`) only
# compares a value's *first* sighting anywhere in the trace to whatever was
# first-sighted immediately before it; a listing that merely re-reports
# already-bound generations appends nothing and stays silent. This naming
# keeps the trace robust against a listing itself being the first place an
# out-of-order generation is exposed. See the trace-5 report for the full
# account.
SINGLE = "01-single.txt"
MULTI = "02-multi.txt"
BIDI = "03-bidi.txt"
RESUMABLE = "04-resumable.txt"
NESTED = "05-dir/nested.txt"


def _bucket_path(name):
    return "projects/_/buckets/%s" % name


def run(emulator):
    rec = Recorder("grpc")
    base = emulator.rest_url
    session = requests.Session()
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
    # UpdateBucket had zero coverage before this, yet Plan 3's first task routes
    # bucket mutations through Database -- so the gate must be able to see a
    # bucket metadata change. `labels` is the one field UpdateBucket's own
    # immutable-field guard (grpc_server.py:UpdateBucket) permits; the FieldMask
    # scopes the write to it. `get-bucket` above still reports metageneration 1,
    # so the increment to 2 here is itself observable in the golden.
    call(
        "update-bucket",
        storage.UpdateBucket,
        storage_pb2.UpdateBucketRequest(
            bucket=storage_pb2.Bucket(
                name=_bucket_path(BUCKET), labels={"env": "conformance"}
            ),
            update_mask=field_mask_pb2.FieldMask(paths=["labels"]),
        ),
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

    # The golden's recorded `offsets` end `[0, 10, 20, 20]`, not `[0, 10, 20]`:
    # a trailing zero-length chunk at offset 20 (where the second range
    # both starts and, given its own length, ends) is genuine emulator
    # behavior from `BidiReadObject`'s chunking, not a `record_stream`
    # artifact -- `record_stream` only records the boundaries it is handed,
    # it does not invent one. Confirmed against a live emulator before this
    # trace was written.
    bidi_read(
        "bidi-read-two-ranges",
        [
            storage_pb2.ReadRange(read_offset=0, read_length=10, read_id=1),
            storage_pb2.ReadRange(read_offset=20, read_length=10, read_id=2),
        ],
    )

    # --- listing and metadata ---------------------------------------------
    # A name containing "/" so `list-objects-delimiter` actually groups
    # something: with no such name, delimiter listing was unmonitored -- it
    # returned the same objects as the plain listing and an empty
    # `prefixes`, so a regression in prefix/delimiter handling would not
    # move the golden at all. Written last (NESTED also sorts last
    # alphabetically, keeping first-sighting order intact).
    call(
        "write-object-nested",
        storage.WriteObject,
        iter(
            [
                storage_pb2.WriteObjectRequest(
                    write_object_spec=write_spec(NESTED),
                    write_offset=0,
                    checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD),
                    finish_write=True,
                ),
            ]
        ),
    )
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
    delimiter_response = storage.ListObjects(
        storage_pb2.ListObjectsRequest(parent=_bucket_path(BUCKET), delimiter="/")
    )
    assert delimiter_response.prefixes, "list-objects-delimiter produced no prefixes"
    rec.record_grpc("list-objects-delimiter", delimiter_response)

    # UpdateObject had zero coverage before this. A dedicated object, rather
    # than one of the objects read above, so the metageneration bump and the
    # new metadata do not perturb any earlier interaction's expected state.
    # Named "06-" so it sorts after every existing object: its generation is
    # therefore the newest at first sighting, keeping the generation-
    # monotonicity invariant intact (see the naming comment at the top). No
    # later interaction lists BUCKET, so the extra object stays invisible to
    # the existing listings. `content_type` and `metadata` are the mutable
    # fields UpdateObject's immutable-field guard permits; the response echoes
    # the applied change, so no follow-up read is needed to observe it.
    call(
        "write-update-target",
        storage.WriteObject,
        iter(
            [
                storage_pb2.WriteObjectRequest(
                    write_object_spec=write_spec("06-update.txt"),
                    write_offset=0,
                    checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD),
                    finish_write=True,
                ),
            ]
        ),
    )
    call(
        "update-object",
        storage.UpdateObject,
        storage_pb2.UpdateObjectRequest(
            object=storage_pb2.Object(
                name="06-update.txt",
                bucket=_bucket_path(BUCKET),
                content_type="text/plain",
                metadata={"reviewed": "yes"},
            ),
            update_mask=field_mask_pb2.FieldMask(paths=["content_type", "metadata"]),
        ),
    )

    # --- redirect faults (gRPC-only; see README and trace_faults.py) -------
    # The Retry Test API's instruction grammar requires a lowercase token
    # (`redirect-send-token-([a-z\-]+)$` and friends, testbench/common.py);
    # the brief's literal "-T" spelling 400s at retry-test creation time --
    # confirmed empirically -- so "t" is used here instead. Verified against
    # a live emulator:
    # - `redirect-send-token-t` and `redirect-send-handle-and-token-t` both
    #   abort `BidiWriteObject` (wired in `gcs/upload.py`'s bidi-write path
    #   via `abort_with_redirect_error`). README documents
    #   `redirect-send-handle-and-token-t` for either
    #   `storage.objects.insert` or `storage.objects.get`; it is exercised
    #   here on `BidiReadObject` instead (also wired, in
    #   `grpc_server.py`'s `BidiReadObject`), spreading coverage across both
    #   RPCs rather than duplicating the write case.
    # - `redirect-expect-token-t` does not abort anything: `BidiWriteObject`
    #   silently *consumes* (dequeues) the instruction once the client's own
    #   `x-goog-request-params: routing_token=t` metadata matches, with no
    #   visible signal in the RPC response either way. What proves the fault
    #   fired is therefore the retry-test resource's own state afterwards
    #   (its instruction list becomes empty), not the RPC result.
    def create_grpc_retry_test(label, instruction, method):
        response = session.post(
            base + "/retry_test",
            json={"instructions": {method: [instruction]}, "transport": "GRPC"},
            timeout=30,
        )
        rec.record_http(label, response)
        assert (
            response.status_code == 200
        ), "%s was rejected at retry-test creation: %s" % (instruction, response.text)
        return response.json()["id"]

    write_redirect_id = create_grpc_retry_test(
        "create-retry-test-redirect-send-token",
        "redirect-send-token-t",
        "storage.objects.insert",
    )
    try:
        list(
            storage.BidiWriteObject(
                iter(
                    [
                        storage_pb2.BidiWriteObjectRequest(
                            write_object_spec=write_spec("redirect-write.txt"),
                            write_offset=0,
                            checksummed_data=storage_pb2.ChecksummedData(
                                content=PAYLOAD
                            ),
                            finish_write=True,
                        )
                    ]
                ),
                metadata=[("x-retry-test-id", write_redirect_id)],
            )
        )
        raise AssertionError("redirect-send-token-t did not abort BidiWriteObject")
    except grpc.RpcError as error:
        assert error.code() == grpc.StatusCode.ABORTED, (
            "redirect-send-token-t: expected ABORTED, got %s" % error.code()
        )
        rec.record_error("redirect-send-token", error)

    read_redirect_id = create_grpc_retry_test(
        "create-retry-test-redirect-send-handle-and-token",
        "redirect-send-handle-and-token-t",
        "storage.objects.get",
    )
    try:
        list(
            storage.BidiReadObject(
                iter(
                    [
                        storage_pb2.BidiReadObjectRequest(
                            read_object_spec=storage_pb2.BidiReadObjectSpec(
                                bucket=_bucket_path(BUCKET), object=SINGLE
                            )
                        )
                    ]
                ),
                metadata=[("x-retry-test-id", read_redirect_id)],
            )
        )
        raise AssertionError(
            "redirect-send-handle-and-token-t did not abort BidiReadObject"
        )
    except grpc.RpcError as error:
        assert error.code() == grpc.StatusCode.ABORTED, (
            "redirect-send-handle-and-token-t: expected ABORTED, got %s" % error.code()
        )
        rec.record_error("redirect-send-handle-and-token", error)

    expect_redirect_id = create_grpc_retry_test(
        "create-retry-test-redirect-expect-token",
        "redirect-expect-token-t",
        "storage.objects.insert",
    )
    expect_responses = list(
        storage.BidiWriteObject(
            iter(
                [
                    storage_pb2.BidiWriteObjectRequest(
                        write_object_spec=write_spec("redirect-expect.txt"),
                        write_offset=0,
                        checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD),
                        finish_write=True,
                    )
                ]
            ),
            metadata=[
                ("x-retry-test-id", expect_redirect_id),
                ("x-goog-request-params", "routing_token=t"),
            ],
        )
    )
    rec.record_grpc("redirect-expect-token", expect_responses[-1])
    status = session.get(base + "/retry_test/%s" % expect_redirect_id, timeout=30)
    rec.record_http("retry-test-status-redirect-expect-token", status)
    assert status.json()["instructions"]["storage.objects.insert"] == [], (
        "redirect-expect-token-t was not consumed: the routing_token match "
        "was not detected"
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
    # Soft-deleted reads had zero coverage. Between the soft delete and the
    # restore, the object is invisible to an ordinary GetObject/ListObjects but
    # must remain readable with `soft_deleted=True` -- exactly the distinction a
    # FileStore has to preserve across a restart (see the spec's soft/hard/purge
    # notification split). GetObject requires the generation when reading a
    # soft-deleted object; it is the same generation read back above, so this
    # re-reports an already-bound value and adds no ordering risk.
    call(
        "get-object-soft-deleted",
        storage.GetObject,
        storage_pb2.GetObjectRequest(
            bucket=_bucket_path(SOFT_DELETE),
            object="sd.txt",
            generation=generation,
            soft_deleted=True,
        ),
    )
    call(
        "list-objects-soft-deleted",
        storage.ListObjects,
        storage_pb2.ListObjectsRequest(
            parent=_bucket_path(SOFT_DELETE), soft_deleted=True
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
