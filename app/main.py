"""Foto-exif — EXIF-Datums-Editor für Immich (Unraid Docker)."""

from __future__ import annotations

import os
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image

from app.exif_utils import patch_exif_dates, read_exif_dates

JPEG_EXTS = {".jpg", ".jpeg"}
PHOTOS_ROOT = Path(os.environ.get("PHOTOS_ROOT", "/photos")).resolve()
PORT = int(os.environ.get("PORT", "8791"))


def create_app() -> Flask:
    app = Flask(__name__)

    def safe_path(rel: str) -> Path:
        rel = (rel or "").replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            abort(400, "Ungültiger Pfad")
        target = (PHOTOS_ROOT / rel).resolve()
        try:
            target.relative_to(PHOTOS_ROOT)
        except ValueError:
            abort(400, "Pfad außerhalb von /photos")
        return target

    def rel_of(path: Path) -> str:
        return path.resolve().relative_to(PHOTOS_ROOT).as_posix()

    @app.get("/health")
    def health():
        return {"ok": True, "photos": str(PHOTOS_ROOT), "exists": PHOTOS_ROOT.is_dir()}

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/browse")
    def browse():
        rel = request.args.get("path", "")
        folder = safe_path(rel)
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
                dirs.append({"name": entry.name, "path": rel_of(entry)})
            elif entry.is_file() and entry.suffix.lower() in JPEG_EXTS:
                dates = read_exif_dates(entry)
                files.append(
                    {
                        "name": entry.name,
                        "path": rel_of(entry),
                        "size": entry.stat().st_size,
                        "original": dates.get("original"),
                        "digitized": dates.get("digitized"),
                        "modify": dates.get("modify"),
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
                "root": str(PHOTOS_ROOT),
                "path": rel,
                "crumbs": crumbs,
                "dirs": dirs,
                "files": files,
            }
        )

    @app.get("/api/thumb")
    def thumb():
        path = safe_path(request.args.get("path", ""))
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
        path = safe_path(request.args.get("path", ""))
        if not path.is_file():
            abort(404)
        return send_file(path)

    @app.post("/api/apply")
    def apply_dates():
        data = request.get_json(force=True, silent=True) or {}
        paths = data.get("paths") or []
        date_str = data.get("date")
        time_str = data.get("time") or "12:00:00"
        opts = data.get("fields") or {}

        if not paths:
            return jsonify({"ok": False, "error": "Keine Dateien"}), 400

        exif_date = to_exif(date_str, time_str)
        if not exif_date:
            return jsonify({"ok": False, "error": "Ungültiges Datum/Uhrzeit"}), 400

        set_original = bool(opts.get("original", True))
        set_digitized = bool(opts.get("digitized", True))
        set_modify = bool(opts.get("modify", True))
        if not (set_original or set_digitized or set_modify):
            return jsonify({"ok": False, "error": "Mindestens ein Feld wählen"}), 400

        written = []
        errors = []
        for rel in paths:
            try:
                path = safe_path(rel)
                if not path.is_file() or path.suffix.lower() not in JPEG_EXTS:
                    raise ValueError("Keine JPEG-Datei")
                patch_exif_dates(
                    path,
                    exif_date,
                    set_original=set_original,
                    set_digitized=set_digitized,
                    set_modify=set_modify,
                )
                dates = read_exif_dates(path)
                written.append({"path": rel, **dates})
            except Exception as exc:
                errors.append({"path": rel, "error": str(exc)})

        return jsonify(
            {
                "ok": len(errors) == 0,
                "written": len(written),
                "files": written,
                "errors": errors,
                "date": exif_date,
            }
        )

    return app


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
