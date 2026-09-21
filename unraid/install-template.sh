#!/bin/bash
# Install / update Foto-exif Unraid user template + pull image
# Run on Unraid Terminal:
# bash <(curl -fsSL https://raw.githubusercontent.com/PaulG67/foto-exif/main/unraid/install-template.sh)

set -euo pipefail

TEMPLATE_DIR="/boot/config/plugins/dockerMan/templates-user"
USER_XML="${TEMPLATE_DIR}/my-foto-exif.xml"
IMAGE="ghcr.io/paulg67/foto-exif:latest"
RAW_BASE="https://raw.githubusercontent.com/PaulG67/foto-exif/main"
SRC_DIR="/mnt/user/appdata/foto-exif-src"
REPO="https://github.com/PaulG67/foto-exif.git"

echo "==> 1/3 Unraid-Vorlage"
mkdir -p "${TEMPLATE_DIR}"
# Cache umgehen
curl -fsSL -H "Cache-Control: no-cache" "${RAW_BASE}/unraid/my-foto-exif.xml?$(date +%s)" -o "${USER_XML}"

# Validierung: ohne Repository/Photos ist die Vorlage fuer Unraid nutzlos
if ! grep -q '<Repository>ghcr.io/paulg67/foto-exif:latest</Repository>' "${USER_XML}"; then
  echo "FEHLER: Vorlage enthaelt kein Repository — Abbruch."
  exit 1
fi
if ! grep -q 'Name="Photos"' "${USER_XML}"; then
  echo "FEHLER: Vorlage enthaelt keinen Photos-Pfad — Abbruch."
  exit 1
fi
if ! grep -q '</WebUI>' "${USER_XML}"; then
  echo "FEHLER: WebUI-Tag kaputt — Abbruch."
  exit 1
fi

echo "  ${USER_XML}"
echo "  Name/Repository/Photos OK"
wc -c "${USER_XML}" | awk '{print "  Groesse:" $1 " Bytes"}'
grep -E 'Name="Photos"|<Repository>|<Name>' "${USER_XML}" | sed 's/^/  /'

echo "==> 2/3 Docker-Image"
if docker pull "${IMAGE}" 2>/dev/null; then
  echo "  pulled ${IMAGE}"
else
  echo "  pull failed — building locally from GitHub"
  if command -v git >/dev/null 2>&1; then
    if [[ -d "${SRC_DIR}/.git" ]]; then
      git -C "${SRC_DIR}" pull --ff-only || true
    else
      rm -rf "${SRC_DIR}"
      git clone --depth 1 "${REPO}" "${SRC_DIR}"
    fi
  else
    mkdir -p "${SRC_DIR}"
    curl -fsSL "https://codeload.github.com/PaulG67/foto-exif/tar.gz/refs/heads/main" \
      | tar -xz -C "${SRC_DIR}" --strip-components=1
  fi
  docker build -t "${IMAGE}" "${SRC_DIR}"
  echo "  tagged ${IMAGE}"
fi

echo "==> 3/3 Fertig"
echo
echo "WICHTIG: Seite neu laden (F5), dann:"
echo "  Docker -> Container hinzufuegen -> Template "foto-exif" (nicht alte Session)"
echo "  Es muessen erscheinen: Name=foto-exif, Quelle=ghcr.io/..., Pfad Photos"
echo "  WebUI: http://UNRAID-IP:8791"
echo
echo "Falls Felder leer bleiben:"
echo "  cat ${USER_XML} | head"
echo "  und dieses Script erneut ausfuehren."
