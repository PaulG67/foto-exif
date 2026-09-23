"""Pixel-level image adjustments (separate from EXIF metadata edits)."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any

import piexif
from PIL import Image, ImageEnhance, ImageOps

from app.exif_utils import _insert_xmp, read_exif_meta

DEFAULT_SAVE_QUALITY = 92
DEFAULT_PIXEL_STRENGTH = 0.045


def _clamp_factor(value: float, lo: float = 0.2, hi: float = 2.5) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(lo, min(hi, v))


def _clamp_strength(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_PIXEL_STRENGTH
    return max(0.015, min(0.18, v))


def parse_regions(raw: Any) -> list[tuple[float, float, float, float]]:
    """
    Accept JSON list of {x,y,w,h} or [x,y,w,h], or compact string
    'x,y,w,h;x,y,w,h' with normalized 0..1 coordinates.
    """
    if raw is None or raw == "":
        return []
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return []
        else:
            data = []
            for part in text.split(";"):
                part = part.strip()
                if not part:
                    continue
                nums = [p.strip() for p in part.split(",")]
                if len(nums) >= 4:
                    data.append(nums[:4])

    if not isinstance(data, list):
        return []

    out: list[tuple[float, float, float, float]] = []
    for item in data[:20]:
        try:
            if isinstance(item, dict):
                x = float(item.get("x", 0))
                y = float(item.get("y", 0))
                w = float(item.get("w", 0))
                h = float(item.get("h", 0))
            elif isinstance(item, (list, tuple)) and len(item) >= 4:
                x, y, w, h = (float(item[0]), float(item[1]), float(item[2]), float(item[3]))
            else:
                continue
        except (TypeError, ValueError):
            continue

        if w < 0:
            x, w = x + w, -w
        if h < 0:
            y, h = y + h, -h
        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))
        w = max(0.0, min(1.0 - x, w))
        h = max(0.0, min(1.0 - y, h))
        if w < 0.005 or h < 0.005:
            continue
        out.append((x, y, w, h))
    return out


def encode_regions(regions: list[tuple[float, float, float, float]]) -> str:
    return ";".join(f"{x:.5f},{y:.5f},{w:.5f},{h:.5f}" for x, y, w, h in regions)


def _is_neutral(
    brightness: float,
    contrast: float,
    saturation: float,
    regions: list[tuple[float, float, float, float]] | None = None,
) -> bool:
    if regions:
        return False
    return (
        abs(brightness - 1.0) < 0.001
        and abs(contrast - 1.0) < 0.001
        and abs(saturation - 1.0) < 0.001
    )


def pixelate_regions(
    im: Image.Image,
    regions: list[tuple[float, float, float, float]],
    strength: float = DEFAULT_PIXEL_STRENGTH,
) -> Image.Image:
    """Pixelate normalized regions in-place (returns same image)."""
    if not regions:
        return im
    strength = _clamp_strength(strength)
    width, height = im.size
    block = max(4, int(min(width, height) * strength))

    for nx, ny, nw, nh in regions:
        x0 = int(round(nx * width))
        y0 = int(round(ny * height))
        x1 = int(round((nx + nw) * width))
        y1 = int(round((ny + nh) * height))
        x0 = max(0, min(width - 1, x0))
        y0 = max(0, min(height - 1, y0))
        x1 = max(x0 + 1, min(width, x1))
        y1 = max(y0 + 1, min(height, y1))
        rw, rh = x1 - x0, y1 - y0
        if rw < 2 or rh < 2:
            continue
        crop = im.crop((x0, y0, x1, y1))
        sw = max(1, (rw + block - 1) // block)
        sh = max(1, (rh + block - 1) // block)
        small = crop.resize((sw, sh), Image.Resampling.BOX)
        pixelated = small.resize((rw, rh), Image.Resampling.NEAREST)
        im.paste(pixelated, (x0, y0))
    return im


def _enhance_rgb(
    im: Image.Image,
    *,
    brightness: float,
    contrast: float,
    saturation: float,
) -> Image.Image:
    if abs(brightness - 1.0) > 0.001:
        im = ImageEnhance.Brightness(im).enhance(brightness)
    if abs(contrast - 1.0) > 0.001:
        im = ImageEnhance.Contrast(im).enhance(contrast)
    if abs(saturation - 1.0) > 0.001:
        im = ImageEnhance.Color(im).enhance(saturation)
    return im


def render_adjusted_jpeg(
    path: Path,
    *,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    regions: list[tuple[float, float, float, float]] | None = None,
    pixel_strength: float = DEFAULT_PIXEL_STRENGTH,
    quality: int = 90,
    max_side: int | None = None,
) -> bytes:
    """Return adjusted JPEG bytes (does not write to disk)."""
    brightness = _clamp_factor(brightness)
    contrast = _clamp_factor(contrast)
    saturation = _clamp_factor(saturation)
    regions = regions or []
    quality = max(60, min(98, int(quality)))

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        if max_side:
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        im = _enhance_rgb(im, brightness=brightness, contrast=contrast, saturation=saturation)
        pixelate_regions(im, regions, pixel_strength)
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


def build_adjusted_jpeg(
    path: Path,
    *,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    regions: list[tuple[float, float, float, float]] | None = None,
    pixel_strength: float = DEFAULT_PIXEL_STRENGTH,
    quality: int = DEFAULT_SAVE_QUALITY,
) -> bytes | None:
    """
    Build full-resolution adjusted JPEG bytes as they would be saved.
    Returns None if nothing would change.
    """
    brightness = _clamp_factor(brightness)
    contrast = _clamp_factor(contrast)
    saturation = _clamp_factor(saturation)
    regions = regions or []
    if _is_neutral(brightness, contrast, saturation, regions):
        return None

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
        im = _enhance_rgb(im, brightness=brightness, contrast=contrast, saturation=saturation)
        pixelate_regions(im, regions, pixel_strength)
        buf = BytesIO()
        save_kw: dict = {"format": "JPEG", "quality": quality, "optimize": True}
        if exif_bytes:
            save_kw["exif"] = exif_bytes
        im.save(buf, **save_kw)
        out = buf.getvalue()

    tags = meta.get("tags") or []
    description = meta.get("description")
    if tags or description:
        out = _insert_xmp(out, tags, description)
    return out


def estimate_adjusted_size(
    path: Path,
    *,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    regions: list[tuple[float, float, float, float]] | None = None,
    pixel_strength: float = DEFAULT_PIXEL_STRENGTH,
    quality: int = DEFAULT_SAVE_QUALITY,
) -> int:
    """Byte size after apply (or current size if adjustments are neutral)."""
    out = build_adjusted_jpeg(
        path,
        brightness=brightness,
        contrast=contrast,
        saturation=saturation,
        regions=regions,
        pixel_strength=pixel_strength,
        quality=quality,
    )
    if out is None:
        return path.stat().st_size
    return len(out)


def apply_image_adjustments(
    path: Path,
    *,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    regions: list[tuple[float, float, float, float]] | None = None,
    pixel_strength: float = DEFAULT_PIXEL_STRENGTH,
    quality: int = DEFAULT_SAVE_QUALITY,
) -> None:
    """
    Permanently adjust pixels in the JPEG file.
    Tries to keep EXIF dates/orientation and XMP tags/description.
    Note: JPEG is re-encoded (not lossless).
    """
    out = build_adjusted_jpeg(
        path,
        brightness=brightness,
        contrast=contrast,
        saturation=saturation,
        regions=regions,
        pixel_strength=pixel_strength,
        quality=quality,
    )
    if out is None:
        return

    tmp = path.with_name(path.name + ".adjtmp")
    tmp.write_bytes(out)
    tmp.replace(path)
