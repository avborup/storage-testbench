"""On-disk sidecar format: the proto as JSON in a small versioned envelope
carrying the true (unescaped) name. Round-trips exactly; no second data model
(spec On-disk-layout rule 3). Writes go through containment so they inherit the
O_NOFOLLOW backstop."""

import json

from google.protobuf import json_format

from google.storage.v2 import storage_pb2
from testbench import containment

SCHEMA_VERSION = 1


def dump(proto, true_name):
    kind = type(proto).__name__  # "Object" or "Bucket"
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": kind,
            "name": true_name,
            "proto": json.loads(json_format.MessageToJson(proto)),
        },
        sort_keys=True,
    )


def load(text):
    try:
        env = json.loads(text)
        kind = env["kind"]
        proto = {"Object": storage_pb2.Object, "Bucket": storage_pb2.Bucket}[kind]()
        json_format.ParseDict(env["proto"], proto)
        return kind, env["name"], proto
    except (ValueError, KeyError, json_format.ParseError) as exc:
        raise ValueError("corrupt sidecar: %s" % exc)


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return load(handle.read())


def write_atomic(dir_fd, filename, text):
    containment.write_bytes_atomic(dir_fd, filename, text.encode("utf-8"))
