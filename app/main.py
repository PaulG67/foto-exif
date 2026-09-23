"""Foto-exif — EXIF-Datums-Editor für Immich (Unraid Docker)."""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from io import BytesIO
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image

from app.exif_utils import DEFAULT_SCAN_TAG, patch_exif_dates, read_exif_meta, rotate_jpeg

JPEG_EXTS = {".jpg", ".jpeg"}
PHOTOS_ROOT = Path(os.environ.get("PHOTOS_ROOT", "/photos")).resolve()
EXPORT_ROOT = Path(os.environ.get("EXPORT_ROOT", "/export")).resolve()
PORT = int(os.environ.get("PORT", "8791"))

ROOTS = {
    "photos": PHOTOS_ROOT,
    "export": EXPORT_ROOT,
}


def create_app() -> Flask:
    app = Flask(__name__)
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)

    def root_of(name: str | None) -> Path:
        key = (name or "photos").strip().lower()
        if key not in ROOTS:
            abort(400, "Ungueltiger Root")
        return ROOTS[key]

    def safe_path(rel: str, root_name: str = "photos") -> Path:
        root = root_of(root_name)
        rel = (rel or "").replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            abort(400, "Ungueltiger Pfad")
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            abort(400, f"Pfad ausserhalb von /{root_name}")
        return target

    def rel_of(path: Path, root_name: str = "photos") -> str:
        root = root_of(root_name)
        return path.resolve().relative_to(root).as_posix()

    def unique_dest(dest_dir: Path, name: str) -> Path:
        dest = dest_dir / name
        if not dest.exists():
            return dest
        stem = Path(name).stem
        suffix = Path(name).suffix
        n = 1
        while True:
            candidate = dest_dir / f"{stem}_{n}{suffix}"
            if not candidate.exists():
                return candidate
            n += 1

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "photos": str(PHOTOS_ROOT),
            "photos_exists": PHOTOS_ROOT.is_dir(),
            "export": str(EXPORT_ROOT),
            "export_exists": EXPORT_ROOT.is_dir(),
        }

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/browse")
    def browse():
        root_name = request.args.get("root", "photos")
        rel = request.args.get("path", "")
        folder = safe_path(rel, root_name)
        if not folder.exists():
            abort(404, "Ordner nicht gefunden")
        if not folder.is_dir():
            abort(400, "Kein Ordner")

        dirs = []
        files = []
        try:
            entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            abort(403, "Kein Zugriff")

        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": rel_of(entry, root_name)})
            elif entry.is_file() and entry.suffix.lower() in JPEG_EXTS:
                meta = read_exif_meta(entry)
                files.append(
                    {
                        "name": entry.name,
                        "path": rel_of(entry, root_name),
                        "size": entry.stat().st_size,
                        "original": meta.get("original"),
                        "digitized": meta.get("digitized"),
                        "modify": meta.get("modify"),
                        "description": meta.get("description"),
                        "tags": meta.get("tags") or [],
                    }
                )

        crumbs = []
        if rel:
            acc = []
            for part in Path(rel).parts:
                acc.append(part)
                crumbs.append({"name": part, "path": "/".join(acc)})

        return jsonify(
            {
                "root": root_name,
                "rootPath": str(root_of(root_name)),
                "path": rel,
                "crumbs": crumbs,
                "dirs": dirs,
                "files": files,
            }
        )

    @app.get("/api/thumb")
    def thumb():
        root_name = request.args.get("root", "photos")
        path = safe_path(request.args.get("path", ""), root_name)
        if not path.is_file():
            abort(404)
        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((240, 240), Image.Resampling.LANCZOS)
                buf = BytesIO()
                im.save(buf, format="JPEG", quality=82)
                buf.seek(0)
                return send_file(buf, mimetype="image/jpeg")
        except Exception:
            abort(500)

    @app.get("/api/preview")
    def preview():
        root_name = request.args.get("root", "photos")
        path = safe_path(request.args.get("path", ""), root_name)
        if not path.is_file():
            abort(404)
        return send_file(path)

    @app.post("/api/apply")
    def apply_dates():
        data = request.get_json(force=True, silent=True) or {}
        root_name = data.get("root") or "photos"
        paths = data.get("paths") or []
        date_str = data.get("date")
        time_str = data.get("time") or "12:00:00"
        opts = data.get("fields") or {}
        tag_opts = data.get("tags") or {}
        description = (data.get("description") or "").strip()
        rename_opts = data.get("rename") or {}

        if not paths:
            return jsonify({"ok": False, "error": "Keine Dateien"}), 400

        exif_date = to_exif(date_str, time_str)
        if not exif_date:
            return jsonify({"ok": False, "error": "Ungueltiges Datum/Uhrzeit"}), 400

        set_original = bool(opts.get("original", True))
        set_digitized = bool(opts.get("digitized", True))
        set_modify = bool(opts.get("modify", True))
        if not (set_original or set_digitized or set_modify):
            return jsonify({"ok": False, "error": "Mindestens ein Datumsfeld waehlen"}), 400

        tags = collect_tags(tag_opts)
        do_rename = bool(rename_opts.get("enabled"))
        rename_prefix = sanitize_basename(str(rename_opts.get("prefix") or "foto"))
        try:
            start_num = int(rename_opts.get("start") or 1)
            digits = int(rename_opts.get("digits") or 3)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Ungueltige Nummerierung"}), 400
        if start_num < 0 or digits < 1 or digits > 8:
            return jsonify({"ok": False, "error": "Nummerierung: Start >= 0, Stellen 1-8"}), 400

        written = []
        errors = []
        new_selection = []

        for index, rel in enumerate(paths):
            try:
                path = safe_path(rel, root_name)
                if not path.is_file() or path.suffix.lower() not in JPEG_EXTS:
                    raise ValueError("Keine JPEG-Datei")
                patch_exif_dates(
                    path,
                    exif_date,
                    set_original=set_original,
                    set_digitized=set_digitized,
                    set_modify=set_modify,
                    tags=tags,
                    merge_tags=True,
                    description=description or None,
                )

                new_rel = rel
                new_name = path.name
                if do_rename:
                    num = start_num + index
                    suffix = path.suffix.lower() if path.suffix else ".jpg"
                    candidate = f"{rename_prefix}{num:0{digits}d}{suffix}"
                    dest = unique_dest(path.parent, candidate)
                    if dest.resolve() != path.resolve():
                        path.rename(dest)
                        path = dest
                    new_name = path.name
                    new_rel = rel_of(path, root_name)

                meta = read_exif_meta(path)
                written.append(
                    {
                        "path": new_rel,
                        "from": rel,
                        "name": new_name,
                        **meta,
                    }
                )
                new_selection.append(new_rel)
            except Exception as exc:
                errors.append({"path": rel, "error": str(exc)})

        return jsonify(
            {
                "ok": len(errors) == 0,
                "written": len(written),
                "files": written,
                "errors": errors,
                "date": exif_date,
                "tags": tags,
                "description": description or None,
                "renamed": do_rename,
                "paths": new_selection,
            }
        )

    @app.post("/api/rotate")
    def rotate():
        data = request.get_json(force=True, silent=True) or {}
        root_name = data.get("root") or "photos"
        rel = data.get("path") or ""
        try:
            degrees = int(data.get("degrees") or 90)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Ungueltiger Winkel"}), 400

        # allow -90 as CCW
        if degrees == -90:
            degrees = 270
        if degrees not in (90, 180, 270):
            return jsonify({"ok": False, "error": "Nur 90, 180 oder 270 Grad"}), 400

        try:
            path = safe_path(rel, root_name)
            if not path.is_file() or path.suffix.lower() not in JPEG_EXTS:
                raise ValueError("Keine JPEG-Datei")
            method = rotate_jpeg(path, degrees)
            meta = read_exif_meta(path)
            return jsonify(
                {
                    "ok": True,
                    "path": rel,
                    "degrees": degrees,
                    "method": method,
                    **meta,
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "path": rel}), 400

    @app.post("/api/move-to-export")
    def move_to_export():
        data = request.get_json(force=True, silent=True) or {}
        paths = data.get("paths") or []
        if not paths:
            return jsonify({"ok": False, "error": "Keine Dateien"}), 400

        EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
        moved = []
        errors = []

        for rel in paths:
            try:
                src = safe_path(rel, "photos")
                if not src.is_file() or src.suffix.lower() not in JPEG_EXTS:
                    raise ValueError("Keine JPEG-Datei")
                dest = unique_dest(EXPORT_ROOT, src.name)
                shutil.move(str(src), str(dest))
                meta = read_exif_meta(dest)
                moved.append(
                    {
                        "from": rel,
                        "to": dest.name,
                        "path": dest.name,
                        **meta,
                    }
                )
            except Exception as exc:
                errors.append({"path": rel, "error": str(exc)})

        return jsonify(
            {
                "ok": len(errors) == 0,
                "moved": len(moved),
                "files": moved,
                "errors": errors,
            }
        )

    return app


def sanitize_basename(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", name)
    name = name.strip(". ")
    return name or "foto"


def collect_tags(tag_opts: dict) -> list[str]:
    tags: list[str] = []
    if bool(tag_opts.get("fotoScan", True)):
        tags.append(DEFAULT_SCAN_TAG)
    extra = tag_opts.get("extra") or ""
    if isinstance(extra, list):
        tags.extend(str(t).strip() for t in extra if str(t).strip())
    else:
        for part in re.split(r"[,;]+", str(extra)):
            part = part.strip()
            if part:
                tags.append(part)
    # dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        key = t.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def to_exif(date_str: str | None, time_str: str | None) -> str | None:
    if not date_str:
        return None
    time_str = (time_str or "12:00:00").strip()
    if re.fullmatch(r"\d{2}:\d{2}", time_str):
        time_str += ":00"
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.strftime("%Y:%m:%d %H:%M:%S")


app = create_app()


def main() -> None:
    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()
