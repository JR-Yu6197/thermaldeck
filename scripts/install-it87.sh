#!/usr/bin/env bash
# Installs the reviewed upstream it87 driver. No PWM values are written here.
set -Eeuo pipefail
umask 022

readonly COMMIT='bc06d3488439e5fcd725c1bdcfcac994d6d95cac'
readonly VERSION='v2.0-4-gbc06d34.20260913'
readonly SOURCE_DIR="/usr/src/it87-${VERSION}"
LOAD_MODULE=1

usage() {
    cat <<'EOF'
Usage: sudo ./scripts/install-it87.sh [--no-load]

Downloads checksum-verified, pinned upstream source; builds and installs it87
with DKMS for the running kernel. By default, also loads the module and records
sensor/PWM state. Loading a hardware driver performs hardware initialization.
This installer never changes fan duty or runs automatic fan calibration.

  --no-load  Install with DKMS without loading the module now.
  --help     Show this message without changing the system.

Secure Boot must be disabled. An existing different it87 DKMS version or a
different loaded it87 module causes an error; nothing is forcibly removed.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while (($#)); do
    case "$1" in
        --no-load) LOAD_MODULE=0 ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; die "Unknown option: $1" ;;
    esac
    shift
done

((EUID == 0)) || die 'Run this installer with sudo or the authorized system installer.'
for tool in curl sha256sum dkms make gcc modprobe modinfo uname flock mktemp install cmp sed stat readlink; do
    command -v "$tool" >/dev/null || die "Required command is missing: $tool"
done

exec 9>/run/lock/thermaldeck-it87-install.lock
flock -n 9 || die 'Another ThermalDeck it87 installation is running.'

readonly KERNEL="$(uname -r)"
[[ -f "/lib/modules/${KERNEL}/build/Makefile" ]] ||
    die "Install headers for the running kernel first: linux-headers-${KERNEL}"

if [[ -d /sys/firmware/efi ]]; then
    command -v mokutil >/dev/null || die 'Install mokutil to check Secure Boot status.'
    SB_STATE=$(LC_ALL=C mokutil --sb-state) || die 'Could not determine Secure Boot state.'
    [[ "$SB_STATE" == *'SecureBoot disabled'* ]] ||
        die "Secure Boot is not confirmed disabled. This installer does not enroll signing keys: ${SB_STATE}"
fi

DKMS_STATUS=$(dkms status -m it87) || die 'Could not read existing it87 DKMS registrations.'
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    case "$entry" in
        "it87/${VERSION},"*|"it87/${VERSION}:"*) ;;
        *) die "Another it87 DKMS registration exists. Review it before proceeding: ${entry}" ;;
    esac
done <<< "$DKMS_STATUS"

readonly REPORT_DIR='/var/log/thermaldeck'
[[ ! -L "$REPORT_DIR" ]] || die "Report directory must not be a symlink: ${REPORT_DIR}"
if [[ -e "$REPORT_DIR" ]]; then
    [[ "$(stat -c %u "$REPORT_DIR")" == 0 ]] || die 'Report directory must be root-owned.'
    (( (8#$(stat -c %a "$REPORT_DIR") & 8#022) == 0 )) || die 'Report directory must not be writable by other users.'
fi
install -d -m 0755 "$REPORT_DIR"
readonly REPORT="${REPORT_DIR}/it87-install-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
printf 'commit=%s\nversion=%s\nkernel=%s\n' "$COMMIT" "$VERSION" "$KERNEL" > "$REPORT"

snapshot() {
    local chip attr value
    printf '\n[%s]\n' "$1" >> "$REPORT"
    shopt -s nullglob
    for chip in /sys/class/hwmon/hwmon*; do
        for attr in "$chip"/name "$chip"/fan*_input "$chip"/pwm[0-9] "$chip"/pwm[0-9][0-9] "$chip"/pwm*_enable; do
            [[ -f "$attr" ]] || continue
            if value=$(cat "$attr" 2>/dev/null); then
                printf '%s=%s\n' "$attr" "$value" >> "$REPORT"
            else
                printf '%s=<unavailable>\n' "$attr" >> "$REPORT"
            fi
        done
    done
    shopt -u nullglob
}

CURRENT_STATUS=$(dkms status -m it87 -v "$VERSION" -k "$KERNEL") || die 'DKMS status check failed.'
if [[ -d /sys/module/it87 ]]; then
    LOADED_VERSION=$(cat /sys/module/it87/version 2>/dev/null || true)
    if [[ "$LOADED_VERSION" == "$VERSION" && "$CURRENT_STATUS" == *': installed'* ]]; then
        snapshot 'already-installed-and-loaded'
        printf 'Pinned it87 is already installed and loaded; no changes made.\nReport: %s\n' "$REPORT"
        exit 0
    fi
    die "it87 is already loaded (version: ${LOADED_VERSION:-unknown}). Stop the controller and review/unload it before installation."
fi

snapshot 'before-install-and-load'
STAGE=$(mktemp -d /var/tmp/thermaldeck-it87.XXXXXXXX)
cleanup() { rm -rf -- "$STAGE"; }
trap cleanup EXIT
trap 'printf "Installation stopped at line %s. No automatic module removal was attempted. Report: %s\n" "$LINENO" "$REPORT" >&2' ERR

cat > "$STAGE/SHA256SUMS" <<'EOF'
564ca55892feb10b76e86f145ca7f03777ad020c62993dee170db6a83820e693  it87.c
a49cb105b9c2f7c5dad21f5c63af26e1711811ee91444783948bfb511ff1e044  compat.h
7f9ab09786229f56183600acfe693a15e89e809414650c8744ab12d67e341ede  Makefile
bf99917c92c732674807fd5160a08ceec5002ce94465f2e0281da157e8139bdd  dkms.conf
32b1062f7da84967e7019d01ab805935caa7ab7321a7ced0e30ebe75e5df1670  COPYING
EOF

printf 'Downloading reviewed it87 commit %s...\n' "$COMMIT"
for file in it87.c compat.h Makefile dkms.conf COPYING; do
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        --connect-timeout 20 --max-time 120 \
        "https://raw.githubusercontent.com/frankcrawford/it87/${COMMIT}/${file}" \
        --output "$STAGE/$file"
done
(cd "$STAGE" && sha256sum --check SHA256SUMS)
sed -i "s/^PACKAGE_VERSION=.*/PACKAGE_VERSION=\"${VERSION}\"/" "$STAGE/dkms.conf"
printf '%s\n' "$VERSION" > "$STAGE/VERSION"
printf '%s\n' "$COMMIT" > "$STAGE/SOURCE_COMMIT"

if [[ -e "$SOURCE_DIR" || -L "$SOURCE_DIR" ]]; then
    [[ -d "$SOURCE_DIR" && ! -L "$SOURCE_DIR" ]] || die "Unexpected source path: ${SOURCE_DIR}"
    [[ "$(stat -c %u "$SOURCE_DIR")" == 0 ]] || die 'Existing driver source must be root-owned.'
    (( (8#$(stat -c %a "$SOURCE_DIR") & 8#022) == 0 )) || die 'Existing source directory must not be writable by other users.'
    for file in it87.c compat.h Makefile dkms.conf COPYING VERSION SOURCE_COMMIT; do
        [[ -f "$SOURCE_DIR/$file" && ! -L "$SOURCE_DIR/$file" ]] || die "Unexpected source file: ${file}"
        [[ "$(stat -c %u "$SOURCE_DIR/$file")" == 0 ]] || die "Existing source file is not root-owned: ${file}"
        (( (8#$(stat -c %a "$SOURCE_DIR/$file") & 8#022) == 0 )) || die "Existing source file is writable by other users: ${file}"
        cmp -s "$STAGE/$file" "$SOURCE_DIR/$file" ||
            die "Existing source differs from reviewed source: ${SOURCE_DIR}/${file}"
    done
else
    install -d -m 0755 "$SOURCE_DIR"
    for file in it87.c compat.h Makefile dkms.conf COPYING VERSION SOURCE_COMMIT; do
        install -m 0644 "$STAGE/$file" "$SOURCE_DIR/$file"
    done
fi

if [[ -z "$DKMS_STATUS" ]]; then
    dkms add -m it87 -v "$VERSION"
fi
[[ "$(readlink -f "/var/lib/dkms/it87/${VERSION}/source")" == "$SOURCE_DIR" ]] ||
    die 'Existing DKMS registration points to unexpected source.'
if [[ "$CURRENT_STATUS" != *': installed'* ]]; then
    dkms build -m it87 -v "$VERSION" -k "$KERNEL"
    dkms install -m it87 -v "$VERSION" -k "$KERNEL"
fi

MODULE_PATH=$(modinfo -k "$KERNEL" -n it87)
INSTALLED_VERSION=$(modinfo -k "$KERNEL" -F version it87)
[[ "$INSTALLED_VERSION" == "$VERSION" ]] || die "Selected module version is unexpected: ${INSTALLED_VERSION}"
[[ "$MODULE_PATH" == */updates/dkms/* ]] || die "Selected module is not the DKMS override: ${MODULE_PATH}"
printf 'module_path=%s\n' "$MODULE_PATH" >> "$REPORT"

if ((LOAD_MODULE)); then
    printf 'Loading it87 with its default options (no fan-duty writes)...\n'
    modprobe it87
    [[ "$(cat /sys/module/it87/version)" == "$VERSION" ]] || die 'Loaded module version mismatch.'
    snapshot 'after-load'
    if command -v journalctl >/dev/null; then
        journalctl -k -b --no-pager -n 100 >> "$REPORT" 2>&1 || true
    fi
    printf 'Driver loaded. Sensor visibility still needs verification; this does not prove fan control.\n'
else
    printf 'Driver installed without loading. Load later with: sudo modprobe it87\n'
fi
printf 'Report: %s\nNo boot autoload file or fan curve was created.\n' "$REPORT"
