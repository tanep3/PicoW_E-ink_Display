"""MicroPython-compatible manifest and RAW integrity checks for LAN pull."""

try:
    import uhashlib as hashlib
except ImportError:
    import hashlib
try:
    import ujson as json
except ImportError:
    import json
try:
    import ubinascii as binascii
except ImportError:
    import binascii

FORMAT_ID = "epd122x250-msb-white1-v1"
WIRE_LENGTH = 4000


def hex_digest(data):
    return binascii.hexlify(hashlib.sha256(data).digest()).decode()


def validate_response(status, headers, body, expected_type):
    if headers.get("content-type") != expected_type or headers.get("content-length") != str(len(body)):
        raise ValueError("response type or length mismatch")


def _hex(value, length):
    return isinstance(value, str) and len(value) == length and all(c in "0123456789abcdef" for c in value)


def validate_manifest(data):
    if len(data) > 2048:
        raise ValueError("manifest too large")
    value = json.loads(data)
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("manifest schema mismatch")
    if value.get("format_id") != FORMAT_ID or type(value.get("length")) is not int or value["length"] != WIRE_LENGTH:
        raise ValueError("manifest format mismatch")
    frame_id = value.get("frame_id")
    if not isinstance(frame_id, str) or len(frame_id) != 67 or not _hex(frame_id[:64], 64) or frame_id[64:] != "-a1":
        raise ValueError("invalid frame ID")
    if not _hex(value.get("png_sha256"), 64) or not _hex(value.get("wire_sha256"), 64):
        raise ValueError("manifest hash invalid")
    path = "/v1/frames/" + frame_id + ".raw"
    if value.get("raw_path") != path:
        raise ValueError("unsafe RAW path")
    if type(value.get("publish_seq")) is not int or value["publish_seq"] < 1:
        raise ValueError("publish sequence invalid")
    return value


def validate_wire(wire, expected_hash):
    if len(wire) != WIRE_LENGTH or hex_digest(wire) != expected_hash:
        raise ValueError("RAW length or hash mismatch")
    for row in range(250):
        if wire[row * 16 + 15] & 0x3f != 0x3f:
            raise ValueError("RAW padding invalid")


def landscape_buffer(wire):
    """B[i+(15-j)*250]=W[i*16+j] for the reference V4 display order."""
    if len(wire) != WIRE_LENGTH:
        raise ValueError("RAW length invalid")
    result = bytearray(WIRE_LENGTH)
    for i in range(250):
        for j in range(16):
            result[i + (15 - j) * 250] = wire[i * 16 + j]
    return result
