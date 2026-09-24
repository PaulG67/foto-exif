from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import piexif

DEFAULT_SCAN_TAG = "Foto-Scan"
XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"


def read_exif_dates(path: Path) -> dict[str, Optional[str]]:
    meta = read_exif_meta(path)
    return {
        "original": meta.get("original"),
        "digitized": meta.get("digitized"),
        "modify": meta.get("modify"),
    }


def read_exif_meta(path: Path) -> dict:
    result = {
        "original": None,
        "digitized": None,
        "modify": None,
        "description": None,
        "tags": [],
    }
    try:
        raw = path.read_bytes()
    except Exception:
        return result

    try:
        exif = piexif.load(raw)
    except Exception:
        exif = {}

    def dec(value) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            try:
                return bytes(int(x) & 0xFF for x in value).decode("utf-8", errors="replace").rstrip("\x00")
            except Exception:
                return None
        if isinstance(value, bytearray):
            value = bytes(value)
        if isinstance(value, bytes):
            if len(value) >= 2 and value[1] == 0:
                try:
                    return value.decode("utf-16le", errors="replace").rstrip("\x00")
                except Exception:
                    pass
            return value.decode("utf-8", errors="replace").rstrip("\x00")
        text = str(value).rstrip("\x00")
        # Guard: never show str(bytes-tuple) like "(70, 111, ...)"
        if re.fullmatch(r"\([\d,\s]+\)", text):
            try:
                nums = [int(x.strip()) for x in text[1:-1].split(",") if x.strip() != ""]
                return bytes(nums).decode("utf-8", errors="replace").rstrip("\x00")
            except Exception:
                return None
        return text

    zeroth = exif.get("0th") or {}
    exif_ifd = exif.get("Exif") or {}
    result["original"] = dec(exif_ifd.get(piexif.ExifIFD.DateTimeOriginal))
    result["digitized"] = dec(exif_ifd.get(piexif.ExifIFD.DateTimeDigitized))
    result["modify"] = dec(zeroth.get(piexif.ImageIFD.DateTime))

    desc = dec(zeroth.get(piexif.ImageIFD.ImageDescription))
    if not desc:
        desc = dec(zeroth.get(piexif.ImageIFD.XPComment))
    if not desc:
        desc = _read_xmp_description(raw)
    result["description"] = desc or None

    # One source only — otherwise Immich/UI show the same tags twice (XPKeywords + XMP)
    xmp_tags = _normalize_tags(_read_xmp_subjects(raw))
    if xmp_tags:
        result["tags"] = xmp_tags
    else:
        xp_tags: list[str] = []
        xp = zeroth.get(piexif.ImageIFD.XPKeywords)
        if xp:
            text = dec(xp) or ""
            xp_tags.extend(_split_tags(text.replace(";", ",")))
        result["tags"] = _normalize_tags(xp_tags)
    return result


def jpeg_scan_offset(data: bytes) -> Optional[int]:
    """
    Offset of the SOS marker (start of compressed image data).
    Must parse JPEG markers — a raw search for FF DA is unsafe because that
    byte pair can appear inside EXIF/APP segments.
    """
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i < n - 1:
        if data[i] != 0xFF:
            return None
        # Skip fill bytes (FF FF …)
        while i < n - 1 and data[i] == 0xFF and data[i + 1] == 0xFF:
            i += 1
        if i >= n - 1:
            return None
        marker = data[i + 1]
        if marker == 0xDA:  # Start Of Scan
            return i
        if marker == 0xD9:  # EOI before SOS — invalid
            return None
        # Standalone markers without length
        if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0x01, 0x00):
            i += 2
            continue
        if i + 3 >= n:
            return None
        seglen = int.from_bytes(data[i + 2 : i + 4], "big")
        if seglen < 2:
            return None
        i += 2 + seglen
    return None


def _is_exif_app1(jpeg: bytes, offset: int, seglen: int) -> bool:
    """True if segment at offset is APP1 with Exif payload."""
    if jpeg[offset + 1] != 0xE1 or seglen < 8:
        return False
    payload = jpeg[offset + 4 : offset + 2 + seglen]
    return payload.startswith(b"Exif\x00\x00")


def _replace_exif_app1(jpeg: bytes, exif_bytes: bytes) -> bytes:
    """
    Insert/replace APP1 Exif while copying SOS..EOF byte-identically.
    exif_bytes: output of piexif.dump() (normally starts with b'Exif\\x00\\x00').
    """
    if jpeg[:2] != b"\xff\xd8":
        raise ValueError("Kein JPEG")
    if not exif_bytes.startswith(b"Exif"):
        exif_bytes = b"Exif\x00\x00" + exif_bytes
    size = len(exif_bytes) + 2
    if size > 0xFFFF:
        raise ValueError("EXIF-Segment zu gross")
    app1 = b"\xff\xe1" + size.to_bytes(2, "big") + exif_bytes

    header = bytearray()
    i = 2
    n = len(jpeg)
    sos_at: Optional[int] = None
    while i < n - 1:
        if jpeg[i] != 0xFF:
            raise ValueError("Ungueltiges JPEG")
        while i < n - 1 and jpeg[i] == 0xFF and jpeg[i + 1] == 0xFF:
            header.append(0xFF)
            i += 1
        marker = jpeg[i + 1]
        if marker == 0xDA:
            sos_at = i
            break
        if marker == 0xD9:
            raise ValueError("Ungueltiges JPEG (EOI vor SOS)")
        if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0x01, 0x00):
            header.extend(jpeg[i : i + 2])
            i += 2
            continue
        if i + 3 >= n:
            raise ValueError("Ungueltiges JPEG")
        seglen = int.from_bytes(jpeg[i + 2 : i + 4], "big")
        if seglen < 2:
            raise ValueError("Ungueltiges JPEG (Segmentlaenge)")
        if not _is_exif_app1(jpeg, i, seglen):
            header.extend(jpeg[i : i + 2 + seglen])
        i += 2 + seglen

    if sos_at is None:
        raise ValueError("Ungueltiges JPEG (kein SOS)")

    # Prefer EXIF after JFIF APP0 if present as first header segment
    out = bytearray(b"\xff\xd8")
    inserted = False
    j = 0
    h = bytes(header)
    if len(h) >= 4 and h[0] == 0xFF and h[1] == 0xE0:
        hlen = int.from_bytes(h[2:4], "big")
        if hlen >= 2 and 2 + hlen <= len(h):
            out.extend(h[: 2 + hlen])
            out.extend(app1)
            out.extend(h[2 + hlen :])
            inserted = True
    if not inserted:
        out.extend(app1)
        out.extend(h)
    out.extend(jpeg[sos_at:])
    return bytes(out)


def _split_tags(text: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,;]+", text or "") if t.strip()]


def _normalize_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        tag = (tag or "").strip()
        if not tag or _is_garbage_tag(tag):
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def _is_garbage_tag(tag: str) -> bool:
    """Drop decode artifacts like '70' or '(70, 111, ...)'."""
    t = tag.strip()
    if re.fullmatch(r"\d+", t):
        return True
    if re.fullmatch(r"\(\d+\)", t):
        return True
    if re.fullmatch(r"\(?\d+", t) or re.fullmatch(r"\d+\)?", t):
        return True
    if re.fullmatch(r"\([\d,\s]+\)", t):
        return True
    return False


def _encode_xp_keywords(tags: list[str]) -> bytes:
    return (";".join(tags) + "\x00").encode("utf-16le")


def _encode_xp_string(text: str) -> bytes:
    return (text + "\x00").encode("utf-16le")


def _read_xmp_subjects(jpeg: bytes) -> list[str]:
    subjects: list[str] = []
    for match in re.finditer(
        rb"<dc:subject>\s*<rdf:Bag>(.*?)</rdf:Bag>\s*</dc:subject>",
        jpeg,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        bag = match.group(1).decode("utf-8", errors="replace")
        subjects.extend(re.findall(r"<rdf:li[^>]*>(.*?)</rdf:li>", bag, flags=re.I | re.S))
    return [s.strip() for s in subjects if s.strip()]


def _read_xmp_description(jpeg: bytes) -> Optional[str]:
    match = re.search(
        rb"<dc:description>\s*<rdf:Alt>\s*<rdf:li[^>]*>(.*?)</rdf:li>",
        jpeg,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        match = re.search(
            rb"<dc:description[^>]*>(.*?)</dc:description>",
            jpeg,
            flags=re.DOTALL | re.IGNORECASE,
        )
    if not match:
        return None
    text = match.group(1).decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]+>", "", text).strip()
    return text or None


def _build_xmp_packet(tags: list[str], description: str | None = None) -> bytes:
    parts = []
    if description:
        parts.append(
            "   <dc:description>\n"
            "    <rdf:Alt>\n"
            f'     <rdf:li xml:lang="x-default">{_xml_escape(description)}</rdf:li>\n'
            "    </rdf:Alt>\n"
            "   </dc:description>"
        )
    if tags:
        items = "\n".join(f"     <rdf:li>{_xml_escape(t)}</rdf:li>" for t in tags)
        parts.append(
            "   <dc:subject>\n"
            "    <rdf:Bag>\n"
            f"{items}\n"
            "    </rdf:Bag>\n"
            "   </dc:subject>"
        )
    inner = "\n".join(parts)
    xml = f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/">
{inner}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    body = XMP_HEADER + xml.encode("utf-8")
    size = len(body) + 2
    if size > 0xFFFF:
        raise ValueError("XMP-Paket zu gross")
    return b"\xff\xe1" + size.to_bytes(2, "big") + body


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _strip_xmp_segments(jpeg: bytes) -> bytes:
    if jpeg[:2] != b"\xff\xd8":
        return jpeg
    out = bytearray(jpeg[:2])
    i = 2
    n = len(jpeg)
    while i < n - 1:
        if jpeg[i] != 0xFF:
            out.extend(jpeg[i:])
            break
        marker = jpeg[i + 1]
        if marker == 0xDA:
            out.extend(jpeg[i:])
            break
        if marker == 0xD9:
            out.extend(jpeg[i : i + 2])
            break
        if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0x01, 0x00):
            out.extend(jpeg[i : i + 2])
            i += 2
            continue
        if i + 3 >= n:
            out.extend(jpeg[i:])
            break
        seglen = int.from_bytes(jpeg[i + 2 : i + 4], "big")
        segment = jpeg[i : i + 2 + seglen]
        payload = jpeg[i + 4 : i + 2 + seglen]
        is_xmp = marker == 0xE1 and payload.startswith(XMP_HEADER)
        if not is_xmp:
            out.extend(segment)
        i += 2 + seglen
    return bytes(out)


def _insert_xmp(jpeg: bytes, tags: list[str], description: str | None = None) -> bytes:
    if not tags and not description:
        return jpeg
    jpeg = _strip_xmp_segments(jpeg)
    xmp = _build_xmp_packet(tags, description)
    return jpeg[:2] + xmp + jpeg[2:]


def patch_exif_dates(
    path: Path,
    date_str: str | None = None,
    *,
    set_original: bool = True,
    set_digitized: bool = True,
    set_modify: bool = True,
    tags: list[str] | None = None,
    merge_tags: bool = True,
    description: str | None = None,
) -> None:
    """Write EXIF dates/tags/description; JPEG image payload stays byte-identical."""
    raw = path.read_bytes()
    if raw[:2] != b"\xff\xd8":
        raise ValueError("Kein JPEG")

    before_sos = jpeg_scan_offset(raw)
    if before_sos is None:
        raise ValueError("Ungueltiges JPEG (kein SOS)")
    before_payload = raw[before_sos:]

    existing_meta = read_exif_meta(path)

    try:
        exif = piexif.load(raw)
    except Exception:
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}, "thumbnail": None}

    exif.setdefault("0th", {})
    exif.setdefault("Exif", {})
    # Avoid piexif rewriting/breaking embedded thumbnails
    exif["thumbnail"] = None
    if "1st" in exif:
        exif["1st"] = {}

    if date_str:
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

    final_tags: list[str] | None = None
    if tags is not None:
        existing = existing_meta.get("tags") or []
        final_tags = _normalize_tags((existing if merge_tags else []) + list(tags))
        # Nur XMP Subject schreiben — XPKeywords entfernen, sonst doppelte Tags in Immich
        exif["0th"].pop(piexif.ImageIFD.XPKeywords, None)

    write_description = description is not None and description.strip() != ""
    desc_text = description.strip() if write_description else None
    if write_description and desc_text is not None:
        # ASCII-ish ImageDescription + Unicode XPComment for Immich/Windows
        try:
            exif["0th"][piexif.ImageIFD.ImageDescription] = desc_text.encode("ascii", errors="replace")
        except Exception:
            exif["0th"][piexif.ImageIFD.ImageDescription] = desc_text.encode("latin-1", errors="replace")
        exif["0th"][piexif.ImageIFD.XPComment] = _encode_xp_string(desc_text)

    exif_bytes = piexif.dump(exif)
    tmp = path.with_name(path.name + ".exiftmp")

    # Prefer our segment splice (guarantees identical image payload)
    try:
        out = _replace_exif_app1(raw, exif_bytes)
    except Exception:
        try:
            maybe = piexif.insert(exif_bytes, raw)
            if isinstance(maybe, (bytes, bytearray)):
                out = bytes(maybe)
            else:
                piexif.insert(exif_bytes, str(path), str(tmp))
                out = tmp.read_bytes()
        except (TypeError, ValueError):
            piexif.insert(exif_bytes, str(path), str(tmp))
            out = tmp.read_bytes()

    xmp_tags = final_tags if final_tags is not None else (existing_meta.get("tags") or [])
    xmp_desc = desc_text if write_description else existing_meta.get("description")
    if final_tags is not None or write_description:
        out = _insert_xmp(out, xmp_tags or [], xmp_desc)

    after_sos = jpeg_scan_offset(out)
    if after_sos is None or out[after_sos:] != before_payload:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(
            "Abbruch: Bilddaten haetten sich geaendert "
            "(Datei ggf. unuebliches JPEG — bitte melden)"
        )

    tmp.write_bytes(out)
    tmp.replace(path)


def rotate_jpeg(path: Path, degrees: int) -> str:
    """
    Rotate JPEG permanently (pixel data). Prefer lossless jpegtran; fallback Pillow.
    Resets EXIF Orientation to 1 so Immich/viewers show it correctly forever.
    degrees: 90 (CW), 180, 270 (CW) / or -90 for CCW.
    Returns method used: 'jpegtran' | 'pillow'
    """
    degrees = int(degrees) % 360
    if degrees not in (90, 180, 270):
        raise ValueError("Nur 90, 180 oder 270 Grad")

    raw = path.read_bytes()
    if raw[:2] != b"\xff\xd8":
        raise ValueError("Kein JPEG")

    tmp = path.with_name(path.name + ".rottmp")
    method = "jpegtran"

    if not _jpegtran_rotate(path, tmp, degrees):
        method = "pillow"
        _pillow_rotate(path, tmp, degrees)

    # Normalize orientation + drop stale thumbnail
    try:
        data = tmp.read_bytes()
        try:
            exif = piexif.load(data)
        except Exception:
            exif = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}, "thumbnail": None}
        exif.setdefault("0th", {})
        exif["0th"][piexif.ImageIFD.Orientation] = 1
        exif["thumbnail"] = None
        if "1st" in exif:
            exif["1st"] = {}
        exif_bytes = piexif.dump(exif)
        try:
            maybe = piexif.insert(exif_bytes, data)
            if isinstance(maybe, (bytes, bytearray)):
                fixed = bytes(maybe)
            else:
                piexif.insert(exif_bytes, str(tmp), str(tmp) + ".fix")
                fixed = Path(str(tmp) + ".fix").read_bytes()
                Path(str(tmp) + ".fix").unlink(missing_ok=True)
        except (TypeError, ValueError):
            out2 = path.with_name(path.name + ".rotfix")
            piexif.insert(exif_bytes, str(tmp), str(out2))
            fixed = out2.read_bytes()
            out2.unlink(missing_ok=True)
        tmp.write_bytes(fixed)
    except Exception:
        # Rotation already done; orientation fix is best-effort
        pass

    tmp.replace(path)
    return method


def _jpegtran_rotate(src: Path, dest: Path, degrees: int) -> bool:
    import shutil
    import subprocess

    bin_path = shutil.which("jpegtran")
    if not bin_path:
        return False
    try:
        subprocess.run(
            [
                bin_path,
                "-rotate",
                str(degrees),
                "-copy",
                "all",
                "-outfile",
                str(dest),
                str(src),
            ],
            check=True,
            capture_output=True,
        )
        return dest.is_file() and dest.stat().st_size > 0
    except Exception:
        dest.unlink(missing_ok=True)
        return False


def _pillow_rotate(src: Path, dest: Path, degrees: int) -> None:
    from PIL import Image

    # Map CW degrees to PIL transpose
    ops = {
        90: Image.Transpose.ROTATE_270,  # PIL ROTATE_270 = 90° CW
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_90,  # PIL ROTATE_90 = 90° CCW = 270° CW
    }
    with Image.open(src) as im:
        im = im.convert("RGB")
        im = im.transpose(ops[degrees])
        im.save(dest, format="JPEG", quality=95, optimize=True)
