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

import gcs
import testbench.common
import testbench.error
from google.storage.control.v2 import storage_control_pb2
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

    @contextlib.contextmanager
    def _uploads_dfd(self, bucket_name):
        short = self._bucket_name(bucket_name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, [".gcs", "uploads"], create=True) as ufd:
                yield ufd

    def new_upload_media(self, bucket_name, upload_id):
        """Stage an Upload's bytes in a real O_APPEND file under
        <bucket>/.gcs/uploads/<upload_id>. FileMedia.new_staging dup's the dir_fd,
        so the staging fds outlive this `with` context (released by
        finalize/seal/close). bucket_name is proto-form."""
        from testbench.filemedia import FileMedia

        with self._uploads_dfd(bucket_name) as ufd:
            return FileMedia.new_staging(ufd, upload_id)

    def new_staging_media(self, bucket_name, token):
        """Compose/rewrite/move destinations stage under the same uploads dir,
        keyed by a caller-supplied token."""
        return self.new_upload_media(bucket_name, token)

    def delete_upload(self, bucket_name, upload_id):
        """Remove an abandoned/cancelled staging file (Task 12 wires this to
        Database.delete_upload / CancelResumableWrite)."""
        with self._uploads_dfd(bucket_name) as ufd:
            _unlink_quiet(ufd, upload_id)

    def object_inserted(self, bucket_name, blob):
        from testbench.filemedia import FileMedia

        object_name = blob.metadata.name
        short = self._bucket_name(bucket_name)
        parts, base = self._dest_parts(object_name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, parts, create=True) as dfd:
                self._guard_collision(dfd, base, object_name)  # write-time
                if isinstance(blob.media, FileMedia):
                    # blob.upload is set iff this is an in-progress (unfinalized)
                    # appendable insert (see upload.py _insert_empty_appendable_object,
                    # which passes upload=upload). Those keep growing, so hardlink
                    # the staging inode into the destination and leave it open so
                    # subsequent appends flow to the shared inode; everything else
                    # is a one-shot O(1) promote. No to_bytes(), no double-write.
                    if getattr(blob, "upload", None) is not None:
                        blob.media.link_into(
                            (dfd, base)
                        )  # MEDIA CALL SITE (appendable)
                    else:
                        blob.media.finalize((dfd, base))  # MEDIA CALL SITE (one-shot)
                else:
                    data = (
                        blob.media.to_bytes()
                    )  # MEDIA CALL SITE (BytesMedia fallback)
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
        from testbench.filemedia import FileMedia

        short = self._bucket_name(bucket_name)
        parts, base = self._dest_parts(blob.metadata.name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, parts, create=False) as dfd:
                # Appendable finalize checkpoint: finalize_blob (upload.py)
                # cleared blob.upload while blob.media is still an unsealed
                # staging FileMedia. Seal it -- close the append fd, unlink the
                # staging NAME (the destination hardlink established at
                # object_inserted's link_into + its inode both survive), freeze
                # md5. Intermediate checkpoints keep blob.upload set and the
                # media bytes are ALREADY live at the destination via the shared
                # inode, so they (and PATCH/ACL updates carrying a BytesMedia or
                # an already-sealed FileMedia) fall through to the sidecar-only
                # write below. Gating on `blob.upload is None` makes seal run
                # exactly once. Trace-UNCOVERED: no appendable upload in the
                # conformance trace, so the dedicated test is the safety net.
                if (
                    isinstance(blob.media, FileMedia)
                    and not blob.media.is_finalized
                    and getattr(blob, "upload", None) is None
                ):
                    blob.media.seal()  # MEDIA CALL SITE (appendable finalize)
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
        # `src` is a full proto resource name; `dst` is only the bare
        # `destination_folder_id` the gRPC RenameFolder forwards (a documented
        # testbench quirk -- the folder is re-keyed under that bare id). A rename
        # never leaves its bucket, so derive the bucket short ONCE from `src` and
        # apply it to both sides; `dst` still supplies the destination true-name
        # for the envelope and its overflow relname.
        short = self._folder_bucket_short(src)
        self._remove_folder(src, short=short)
        self._write_folder(dst, short=short)

    def _folder_relname(self, folder_name):
        # A folder resource name is fully caller-controlled and always
        # slash-bearing (`projects/_/buckets/<bucket>/folders/<id>[/]`), whether
        # or not it ends in "/", so it can never be a single safe filename.
        # Flatten it UNCONDITIONALLY to pathing's caller-byte-free SHA-256 token
        # -- unlike an object name, a folder is stored as one flat envelope, so
        # classify's natural-nested-path branch (which triggers on a
        # non-trailing-slash name) must not apply here. The true name survives in
        # the envelope below, so the startup scan can re-derive it. No name/path
        # logic of our own.
        return pathing.overflow_token(folder_name)

    def _folder_envelope(self, folder_name):
        return json.dumps(
            {
                "schema_version": sidecar.SCHEMA_VERSION,
                "kind": "Folder",
                "name": folder_name,
            },
            sort_keys=True,
        )

    def _folder_bucket_short(self, folder_name):
        # A folder name is the full proto resource name Database keys `_folders`
        # on (`projects/_/buckets/<bucket>/folders/<id>/`). Strip the proto
        # prefix via the same helper `_bucket_name` uses, then take the leading
        # segment -- the bucket short. No name/path logic of our own.
        return self._bucket_name(folder_name).partition("/")[0]

    def _write_folder(self, folder_name, short=None):
        if short is None:
            short = self._folder_bucket_short(folder_name)
        relname = self._folder_relname(folder_name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, [".gcs", "folders"], create=True) as ffd:
                containment.write_bytes_atomic(
                    ffd,
                    relname + ".json",
                    self._folder_envelope(folder_name).encode("utf-8"),
                )

    def _remove_folder(self, folder_name, short=None):
        if short is None:
            short = self._folder_bucket_short(folder_name)
        relname = self._folder_relname(folder_name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, [".gcs", "folders"], create=False) as ffd:
                _unlink_quiet(ffd, relname + ".json")

    # --- startup tree-scan / index hydration -------------------------------
    def rebuild_index(self, database):
        """Walk the on-disk tree and reseed `database`'s in-memory index
        DIRECTLY (never via insert_*), so no notification re-fires. Fails
        LOUDLY on a corrupt sidecar (sidecar.read's ValueError propagates) and
        on a collision -- two distinct true-names resolving to the same on-disk
        inode, the filesystem-truthful identity rule (fires on a
        case-insensitive FS, correctly silent on a case-sensitive one). A media
        file with no sidecar is an invisible orphan."""
        for short in sorted(os.listdir(self._root)):
            bucket_path = os.path.join(self._root, short)
            bucket_json = os.path.join(bucket_path, ".gcs", "bucket.json")
            if not os.path.isdir(bucket_path) or not os.path.isfile(bucket_json):
                continue
            _, _, bucket_proto = sidecar.read(bucket_json)
            proto_name = bucket_proto.name
            database._buckets[proto_name] = self._hydrate_bucket(bucket_proto)
            database._objects[proto_name] = {}
            database._live_generations[proto_name] = {}
            database._soft_deleted_objects[proto_name] = {}
            self._scan_bucket(database, bucket_path, proto_name, bucket_proto)

    def _hydrate_bucket(self, bucket_proto):
        # The sidecar persists only bucket.metadata; rebuild the in-memory
        # gcs.bucket.Bucket around it. The IAM policy is re-derived from the
        # persisted ACLs exactly as Bucket.init does -- it is not persisted, and
        # its etag is a fresh uuid4 per construction, so it can never be
        # byte-identical across a restart regardless.
        iam_policy = gcs.bucket.Bucket._Bucket__init_iam_policy(bucket_proto, None)
        return gcs.bucket.Bucket(bucket_proto, {}, iam_policy)

    def _scan_bucket(self, database, bucket_path, proto_name, bucket_proto):
        # `seen` maps (st_dev, st_ino) of each LIVE media file to its true-name,
        # scoped per bucket. A second true-name landing on an already-seen inode
        # is the collision the case-insensitive collapse produces.
        seen = {}
        meta_suffix = pathing.RESERVED_SUFFIX  # ".gcsmeta"
        for dirpath, _dirnames, filenames in os.walk(bucket_path):
            rel = os.path.relpath(dirpath, bucket_path)
            parts = [] if rel == "." else rel.split(os.sep)
            in_gcs = bool(parts) and parts[0] == ".gcs"
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                if in_gcs:
                    if parts == [".gcs"]:
                        continue  # bucket.json + any future top-level .gcs file
                    sub = parts[1] if len(parts) >= 2 else None
                    if sub == "soft_deleted" and filename == "meta" + meta_suffix:
                        self._hydrate_soft_deleted(
                            database, proto_name, bucket_proto, dirpath, full
                        )
                    elif sub == "overflow" and filename.endswith(meta_suffix):
                        media = os.path.join(dirpath, filename[: -len(meta_suffix)])
                        self._hydrate_live(
                            database, proto_name, bucket_proto, full, media, seen
                        )
                    elif sub == "folders" and filename.endswith(".json"):
                        self._hydrate_folder(database, full)
                    # generations / uploads / tmp: not part of the index
                elif filename.endswith(meta_suffix):
                    media = os.path.join(dirpath, filename[: -len(meta_suffix)])
                    self._hydrate_live(
                        database, proto_name, bucket_proto, full, media, seen
                    )

    def _read_media(self, media_path):
        if not os.path.isfile(media_path):
            return b""
        with open(media_path, "rb") as handle:
            return handle.read()

    def _hydrate_live(
        self, database, proto_name, bucket_proto, sidecar_path, media_path, seen
    ):
        _, _true_name, obj_proto = sidecar.read(sidecar_path)
        name = obj_proto.name
        if os.path.isfile(media_path):
            st = os.stat(media_path)
            ident = (st.st_dev, st.st_ino)
            previous = seen.get(ident)
            if previous is not None and previous != name:
                raise RuntimeError(
                    "collision: %r and %r resolve to the same inode" % (previous, name)
                )
            seen[ident] = name
        blob = gcs.object.Object(obj_proto, self._read_media(media_path), bucket_proto)
        gen = obj_proto.generation
        database._objects[proto_name]["%s#%d" % (name, gen)] = blob
        database._live_generations[proto_name][name] = gen

    def _hydrate_soft_deleted(
        self, database, proto_name, bucket_proto, dirpath, sidecar_path
    ):
        _, _true_name, obj_proto = sidecar.read(sidecar_path)
        media = os.path.join(dirpath, "media")
        blob = gcs.object.Object(obj_proto, self._read_media(media), bucket_proto)
        database._soft_deleted_objects[proto_name].setdefault(
            obj_proto.name, []
        ).append(blob)

    def _hydrate_folder(self, database, envelope_path):
        with open(envelope_path, "r", encoding="utf-8") as handle:
            env = json.load(handle)
        name = env["name"]
        database._folders[name] = storage_control_pb2.Folder(
            name=name, metageneration=1
        )

    def cleared(self):
        for name in self._index_names():
            containment.constrained_rmtree(
                os.path.join(self._root, name), self._root, self._index_names()
            )
