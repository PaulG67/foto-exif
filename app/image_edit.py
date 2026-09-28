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


def _clamp_perspective(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(-30.0, min(30.0, v))


def _clamp_rotation(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(-15.0, min(15.0, v))


def parse_crop(raw: Any) -> tuple[float, float, float, float] | None:
    regions = parse_regions(raw)
    if not regions:
        return None
    return regions[0]


def _is_neutral(
    brightness: float,
    contrast: float,
    saturation: float,
    regions: list[tuple[float, float, float, float]] | None = None,
    *,
    persp_vertical: float = 0.0,
    persp_horizontal: float = 0.0,
    rotate_deg: float = 0.0,
    crop: tuple[float, float, float, float] | None = None,
) -> bool:
    if regions:
        return False
    if crop:
        return False
    return (
        abs(brightness - 1.0) < 0.001
        and abs(contrast - 1.0) < 0.001
        and abs(saturation - 1.0) < 0.001
        and abs(_clamp_perspective(persp_vertical)) < 0.05
        and abs(_clamp_perspective(persp_horizontal)) < 0.05
        and abs(_clamp_rotation(rotate_deg)) < 0.05
    )


def apply_perspective(
    im: Image.Image,
    *,
    vertical: float = 0.0,
    horizontal: float = 0.0,
) -> Image.Image:
    """
    3D-Tilt / Trapez-Korrektur (kein 90°-Drehen).
    vertical (+): obere Kante nach hinten (verengt oben)
    horizontal (+): rechts nach hinten, links nach vorne
    """
    vertical = _clamp_perspective(vertical)
    horizontal = _clamp_perspective(horizontal)
    if abs(vertical) < 0.05 and abs(horizontal) < 0.05:
        return im
    w, h = im.size
    if w < 8 or h < 8:
        return im

    dx = (vertical / 100.0) * w * 0.26
    dy = (horizontal / 100.0) * h * 0.26
    dx = max(-w * 0.34, min(w * 0.34, dx))
    dy = max(-h * 0.34, min(h * 0.34, dy))

    # QUAD: ul, ur, lr, ll → output rectangle (keine Rotation, nur Neigung)
    quad = (dx, 0.0, w - dx, dy, w, h, 0.0, h)
    return im.transform((w, h), Image.Transform.QUAD, quad, Image.Resampling.BICUBIC)


def apply_fine_rotation(im: Image.Image, degrees: float) -> Image.Image:
    """Horizont gerade ziehen — wenige Grad, mit weißem Rand."""
    degrees = _clamp_rotation(degrees)
    if abs(degrees) < 0.05:
        return im
    return im.rotate(
        -degrees,
        resample=Image.Resampling.BICUBIC,
        expand=True,
        fillcolor=(255, 255, 255),
    )


def apply_crop_norm(
    im: Image.Image, crop: tuple[float, float, float, float] | None
) -> Image.Image:
    if not crop:
        return im
    nx, ny, nw, nh = crop
    w, h = im.size
    x0 = int(round(nx * w))
    y0 = int(round(ny * h))
    x1 = int(round((nx + nw) * w))
    y1 = int(round((ny + nh) * h))
    x0 = max(0, min(w - 1, x0))
    y0 = max(0, min(h - 1, y0))
    x1 = max(x0 + 1, min(w, x1))
    y1 = max(y0 + 1, min(h, y1))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return im
    return im.crop((x0, y0, x1, y1))


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


def _process_adjusted_rgb(
    im: Image.Image,
    *,
    brightness: float,
    contrast: float,
    saturation: float,
    regions: list[tuple[float, float, float, float]],
    pixel_strength: float,
    persp_vertical: float,
    persp_horizontal: float,
    rotate_deg: float = 0.0,
    crop: tuple[float, float, float, float] | None = None,
) -> Image.Image:
    im = apply_perspective(im, vertical=persp_vertical, horizontal=persp_horizontal)
    im = apply_fine_rotation(im, rotate_deg)
    im = apply_crop_norm(im, crop)
    im = _enhance_rgb(im, brightness=brightness, contrast=contrast, saturation=saturation)
    pixelate_regions(im, regions, pixel_strength)
    return im


def render_adjusted_jpeg(
    path: Path,
    *,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    regions: list[tuple[float, float, float, float]] | None = None,
    pixel_strength: float = DEFAULT_PIXEL_STRENGTH,
    persp_vertical: float = 0.0,
    persp_horizontal: float = 0.0,
    rotate_deg: float = 0.0,
    crop: tuple[float, float, float, float] | None = None,
    quality: int = 90,
    max_side: int | None = None,
) -> bytes:
    """Return adjusted JPEG bytes (does not write to disk)."""
    brightness = _clamp_factor(brightness)
    contrast = _clamp_factor(contrast)
    saturation = _clamp_factor(saturation)
    regions = regions or []
    persp_vertical = _clamp_perspective(persp_vertical)
    persp_horizontal = _clamp_perspective(persp_horizontal)
    rotate_deg = _clamp_rotation(rotate_deg)
    quality = max(60, min(98, int(quality)))

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        if max_side:
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        im = _process_adjusted_rgb(
            im,
            brightness=brightness,
            contrast=contrast,
            saturation=saturation,
            regions=regions,
            pixel_strength=pixel_strength,
            persp_vertical=persp_vertical,
            persp_horizontal=persp_horizontal,
            rotate_deg=rotate_deg,
            crop=crop,
        )
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
    persp_vertical: float = 0.0,
    persp_horizontal: float = 0.0,
    rotate_deg: float = 0.0,
    crop: tuple[float, float, float, float] | None = None,
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
    persp_vertical = _clamp_perspective(persp_vertical)
    persp_horizontal = _clamp_perspective(persp_horizontal)
    rotate_deg = _clamp_rotation(rotate_deg)
    if _is_neutral(
        brightness,
        contrast,
        saturation,
        regions,
        persp_vertical=persp_vertical,
        persp_horizontal=persp_horizontal,
        rotate_deg=rotate_deg,
        crop=crop,
    ):
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
        im = _process_adjusted_rgb(
            im,
            brightness=brightness,
            contrast=contrast,
            saturation=saturation,
            regions=regions,
            pixel_strength=pixel_strength,
            persp_vertical=persp_vertical,
            persp_horizontal=persp_horizontal,
            rotate_deg=rotate_deg,
            crop=crop,
        )
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
    persp_vertical: float = 0.0,
    persp_horizontal: float = 0.0,
    rotate_deg: float = 0.0,
    crop: tuple[float, float, float, float] | None = None,
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
        persp_vertical=persp_vertical,
        persp_horizontal=persp_horizontal,
        rotate_deg=rotate_deg,
        crop=crop,
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
    persp_vertical: float = 0.0,
    persp_horizontal: float = 0.0,
    rotate_deg: float = 0.0,
    crop: tuple[float, float, float, float] | None = None,
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
        persp_vertical=persp_vertical,
        persp_horizontal=persp_horizontal,
        rotate_deg=rotate_deg,
        crop=crop,
        quality=quality,
    )
    if out is None:
        return

    tmp = path.with_name(path.name + ".adjtmp")
    tmp.write_bytes(out)
    tmp.replace(path)
