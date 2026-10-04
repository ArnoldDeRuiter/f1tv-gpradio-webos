#!/bin/sh
# Hand-rolled .ipk build (ar + tar), no ares-cli dependency. See
# webos-home-customizer/build.sh for the same pattern with more comments.
set -e
cd "$(dirname "$0")"

APP_ID="nl.arnolderuiter.f1tvgpradio"
VERSION="$(python3 -c "import json;print(json.load(open('appinfo.json'))['version'])")"
INSTALL_ROOT="usr/palm/applications/$APP_ID"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

DATA_DIR="$WORK/data/$INSTALL_ROOT"
mkdir -p "$DATA_DIR"
cp -R appinfo.json index.html icon.png splash.png loginfill.py start-loginfill.sh \
  kill-netflix.sh kill-youtube-preload.sh gpradio_overlay.py start-gpradio-overlay.sh "$DATA_DIR/"
chmod +x "$DATA_DIR/start-loginfill.sh" "$DATA_DIR/kill-netflix.sh" "$DATA_DIR/kill-youtube-preload.sh" "$DATA_DIR/start-gpradio-overlay.sh"

# packageinfo.json is separate from appinfo.json and lives at a different
# path entirely -- required by the on-device installer (appinstalld), or
# install fails with "Cannot find packageinfo.json". Paths inside the ipk
# are relative to the real root (usr/palm/...), NOT media/developer/apps/...
# -- the installer itself relocates them to the writable partition at
# install time.
PKG_DIR="$WORK/data/usr/palm/packages/$APP_ID"
mkdir -p "$PKG_DIR"
cat > "$PKG_DIR/packageinfo.json" <<EOF
{
  "id": "$APP_ID",
  "version": "$VERSION",
  "app": "$APP_ID"
}
EOF

( cd "$WORK/data" && tar --owner=0 --group=0 --mtime="UTC 2020-01-01" --sort=name -czf "$WORK/data.tar.gz" usr )

INSTALLED_SIZE="$(du -sb "$DATA_DIR" | cut -f1)"
mkdir -p "$WORK/control"
cat > "$WORK/control/control" <<EOF
Package: $APP_ID
Version: $VERSION
Section: misc
Priority: optional
Architecture: all
Installed-Size: $INSTALLED_SIZE
Maintainer: ArnoldDeRuiter
Description: F1TV + Grand Prix Radio combined for rooted webOS TVs -- full-screen F1TV video with a toggle-able Grand Prix Radio audio overlay widget, so both can run at once (something two separate webOS apps can't do).
webOS-Package-Format-Version: 2
webOS-Packager-Version: x.y.x
EOF
( cd "$WORK/control" && tar --owner=0 --group=0 --mtime="UTC 2020-01-01" --sort=name -czf "$WORK/control.tar.gz" control )

echo "2.0" > "$WORK/debian-binary"
OUT="${APP_ID}_${VERSION}_all.ipk"
rm -f "$OUT"
( cd "$WORK" && ar -crf "$OUT" debian-binary control.tar.gz data.tar.gz )
mv "$WORK/$OUT" "./$OUT"

sha256sum "$OUT"
echo "Built: $(pwd)/$OUT"
