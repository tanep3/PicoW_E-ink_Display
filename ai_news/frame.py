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


def require_dpid_backend():
    """Fail before a generation job spends time creating an unusable image."""
    try:
        import numpy as np
        from pepedpid import dpid_resize
    except ImportError as exc:
        raise RuntimeError("DPID requires installed pepedpid and numpy") from exc
    return np, dpid_resize


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(source: bytes, threshold: int = 128, *, method: str = "lanczos",
              dpid_lambda: float | None = None) -> bytes:
    """Fit without cropping, then produce grayscale, non-interlaced 1-bit PNG."""
    if type(threshold) is not int or not 1 <= threshold <= 254:
        raise ValueError("threshold out of range")
    if method not in ("lanczos", "dpid"):
        raise ValueError("unknown resize method")
    with Image.open(BytesIO(source)) as original:
        original.load()
        canvas = Image.new("L", (WIDTH, HEIGHT), 255)
        gray = original.convert("L")
        if method == "lanczos":
            fitted = ImageOps.contain(gray, (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        else:
            from .retry_config import validate_dpid_lambda
            value = validate_dpid_lambda(dpid_lambda)
            np, dpid_resize = require_dpid_backend()
            if gray.width * HEIGHT > gray.height * WIDTH:
                size = (WIDTH, max(1, round(gray.height * WIDTH / gray.width)))
            elif gray.width * HEIGHT < gray.height * WIDTH:
                size = (max(1, round(gray.width * HEIGHT / gray.height)), HEIGHT)
            else:
                size = (WIDTH, HEIGHT)
            # The 0.1.2 wheel panics on H×W×1; repeat the same L pixels into RGB.
            luminance = np.asarray(gray, dtype=np.float32) / np.float32(255.0)
            pixels = np.ascontiguousarray(np.repeat(luminance[:, :, None], 3, axis=2))
            reduced = dpid_resize(pixels, size[1], size[0], value)
            if (reduced.shape != (size[1], size[0], 3) or not np.isfinite(reduced).all()
                    or not np.array_equal(reduced[:, :, 0], reduced[:, :, 1])
                    or not np.array_equal(reduced[:, :, 0], reduced[:, :, 2])):
                raise RuntimeError("DPID returned invalid grayscale pixels")
            output = np.rint(np.clip(reduced[:, :, 0], 0, 1) * 255).astype(np.uint8)
            fitted = Image.fromarray(output, "L")
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
