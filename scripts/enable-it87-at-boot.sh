#!/usr/bin/env bash
# Optional persistence after checking this hardware's sensors and fan controls.
set -Eeuo pipefail
(( EUID == 0 )) || { echo 'Run with sudo after hardware verification.' >&2; exit 1; }
[[ "$(cat /sys/class/dmi/id/board_name)" == 'X870E AORUS XTREME AI TOP' ]] || exit 1
[[ "$(cat /sys/module/it87/version)" == 'v2.0-4-gbc06d34.20260913' ]] || exit 1
TARGET='/etc/modules-load.d/thermaldeck-it87.conf'
if [[ -e "$TARGET" || -L "$TARGET" ]]; then
    [[ -f "$TARGET" && ! -L "$TARGET" && "$(cat "$TARGET")" == 'it87' ]] || {
        echo 'Existing startup file differs; review it before changing it.' >&2; exit 1;
    }
else
    install -d -m 0755 /etc/modules-load.d
    printf 'it87\n' > "$TARGET"
    chmod 0644 "$TARGET"
fi
echo 'The verified it87 driver will load at boot. No fan curve is applied at boot.'
