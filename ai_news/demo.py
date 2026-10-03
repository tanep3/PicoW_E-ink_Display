"""Clearly labelled gallery-only test patterns; never publish to latest."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
import os
from pathlib import Path

from PIL import Image, ImageDraw

from .archive import atomic_write, json_bytes
from .frame import sha256, validate_png
from .gallery import JST


def create_demo(root: Path, now=None) -> list[Path]:
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    local_day = now.astimezone(JST).date()
    created = []
    for index, (day, hour, title) in enumerate((
        (local_day - timedelta(days=1), 10, "検証用パターン A（前日）"),
        (local_day, 10, "検証用パターン B（午前）"),
        (local_day, 15, "検証用パターン C（午後）"),
    )):
        image = Image.new("1", (250, 122), 1)
        draw = ImageDraw.Draw(image)
        draw.rectangle((3, 3, 246, 118), outline=0, width=2)
        if index == 0:
            draw.line((14, 100, 120, 20, 230, 100), fill=0, width=4)
        elif index == 1:
            draw.ellipse((70, 17, 180, 107), outline=0, width=5)
            draw.line((20, 61, 230, 61), fill=0, width=3)
        else:
            for x in range(20, 231, 28):
                draw.rectangle((x, 20, x + 12, 98), fill=0)
        draw.text((12, 8), "TEST %s" % "ABC"[index], fill=0)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        png = buffer.getvalue()
        validate_png(png)
        frame_id = "demo-" + sha256(png)[:16] + "-" + str(index)
        directory = root / "demo" / frame_id
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime(day.year, day.month, day.day, hour, tzinfo=JST).astimezone(timezone.utc)
        record = {"frame_id": frame_id, "created_at": timestamp.isoformat(),
                  "metadata": {"title": title, "source_urls": [], "demo": True}}
        image_path, record_path = directory / "frame.png", directory / "frame.json"
        if not image_path.exists():
            atomic_write(image_path, png)
            atomic_write(record_path, json_bytes(record))
        elif image_path.read_bytes() != png:
            raise ValueError("demo image collision")
        created.append(image_path)
    return created


def main():
    root = Path(os.environ.get("AI_NEWS_STATE", "./state"))
    print("Created/verified", len(create_demo(root)), "gallery-only demo patterns")


if __name__ == "__main__":
    main()
