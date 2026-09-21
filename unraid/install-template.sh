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
curl -fsSL "${RAW_BASE}/unraid/my-foto-exif.xml" -o "${USER_XML}"
echo "  ${USER_XML}"

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
echo "Neu installieren:"
echo "  Docker → Container hinzufügen → Template «foto-exif»"
echo "  Photos-Pfad auf deinen Foto-/Scan-Ordner setzen (rw) → Apply"
echo "  WebUI: http://UNRAID-IP:8791"
echo
echo "Update:"
echo "  bash <(curl -fsSL ${RAW_BASE}/unraid/install-template.sh)"
echo "  danach Docker → foto-exif → Force Update"
echo "  (neue Template-Felder: Edit → Apply)"
