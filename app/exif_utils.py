from __future__ import annotations

from pathlib import Path
from typing import Optional

import piexif


def read_exif_dates(path: Path) -> dict[str, Optional[str]]:
    try:
        exif = piexif.load(str(path))
    except Exception:
        return {"original": None, "digitized": None, "modify": None}

    def dec(value) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    return {
        "original": dec(exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)),
        "digitized": dec(exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeDigitized)),
        "modify": dec(exif.get("0th", {}).get(piexif.ImageIFD.DateTime)),
    }


def jpeg_scan_offset(data: bytes) -> Optional[int]:
    i = 0
    while i < len(data) - 1:
        if data[i] == 0xFF and data[i + 1] == 0xDA:
            return i
        i += 1
    return None


def patch_exif_dates(
    path: Path,
    date_str: str,
    *,
    set_original: bool = True,
    set_digitized: bool = True,
    set_modify: bool = True,
) -> None:
    """Write EXIF date tags only; JPEG image payload stays byte-identical."""
    raw = path.read_bytes()
    if raw[:2] != b"\xff\xd8":
        raise ValueError("Kein JPEG")

    before_sos = jpeg_scan_offset(raw)
    if before_sos is None:
        raise ValueError("Ungültiges JPEG (kein SOS)")
    before_payload = raw[before_sos:]

    try:
        exif = piexif.load(raw)
    except Exception:
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}, "thumbnail": None}

    exif.setdefault("0th", {})
    exif.setdefault("Exif", {})
    encoded = date_str.encode("utf-8")

    if set_original:
        exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = encoded
    if set_digitized:
        exif["Exif"][piexif.ExifIFD.DateTimeDigitized] = encoded
    if set_modify:
        exif["0th"][piexif.ImageIFD.DateTime] = encoded

    for tag in (
        piexif.ExifIFD.SubSecTimeOriginal,
        piexif.ExifIFD.SubSecTimeDigitized,
        piexif.ExifIFD.SubSecTime,
    ):
        exif["Exif"].pop(tag, None)

    out = piexif.insert(piexif.dump(exif), raw)
    after_sos = jpeg_scan_offset(out)
    if after_sos is None or out[after_sos:] != before_payload:
        raise RuntimeError("Abbruch: Bilddaten hätten sich geändert")

    tmp = path.with_suffix(path.suffix + ".exiftmp")
    tmp.write_bytes(out)
    tmp.replace(path)
