#!/bin/sh
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"

if ! getent group "${PGID}" >/dev/null 2>&1; then
  groupadd -g "${PGID}" fotoexif || true
fi
if ! getent passwd "${PUID}" >/dev/null 2>&1; then
  useradd -u "${PUID}" -g "${PGID}" -M -s /usr/sbin/nologin fotoexif || true
fi

exec gosu "${PUID}:${PGID}" \
  gunicorn "app.main:app" \
    --bind "0.0.0.0:${PORT:-8791}" \
    --workers "${WEB_CONCURRENCY:-2}" \
    --threads "${WEB_THREADS:-4}" \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
