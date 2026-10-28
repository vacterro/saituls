"""Minimal CTAP2 canonical CBOR for SAITULS Secure Apps.

CTAP2 authenticators parse requests strictly: map keys must appear in the
CTAP2 canonical order, lengths must use the shortest encoding and only
definite-length items are allowed. This module implements exactly that subset
and nothing else -- no tags on output, no floats on output, no indefinite
lengths in either direction.

Canonical key order (CTAP 2.1, section 8 "Message Encoding"):

1. a key with a lower major type sorts first;
2. otherwise the key with the shorter encoding sorts first;
3. otherwise byte-wise lexical order of the encodings.
"""
import struct

MAX_DEPTH = 16


class CborError(ValueError):
    """The input is not CBOR this module accepts."""


def _head(major, value):
    if value < 0:
        raise CborError("negative length or value in CBOR head")
    if value < 24:
        return bytes([(major << 5) | value])
    if value < 0x100:
        return bytes([(major << 5) | 24, value])
    if value < 0x10000:
        return bytes([(major << 5) | 25]) + struct.pack(">H", value)
    if value < 0x100000000:
        return bytes([(major << 5) | 26]) + struct.pack(">I", value)
    if value < 0x10000000000000000:
        return bytes([(major << 5) | 27]) + struct.pack(">Q", value)
    raise CborError("integer does not fit in a CBOR head")


def _sort_key(encoded_key):
    return (encoded_key[0] >> 5, len(encoded_key), encoded_key)


def encode(value, _depth=0):
    """Encode *value* as canonical CBOR bytes."""
    if _depth > MAX_DEPTH:
        raise CborError("CBOR nesting too deep")
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if value is None:
        return b"\xf6"
    if isinstance(value, int):
        if value >= 0:
            return _head(0, value)
        return _head(1, -1 - value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return _head(2, len(raw)) + raw
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _head(3, len(raw)) + raw
    if isinstance(value, (list, tuple)):
        return _head(4, len(value)) + b"".join(encode(v, _depth + 1) for v in value)
    if isinstance(value, dict):
        items = [(encode(k, _depth + 1), encode(v, _depth + 1)) for k, v in value.items()]
        items.sort(key=lambda kv: _sort_key(kv[0]))
        for index in range(1, len(items)):
            if items[index][0] == items[index - 1][0]:
                raise CborError("duplicate map key")
        return _head(5, len(items)) + b"".join(k + v for k, v in items)
    raise CborError("cannot CBOR-encode %s" % type(value).__name__)


def _read_head(data, offset):
    if offset >= len(data):
        raise CborError("truncated CBOR item")
    initial = data[offset]
    major = initial >> 5
    info = initial & 0x1F
    offset += 1
    if info < 24:
        return major, info, info, offset
    sizes = {24: 1, 25: 2, 26: 4, 27: 8}
    if info not in sizes:
        # 28..30 are reserved; 31 is indefinite length, which CTAP forbids.
        raise CborError("unsupported CBOR additional information %d" % info)
    width = sizes[info]
    if offset + width > len(data):
        raise CborError("truncated CBOR head")
    value = int.from_bytes(data[offset:offset + width], "big")
    return major, info, value, offset + width


def decode_from(data, offset=0, _depth=0):
    """Decode one item starting at *offset*. Returns ``(value, next_offset)``."""
    if _depth > MAX_DEPTH:
        raise CborError("CBOR nesting too deep")
    data = bytes(data) if not isinstance(data, bytes) else data
    major, info, value, offset = _read_head(data, offset)
    if major == 0:
        return value, offset
    if major == 1:
        return -1 - value, offset
    if major in (2, 3):
        end = offset + value
        if end > len(data):
            raise CborError("truncated CBOR string")
        raw = data[offset:end]
        if major == 2:
            return raw, end
        try:
            return raw.decode("utf-8"), end
        except UnicodeDecodeError:
            raise CborError("invalid UTF-8 in CBOR text string")
    if major == 4:
        out = []
        for _ in range(value):
            item, offset = decode_from(data, offset, _depth + 1)
            out.append(item)
        return out, offset
    if major == 5:
        out = {}
        for _ in range(value):
            key, offset = decode_from(data, offset, _depth + 1)
            if isinstance(key, (list, dict)):
                raise CborError("unhashable CBOR map key")
            item, offset = decode_from(data, offset, _depth + 1)
            if key in out:
                raise CborError("duplicate CBOR map key")
            out[key] = item
        return out, offset
    if major == 6:
        # Tags carry no meaning CTAP relies on; return the tagged item.
        return decode_from(data, offset, _depth + 1)
    # major 7: simple values and floats
    if info == 20:
        return False, offset
    if info == 21:
        return True, offset
    if info in (22, 23):
        return None, offset
    if info == 25:
        return _half_float(value), offset
    if info == 26:
        return struct.unpack(">f", value.to_bytes(4, "big"))[0], offset
    if info == 27:
        return struct.unpack(">d", value.to_bytes(8, "big"))[0], offset
    raise CborError("unsupported CBOR simple value %d" % info)


def _half_float(value):
    sign = -1.0 if value & 0x8000 else 1.0
    exponent = (value >> 10) & 0x1F
    fraction = value & 0x3FF
    if exponent == 0:
        return sign * fraction * 2.0 ** -24
    if exponent == 0x1F:
        return sign * float("inf") if fraction == 0 else float("nan")
    return sign * (1 + fraction * 2.0 ** -10) * 2.0 ** (exponent - 15)


def decode(data):
    """Decode exactly one item; trailing bytes are an error."""
    value, offset = decode_from(data, 0)
    if offset != len(data):
        raise CborError("%d trailing byte(s) after CBOR item" % (len(data) - offset))
    return value
