#!/usr/bin/env bash
# Installs only this application; driver installation is a separate command.
set -Eeuo pipefail
umask 022
if (( EUID != 0 )); then
    echo 'Run: sudo ./scripts/install.sh' >&2
    exit 1
fi
PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for file in launcher.py thermaldeck/__main__.py thermaldeck/gui.py thermaldeck/worker.py assets/icon.svg; do
    [[ -f "$PROJECT_DIR/$file" && ! -L "$PROJECT_DIR/$file" ]] || { echo "Missing source: $file" >&2; exit 1; }
done
exec 9>/run/lock/thermaldeck-app-install.lock
flock -n 9 || { echo 'Another installer is running.' >&2; exit 1; }
if [[ -e /run/thermaldeck/session.lock ]]; then
    exec 8<>/run/thermaldeck/session.lock
    flock -n 8 || { echo 'Close ThermalDeck before updating the application.' >&2; exit 1; }
fi
[[ ! -L /opt/thermaldeck ]] || { echo '/opt/thermaldeck must not be a symlink.' >&2; exit 1; }
STAGE=$(mktemp -d /opt/thermaldeck-install.XXXXXXXX)
trap 'rm -rf -- "$STAGE"' EXIT
install -d -m 0755 "$STAGE/thermaldeck" "$STAGE/assets"
install -m 0644 "$PROJECT_DIR/launcher.py" "$STAGE/launcher.py"
for module in "$PROJECT_DIR"/thermaldeck/*.py; do
    [[ ! -L "$module" ]] || { echo 'Source symlinks are not accepted.' >&2; exit 1; }
    install -m 0644 "$module" "$STAGE/thermaldeck/"
done
install -m 0644 "$PROJECT_DIR/assets/icon.svg" "$STAGE/assets/icon.svg"
/usr/bin/python3 -I -m compileall -q "$STAGE"
chmod 0755 "$STAGE"
if [[ -d /opt/thermaldeck ]]; then
    BACKUP="/opt/thermaldeck-backup-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    mv /opt/thermaldeck "$BACKUP"
    echo "Previous application retained at $BACKUP"
fi
mv "$STAGE" /opt/thermaldeck
install -d -m 0755 /usr/local/bin /usr/local/share/applications /usr/local/share/icons/hicolor/scalable/apps
cat > /usr/local/bin/thermaldeck <<'EOF'
#!/bin/sh
exec /usr/bin/python3 -I /opt/thermaldeck/launcher.py "$@"
EOF
chmod 0755 /usr/local/bin/thermaldeck
install -m 0644 /opt/thermaldeck/assets/icon.svg /usr/local/share/icons/hicolor/scalable/apps/thermaldeck.svg
cat > /usr/local/share/applications/io.github.JR-Yu6197.ThermalDeck.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=ThermalDeck
Comment=GPU, CPU, case fan and pump controls
Comment[ko]=GPU·CPU·케이스 팬·펌프 통합 제어
Exec=/usr/local/bin/thermaldeck gui
Icon=thermaldeck
Terminal=false
Categories=System;Monitor;
Keywords=GPU;CPU;Fan;Pump;Cooling;팬;펌프;
StartupNotify=true
EOF
command -v update-desktop-database >/dev/null && update-desktop-database /usr/local/share/applications || true
echo 'ThermalDeck installed. Start as your normal user: thermaldeck'
echo 'First fan change opens the standard administrator authentication dialog.'
