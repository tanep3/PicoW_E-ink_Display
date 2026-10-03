"""Canonical 1-bit PNG and the V4 wire format."""

from __future__ import annotations

from io import BytesIO
import hashlib
import struct

from PIL import Image, ImageOps

WIDTH, HEIGHT = 250, 122
WIRE_LENGTH = 4000
FORMAT_ID = "epd122x250-msb-white1-v1"
ADAPTER_VERSION = 1


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(source: bytes, threshold: int = 128) -> bytes:
    """Fit without cropping, then produce grayscale, non-interlaced 1-bit PNG."""
    if not 1 <= threshold <= 254:
        raise ValueError("threshold out of range")
    with Image.open(BytesIO(source)) as original:
        original.load()
        canvas = Image.new("L", (WIDTH, HEIGHT), 255)
        fitted = ImageOps.contain(original.convert("L"), (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        canvas.paste(fitted, ((WIDTH - fitted.width) // 2, (HEIGHT - fitted.height) // 2))
        result = canvas.point(lambda n: 255 if n >= threshold else 0, mode="1")
        output = BytesIO()
        result.save(output, format="PNG", optimize=False)
    data = output.getvalue()
    validate_png(data)
    return data


def validate_png(data: bytes) -> Image.Image:
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("invalid PNG header")
    width, height, depth, kind, compression, filtering, interlace = struct.unpack(">IIBBBBB", data[16:29])
    if (width, height, depth, kind, compression, filtering, interlace) != (WIDTH, HEIGHT, 1, 0, 0, 0, 0):
        raise ValueError("PNG profile mismatch")
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()  # Pillow verifies decompression; verify CRC separately below.
            if image.mode != "1" or image.size != (WIDTH, HEIGHT):
                raise ValueError("PNG pixels mismatch")
            copy = image.copy()
    except OSError as exc:
        raise ValueError("PNG decode failed") from exc
    import zlib
    pos = 8
    seen_end = False
    while pos + 12 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        end = pos + 12 + size
        if end > len(data):
            raise ValueError("truncated PNG chunk")
        kind_bytes = data[pos + 4:pos + 8]
        crc = int.from_bytes(data[end - 4:end], "big")
        if zlib.crc32(data[pos + 4:end - 4]) & 0xffffffff != crc:
            raise ValueError("PNG CRC mismatch")
        pos = end
        if kind_bytes == b"IEND":
            seen_end = True
            break
    if not seen_end or pos != len(data):
        raise ValueError("PNG tail mismatch")
    return copy


def png_to_wire(data: bytes) -> bytes:
    image = validate_png(data)
    pixels = image.load()
    wire = bytearray(b"\xff" * WIRE_LENGTH)
    for v in range(250):
        for u in range(122):
            if pixels[v, 121 - u] == 0:
                wire[v * 16 + u // 8] &= ~(0x80 >> (u % 8))
    validate_wire(wire)
    return bytes(wire)


def validate_wire(wire: bytes) -> None:
    if len(wire) != WIRE_LENGTH:
        raise ValueError("RAW length must be 4000")
    if any((wire[v * 16 + 15] & 0x3f) != 0x3f for v in range(250)):
        raise ValueError("RAW padding must be white")


def wire_to_pixels(wire: bytes) -> list[list[int]]:
    """Reverse mapping for tests and archive verification; 0=black, 1=white."""
    validate_wire(wire)
    return [[(wire[x * 16 + (121 - y) // 8] >> (7 - (121 - y) % 8)) & 1
             for x in range(WIDTH)] for y in range(HEIGHT)]
