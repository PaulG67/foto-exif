"""Pixel-level image adjustments (separate from EXIF metadata edits)."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import piexif
from PIL import Image, ImageEnhance, ImageOps

from app.exif_utils import _insert_xmp, read_exif_meta


def _clamp_factor(value: float, lo: float = 0.2, hi: float = 2.5) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(lo, min(hi, v))


def render_adjusted_jpeg(
    path: Path,
    *,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    quality: int = 90,
    max_side: int | None = None,
) -> bytes:
    """Return adjusted JPEG bytes (does not write to disk)."""
    brightness = _clamp_factor(brightness)
    contrast = _clamp_factor(contrast)
    saturation = _clamp_factor(saturation)
    quality = max(60, min(98, int(quality)))

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        if max_side:
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        if abs(brightness - 1.0) > 0.001:
            im = ImageEnhance.Brightness(im).enhance(brightness)
        if abs(contrast - 1.0) > 0.001:
            im = ImageEnhance.Contrast(im).enhance(contrast)
        if abs(saturation - 1.0) > 0.001:
            im = ImageEnhance.Color(im).enhance(saturation)
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


def apply_image_adjustments(
    path: Path,
    *,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    quality: int = 92,
) -> None:
    """
    Permanently adjust pixels in the JPEG file.
    Tries to keep EXIF dates/orientation and XMP tags/description.
    Note: JPEG is re-encoded (not lossless).
    """
    brightness = _clamp_factor(brightness)
    contrast = _clamp_factor(contrast)
    saturation = _clamp_factor(saturation)
    if (
        abs(brightness - 1.0) < 0.001
        and abs(contrast - 1.0) < 0.001
        and abs(saturation - 1.0) < 0.001
    ):
        return

    quality = max(60, min(98, int(quality)))
    original = path.read_bytes()
    if original[:2] != b"\xff\xd8":
        raise ValueError("Kein JPEG")

    meta = read_exif_meta(path)
    exif_bytes = b""
    try:
        exif = piexif.load(original)
        exif.setdefault("0th", {})
        exif["0th"][piexif.ImageIFD.Orientation] = 1
        exif["thumbnail"] = None
        if "1st" in exif:
            exif["1st"] = {}
        exif_bytes = piexif.dump(exif)
    except Exception:
        exif_bytes = b""

    with Image.open(BytesIO(original)) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        if abs(brightness - 1.0) > 0.001:
            im = ImageEnhance.Brightness(im).enhance(brightness)
        if abs(contrast - 1.0) > 0.001:
            im = ImageEnhance.Contrast(im).enhance(contrast)
        if abs(saturation - 1.0) > 0.001:
            im = ImageEnhance.Color(im).enhance(saturation)
        buf = BytesIO()
        save_kw = {"format": "JPEG", "quality": quality, "optimize": True}
        if exif_bytes:
            save_kw["exif"] = exif_bytes
        im.save(buf, **save_kw)
        out = buf.getvalue()

    tags = meta.get("tags") or []
    description = meta.get("description")
    if tags or description:
        out = _insert_xmp(out, tags, description)

    tmp = path.with_name(path.name + ".adjtmp")
    tmp.write_bytes(out)
    tmp.replace(path)
