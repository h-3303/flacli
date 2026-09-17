# SPDX-License-Identifier: GPL-3.0-or-later
"""A minimal BSON codec: the subset serde's `bson` crate emits for Flaclify / Euphonica metadata documents.

Types handled: double, string, document, array, bool, null, int32, int64, UTC datetime. Anything else raises,
so a blob we do not fully understand is never rewritten with parts silently dropped.
"""

import struct

from datetime import datetime, timezone


class BsonError(ValueError):
    pass


def _encode_value(value) -> tuple[int, bytes]:
    if value is None:
        return 0x0A, b""
    if isinstance(value, bool):
        return 0x08, b"\x01" if value else b"\x00"
    if isinstance(value, int):
        if -2**31 <= value < 2**31:
            return 0x10, struct.pack("<i", value)

        return 0x12, struct.pack("<q", value)
    if isinstance(value, float):
        return 0x01, struct.pack("<d", value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return 0x02, struct.pack("<i", len(raw) + 1) + raw + b"\x00"
    if isinstance(value, datetime):
        millis = int(value.timestamp() * 1000)
        return 0x09, struct.pack("<q", millis)
    if isinstance(value, dict):
        return 0x03, encode(value)
    if isinstance(value, (list, tuple)):
        return 0x04, encode({str(i): v for i, v in enumerate(value)})

    raise BsonError(f"cannot encode {type(value).__name__}")


def encode(document: dict) -> bytes:
    body = bytearray()

    for key, value in document.items():
        kind, payload = _encode_value(value)
        body.append(kind)
        body += str(key).encode("utf-8") + b"\x00"
        body += payload

    return struct.pack("<i", len(body) + 5) + bytes(body) + b"\x00"


def _read_cstring(data: bytes, pos: int) -> tuple[str, int]:
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8"), end + 1


def _decode_document(data: bytes, pos: int) -> tuple[dict, int]:
    if len(data) < pos + 5:
        raise BsonError("truncated document")

    (size,) = struct.unpack_from("<i", data, pos)
    end = pos + size

    if size < 5 or end > len(data):
        raise BsonError("document size does not fit the data")

    pos += 4
    result = {}

    while pos < end - 1:
        kind = data[pos]
        pos += 1
        key, pos = _read_cstring(data, pos)

        if kind == 0x0A:
            value = None
        elif kind == 0x08:
            value = data[pos] != 0
            pos += 1
        elif kind == 0x10:
            (value,) = struct.unpack_from("<i", data, pos)
            pos += 4
        elif kind == 0x12:
            (value,) = struct.unpack_from("<q", data, pos)
            pos += 8
        elif kind == 0x01:
            (value,) = struct.unpack_from("<d", data, pos)
            pos += 8
        elif kind == 0x09:
            (millis,) = struct.unpack_from("<q", data, pos)
            value = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
            pos += 8
        elif kind == 0x02:
            (length,) = struct.unpack_from("<i", data, pos)
            value = data[pos + 4:pos + 4 + length - 1].decode("utf-8")
            pos += 4 + length
        elif kind == 0x03:
            value, pos = _decode_document(data, pos)
        elif kind == 0x04:
            items, pos = _decode_document(data, pos)
            value = [items[k] for k in sorted(items, key=int)]
        else:
            raise BsonError(f"unsupported BSON type 0x{kind:02x} at key {key!r}")

        result[key] = value

    if data[end - 1] != 0:
        raise BsonError("document not terminated")

    return result, end


def decode(data: bytes) -> dict:
    document, end = _decode_document(bytes(data), 0)

    if end != len(data):
        raise BsonError("trailing bytes after document")

    return document
