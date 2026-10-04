"""MicroPython-compatible RAW4000 integrity and V4 byte ordering for PUSH."""

try:
    import uhashlib as hashlib
except ImportError:
    import hashlib
try:
    import ubinascii as binascii
except ImportError:
    import binascii

FORMAT_ID = "epd122x250-msb-white1-v1"
WIRE_LENGTH = 4000


def hex_digest(data):
    return binascii.hexlify(hashlib.sha256(data).digest()).decode()


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
