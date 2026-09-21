# Foto-exif

Lokale Unraid-App zum Setzen von **EXIF-Aufnahmedaten** (DateTimeOriginal) bei gescannten JPEGs — damit **Immich** die Bilder chronologisch richtig einordnet.

Speichert **direkt in die Dateien** im gemounteten Ordner. Es werden nur Metadaten geschrieben (kein Qualitätsverlust).

## Unraid — Erstinstallation

Im **Unraid-Terminal**:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/PaulG67/foto-exif/main/unraid/install-template.sh)
```

Danach in der Unraid-UI:

1. **Docker → Container hinzufügen**
2. Template **foto-exif** auswählen
3. **Photos**-Pfad auf deinen Scan-/Foto-Ordner setzen (Schreibzugriff)
4. **Apply**
5. WebUI: `http://UNRAID-IP:8791`

## Update erzwingen

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/PaulG67/foto-exif/main/unraid/install-template.sh)
```

Dann **Docker → foto-exif → Force Update**.

Wenn das Template neue Felder hat: zusätzlich **Edit → Apply**.

## Nutzung

1. In der WebUI Ordner öffnen
2. JPEGs auswählen
3. Aufnahmedatum / Uhrzeit setzen
4. **In Dateien speichern**

In Immich anschließend Metadaten der betroffenen Assets neu einlesen.

## Docker Compose (optional)

```yaml
services:
  foto-exif:
    image: ghcr.io/paulg67/foto-exif:latest
    ports:
      - "8791:8791"
    environment:
      - PUID=99
      - PGID=100
      - TZ=Europe/Zurich
    volumes:
      - /mnt/user/photos:/photos:rw
```

## Image

`ghcr.io/paulg67/foto-exif:latest`
