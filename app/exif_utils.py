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
        if isinstance(value, bytes):
            # XP* tags are UTF-16LE
            if len(value) >= 2 and value[1] == 0:
                try:
                    return value.decode("utf-16le", errors="replace").rstrip("\x00")
                except Exception:
                    pass
            return value.decode("utf-8", errors="replace").rstrip("\x00")
        return str(value)

    zeroth = exif.get("0th") or {}
    exif_ifd = exif.get("Exif") or {}
    result["original"] = dec(exif_ifd.get(piexif.ExifIFD.DateTimeOriginal))
    result["digitized"] = dec(exif_ifd.get(piexif.ExifIFD.DateTimeDigitized))
    result["modify"] = dec(zeroth.get(piexif.ImageIFD.DateTime))

    tags: list[str] = []
    xp = zeroth.get(piexif.ImageIFD.XPKeywords)
    if xp:
        text = dec(xp) or ""
        tags.extend(_split_tags(text.replace(";", ",")))

    tags.extend(_read_xmp_subjects(raw))
    result["tags"] = _normalize_tags(tags)
    return result


def jpeg_scan_offset(data: bytes) -> Optional[int]:
    i = 0
    while i < len(data) - 1:
        if data[i] == 0xFF and data[i + 1] == 0xDA:
            return i
        i += 1
    return None


def _split_tags(text: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,;]+", text or "") if t.strip()]


def _normalize_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def _encode_xp_keywords(tags: list[str]) -> bytes:
    return (";".join(tags) + "\x00").encode("utf-16le")


def _read_xmp_subjects(jpeg: bytes) -> list[str]:
    subjects: list[str] = []
    for match in re.finditer(
        rb"<dc:subject>\s*<rdf:Bag>(.*?)</rdf:Bag>\s*</dc:subject>",
        jpeg,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        bag = match.group(1).decode("utf-8", errors="replace")
        subjects.extend(re.findall(r"<rdf:li[^>]*>(.*?)</rdf:li>", bag, flags=re.I | re.S))
    # also rdf:li with parseType
    for match in re.finditer(rb"<rdf:li[^>]*>([^<]+)</rdf:li>", jpeg, flags=re.I):
        val = match.group(1).decode("utf-8", errors="replace").strip()
        if val and val not in subjects:
            # only keep if near subject context is hard; trust bag parse above primarily
            pass
    return [s.strip() for s in subjects if s.strip()]


def _build_xmp_packet(tags: list[str]) -> bytes:
    items = "\n".join(f"     <rdf:li>{_xml_escape(t)}</rdf:li>" for t in tags)
    xml = f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:subject>
    <rdf:Bag>
{items}
    </rdf:Bag>
   </dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    body = XMP_HEADER + xml.encode("utf-8")
    # APP1: FF E1 + length(2) + payload ; length includes size bytes but not FFE1
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
    """Remove existing XMP APP1 segments; keep EXIF and image data."""
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
        if marker == 0xDA:  # SOS — rest is image
            out.extend(jpeg[i:])
            break
        if marker == 0xD9:  # EOI
            out.extend(jpeg[i : i + 2])
            break
        # markers without length
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


def _insert_xmp(jpeg: bytes, tags: list[str]) -> bytes:
    jpeg = _strip_xmp_segments(jpeg)
    xmp = _build_xmp_packet(tags)
    # insert XMP right after SOI
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
) -> None:
    """Write EXIF dates and/or keyword tags; JPEG image payload stays byte-identical."""
    raw = path.read_bytes()
    if raw[:2] != b"\xff\xd8":
        raise ValueError("Kein JPEG")

    before_sos = jpeg_scan_offset(raw)
    if before_sos is None:
        raise ValueError("Ungueltiges JPEG (kein SOS)")
    before_payload = raw[before_sos:]

    try:
        exif = piexif.load(raw)
    except Exception:
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}, "thumbnail": None}

    exif.setdefault("0th", {})
    exif.setdefault("Exif", {})

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
        existing = read_exif_meta(path).get("tags") or []
        final_tags = _normalize_tags((existing if merge_tags else []) + list(tags))
        exif["0th"][piexif.ImageIFD.XPKeywords] = _encode_xp_keywords(final_tags)

    exif_bytes = piexif.dump(exif)
    tmp = path.with_name(path.name + ".exiftmp")

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

    if final_tags is not None:
        out = _insert_xmp(out, final_tags)

    after_sos = jpeg_scan_offset(out)
    if after_sos is None or out[after_sos:] != before_payload:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise RuntimeError("Abbruch: Bilddaten haetten sich geaendert")

    tmp.write_bytes(out)
    tmp.replace(path)
