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

"""FileStore: mirror the in-memory index to a GCS-shaped tree. Thin translator
from Store notifications to CONTAINED filesystem ops -- every path component is
opened with O_NOFOLLOW so a planted/swapped symlink cannot escape the bucket
root. All name/path/security logic lives in pathing/containment/sidecar."""

import contextlib
import json
import os

import testbench.common
import testbench.error
from testbench import containment, pathing, sidecar
from testbench.store import Store

_SUBDIRS = ("generations", "soft_deleted", "uploads", "folders", "overflow")


def _unlink_quiet(dir_fd, name):
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _move_quiet(src_fd, src_name, dst_fd, dst_name):
    try:
        os.replace(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
    except FileNotFoundError:
        pass


class FileStore(Store):
    def __init__(self, root):
        containment.assert_posix_support()
        self._root = os.path.realpath(root)
        os.makedirs(self._root, exist_ok=True)

    def _bucket_name(self, proto_name):
        return testbench.common.bucket_name_from_proto(proto_name)

    def _index_names(self):
        return set(os.listdir(self._root))

    @contextlib.contextmanager
    def _bucket_dirfd(self, short, create=False):
        rfd = os.open(self._root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            if create:
                try:
                    os.mkdir(short, 0o755, dir_fd=rfd)
                except FileExistsError:
                    pass
            bfd = containment.open_bucket_root_fd(rfd, short)
        finally:
            os.close(rfd)
        try:
            yield bfd
        finally:
            os.close(bfd)

    @contextlib.contextmanager
    def _leaf_dirfd(self, bfd, parts, create):
        dfd = containment.walk_dirs(bfd, parts, create=create)
        try:
            yield dfd
        finally:
            if dfd != bfd:
                os.close(dfd)

    # --- pre-commit validation (spec rules 2/4; only check for file backend) -
    def validate_bucket_name(self, name, context=None):
        try:
            pathing.validate_bucket_name(self._bucket_name(name))
        except ValueError as exc:
            # Clean 4xx (REST) / INVALID_ARGUMENT (gRPC) BEFORE commit -- context
            # is threaded from Database.insert_bucket so gRPC aborts correctly.
            testbench.error.invalid("Bucket name %s" % exc, context)

    # --- buckets -----------------------------------------------------------
    def bucket_inserted(self, bucket):
        short = self._bucket_name(bucket.metadata.name)
        with self._bucket_dirfd(short, create=True) as bfd:
            gcs_fd = containment.walk_dirs(bfd, [".gcs"], create=True)
            try:
                for sub in _SUBDIRS:
                    try:
                        os.mkdir(sub, 0o755, dir_fd=gcs_fd)
                    except FileExistsError:
                        pass
                sidecar.write_atomic(
                    gcs_fd, "bucket.json", sidecar.dump(bucket.metadata, short)
                )
            finally:
                os.close(gcs_fd)

    def bucket_updated(self, bucket):
        short = self._bucket_name(bucket.metadata.name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, [".gcs"], create=True) as gcs_fd:
                sidecar.write_atomic(
                    gcs_fd, "bucket.json", sidecar.dump(bucket.metadata, short)
                )

    def bucket_deleted(self, bucket_name):
        short = self._bucket_name(bucket_name)
        path = os.path.join(self._root, short)
        containment.constrained_rmtree(path, self._root, self._index_names())

    # --- objects -----------------------------------------------------------
    def _dest_parts(self, object_name):
        """(reldir_parts, base) for the LIVE object; overflow -> .gcs/overflow."""
        kind, target = pathing.classify(object_name)
        if kind == "overflow":
            return [".gcs", "overflow"], target
        parts = target.split("/")
        return parts[:-1], parts[-1]

    def object_inserted(self, bucket_name, blob):
        object_name = blob.metadata.name
        short = self._bucket_name(bucket_name)
        parts, base = self._dest_parts(object_name)
        data = blob.media.to_bytes()  # MEDIA CALL SITE (tests/media_call_sites.txt)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, parts, create=True) as dfd:
                self._guard_collision(dfd, base, object_name)  # write-time
                containment.write_bytes_atomic(dfd, base, data)
                sidecar.write_atomic(
                    dfd, base + ".gcsmeta", sidecar.dump(blob.metadata, object_name)
                )
        # NOTE: no soft-deleted cleanup here. Restore reconciliation is the
        # object_purged signal fired by Database.restore_object (Task 3) --
        # exactly one mechanism, no `generation - 1` magic.

    def _guard_collision(self, dfd, base, object_name):
        """Refuse to clobber a live media whose sidecar carries a DIFFERENT true
        name -- the case-insensitive-FS collapse (Clip.wav vs clip.wav) surfaces
        here as an existing .gcsmeta with a mismatched true_name."""
        try:
            fd = containment.safe_open(dfd, base + ".gcsmeta", os.O_RDONLY)
        except FileNotFoundError:
            return
        with os.fdopen(fd) as handle:
            _, existing_true, _ = sidecar.load(handle.read())
        if existing_true != object_name:
            raise RuntimeError(
                "collision: %r and %r map to one on-disk target"
                % (existing_true, object_name)
            )

    def object_updated(self, bucket_name, blob):
        short = self._bucket_name(bucket_name)
        parts, base = self._dest_parts(blob.metadata.name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, parts, create=False) as dfd:
                sidecar.write_atomic(
                    dfd,
                    base + ".gcsmeta",
                    sidecar.dump(blob.metadata, blob.metadata.name),
                )

    def object_deleted(self, bucket_name, object_name, generation):
        short = self._bucket_name(bucket_name)
        parts, base = self._dest_parts(object_name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, parts, create=False) as dfd:
                for n in (base, base + ".gcsmeta"):
                    _unlink_quiet(dfd, n)

    def object_soft_deleted(self, bucket_name, blob, hard_delete_time):
        short = self._bucket_name(bucket_name)
        parts, base = self._dest_parts(blob.metadata.name)
        gen = str(blob.metadata.generation)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, [".gcs", "soft_deleted"], create=True) as sfd:
                try:
                    os.mkdir(gen, 0o755, dir_fd=sfd)
                except FileExistsError:
                    pass
                dstfd = containment.open_dir_nofollow(sfd, gen)
                try:
                    with self._leaf_dirfd(bfd, parts, create=False) as dfd:
                        _move_quiet(dfd, base, dstfd, "media")
                        sidecar.write_atomic(
                            dstfd,
                            "meta.gcsmeta",
                            sidecar.dump(blob.metadata, blob.metadata.name),
                        )
                        _unlink_quiet(dfd, base + ".gcsmeta")
                finally:
                    os.close(dstfd)

    def object_purged(self, bucket_name, object_name, generation):
        short = self._bucket_name(bucket_name)
        dst = os.path.join(self._root, short, ".gcs", "soft_deleted", str(generation))
        parent = os.path.dirname(dst)
        if os.path.isdir(dst):
            containment.constrained_rmtree(dst, parent, {str(generation)})

    # --- folders (touch only .gcs/folders; never re-enter resource state) ---
    def folder_inserted(self, folder_name, folder):
        self._write_folder(folder_name)

    def folder_deleted(self, folder_name):
        self._remove_folder(folder_name)

    def folder_renamed(self, src, dst, folder):
        self._remove_folder(src)
        self._write_folder(dst)

    def _folder_relname(self, folder_name):
        # A managed-folder name is fully caller-controlled and always
        # slash-bearing (it ends in "/"), so it can never be a single safe
        # filename. Route it through pathing.classify -- exactly as object
        # names are -- which collapses any such name to a flat, caller-byte-free
        # SHA-256 overflow token. The true name survives in the envelope below,
        # so the startup scan can re-derive it. No name/path logic of our own.
        _, target = pathing.classify(folder_name)
        return target

    def _folder_envelope(self, folder_name):
        return json.dumps(
            {
                "schema_version": sidecar.SCHEMA_VERSION,
                "kind": "Folder",
                "name": folder_name,
            },
            sort_keys=True,
        )

    def _write_folder(self, folder_name):
        short, _, _ = folder_name.partition("/")
        relname = self._folder_relname(folder_name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, [".gcs", "folders"], create=True) as ffd:
                containment.write_bytes_atomic(
                    ffd,
                    relname + ".json",
                    self._folder_envelope(folder_name).encode("utf-8"),
                )

    def _remove_folder(self, folder_name):
        short, _, _ = folder_name.partition("/")
        relname = self._folder_relname(folder_name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, [".gcs", "folders"], create=False) as ffd:
                _unlink_quiet(ffd, relname + ".json")

    def cleared(self):
        for name in self._index_names():
            containment.constrained_rmtree(
                os.path.join(self._root, name), self._root, self._index_names()
            )
