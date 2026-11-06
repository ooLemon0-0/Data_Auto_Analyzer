"""Reproduce the OCR site's review overlay on the unmodified full-size image."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from app.core.cancellation import check_cancelled
from app.core.progress import report_progress


REVISION = "phase2_review_v1"
COLOR = "#2CB6B3"  # OcrRect in the site's review dialog.


def parse_box(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        box = [float(v) for v in value]
    except (ValueError, TypeError):
        return None
    if not all(math.isfinite(v) for v in box) or box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box


def render_review(original: Path, destination: Path, metadata: dict, label: str):
    report_progress("rendering", "正在生成端面框复核图")
    from PIL import Image, ImageDraw, ImageFont
    with Image.open(original) as opened:
        # Coordinates refer to original pixel axes, before any display rotation.
        image = opened.convert("RGB")
    image.info.pop("exif", None)
    width, height = image.size
    box = parse_box(metadata.get("review_box")) if metadata.get("original_image_url") else None
    if box:
        box = [max(0, min(width - 1 if i % 2 == 0 else height - 1, value)) for i, value in enumerate(box)]
        if box[2] <= box[0] or box[3] <= box[1]:
            box = None
    kind = "boxed" if box else "original" if metadata.get("original_image_url") else "detail"
    if box:
        draw = ImageDraw.Draw(image)
        scale = max(1.0, width / 1000)
        border = max(2, round(2 * scale))
        draw.rectangle(tuple(box), outline=COLOR, width=border)
        if label:
            font_size = max(14, round(14 * scale))
            fonts = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts"
            font = None
            for name in ("msyh.ttc", "segoeui.ttf", "arial.ttf"):
                try:
                    font = ImageFont.truetype(str(fonts / name), font_size)
                    break
                except OSError:
                    pass
            font = font or ImageFont.load_default(size=font_size)
            text_box = draw.textbbox((0, 0), label, font=font)
            pad = max(4, round(4 * scale))
            tag_width = text_box[2] - text_box[0] + pad * 2
            tag_height = max(round(font_size * 1.2), text_box[3] - text_box[1] + 4)
            left = max(0, min(round(box[0]), width - tag_width))
            top = round(box[1]) - tag_height
            if top < 0:
                top = min(height - tag_height, round(box[3]))
            draw.rectangle((left, top, min(width - 1, left + tag_width), top + tag_height), fill=COLOR)
            draw.text((left + pad, top + (tag_height - (text_box[3] - text_box[1])) / 2 - text_box[1]), label, font=font, fill="white")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        image.save(temporary, format="PNG", compress_level=3)
        check_cancelled()
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
        image.close()
    return destination, {"review_image_kind": kind, "review_box": box, "original_cache_path": str(original.resolve())}
