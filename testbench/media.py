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

"""Media abstraction between the emulator and object/upload bytes.

`BytesMedia` is behaviourally identical to the raw `bytes` the emulator used
before this seam: it is what `NullStore` uses and what keeps phase 3 a pure
refactor. `FileMedia` (Plan 3) will back the same interface with a real file.

The bytes-compatibility surface (`__len__`, `__getitem__`, `__add__`,
`__radd__`, `__iadd__`, `__eq__`) exists so the ~71 existing `.media` call
sites that only slice, measure, concatenate, or compare keep working
unchanged; the streaming methods (`chunks`, `reader`, incremental
`crc32c`/`md5`) are what the size-sensitive paths migrate onto so `FileMedia`
can later avoid materialising multi-GB buffers.
"""

import hashlib
import io

import crc32c


class Media:
    """Shared base for the two media backends. Exists only so the construction
    choke-points (gcs/object.py) can widen their isinstance guard without gcs/
    importing any file-backend code. BytesMedia is the memory impl; FileMedia
    (this plan) is the FILE-backend impl. Defines no behaviour: the interface is
    the duck surface (__len__/__getitem__/append/chunks/reader/crc32c/md5/
    finalize/to_bytes) both subclasses implement."""


class BytesMedia(Media):
    def __init__(self, initial=b""):
        self._buf = bytearray(initial)
        # Rolling checksums. Maintained incrementally so crc32c()/md5() are O(1)
        # and the O(n^2) whole-buffer recompute at gcs/upload.py:591 becomes O(n)
        # total. crc32c(data, seed) chaining was pinned in Plan 1
        # (tests/test_crc32c_assumptions.py); hashlib is natively incremental.
        self._crc = crc32c.crc32c(bytes(self._buf))
        self._md5 = hashlib.md5(bytes(self._buf))

    def __len__(self):
        return len(self._buf)

    def __getitem__(self, key):
        # A slice returns bytes (not bytearray) so callers see exactly what raw
        # `bytes[...]` gave them; an int index returns an int, as bytes does.
        result = self._buf[key]
        return bytes(result) if isinstance(key, slice) else result

    def append(self, data):
        self._buf.extend(data)
        self._crc = crc32c.crc32c(data, self._crc)
        self._md5.update(data)

    def __iadd__(self, data):
        self.append(data)
        return self

    def __add__(self, data):
        return bytes(self._buf) + data

    def __radd__(self, data):
        return data + bytes(self._buf)

    def __eq__(self, other):
        # `blob.media == b"..."` is a normal assertion shape in the test
        # suite (and was always true of the raw bytes this class replaces),
        # so equality is part of the bytes-compatibility surface, not an
        # afterthought. A `BytesMedia` is mutable, so leaving `__hash__`
        # unset here (Python sets it to None once `__eq__` is defined) is
        # deliberate: unlike `bytes`, this type must not be used as a dict
        # key or set member whose identity could shift under `append`.
        if isinstance(other, BytesMedia):
            return bytes(self._buf) == bytes(other._buf)
        if isinstance(other, (bytes, bytearray)):
            return bytes(self._buf) == other
        return NotImplemented

    def chunks(self, begin, end, size):
        pos = begin
        while pos < end:
            stop = min(pos + size, end)
            yield bytes(self._buf[pos:stop])
            pos = stop

    def crc32c(self):
        return self._crc

    def md5(self):
        return self._md5.digest()

    def reader(self):
        return io.BytesIO(bytes(self._buf))

    def finalize(self, dest):
        # BytesMedia has nothing to promote; FileMedia (Plan 3) overrides this to
        # os.replace a staging file into its final path.
        return None

    def to_bytes(self):
        return bytes(self._buf)
