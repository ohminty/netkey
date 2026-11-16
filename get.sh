#!/usr/bin/env bash
# NetKey quick install: downloads the latest release installer from GitHub and runs it.
#   curl -fsSL https://raw.githubusercontent.com/ohminty/netkey/main/get.sh | sudo bash
#   ... | sudo bash -s -- --hostname netkey-attic          installer options go after "--"
#   ... | sudo NETKEY_VERSION=0.10.3 bash                   a specific release instead of the latest
#   ... | sudo NETKEY_URL=https://example/netkey-install.sh bash    the installer from another server
# Running it again updates the box to the latest release; settings, users and history are kept.
# Everything sits in main(), called on the last line: if the download of this script breaks off, nothing runs.
set -euo pipefail

REPO=ohminty/netkey
ASSET=netkey-install.sh
tmp=""

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

main() {
    [[ $EUID -eq 0 ]] || die "run as root: curl -fsSL https://raw.githubusercontent.com/$REPO/main/get.sh | sudo bash"
    command -v curl >/dev/null || die "curl is missing: sudo apt install curl"
    local url
    if [[ -n ${NETKEY_URL:-} ]]; then
        url=$NETKEY_URL
    elif [[ -n ${NETKEY_VERSION:-} ]]; then
        [[ $NETKEY_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || die "bad NETKEY_VERSION: $NETKEY_VERSION"
        url="https://github.com/$REPO/releases/download/v$NETKEY_VERSION/$ASSET"
    else
        url="https://github.com/$REPO/releases/latest/download/$ASSET"
    fi
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    printf '\033[1m==>\033[0m Downloading %s\n' "$url"
    curl -fL --proto '=https' --tlsv1.2 --retry 3 --connect-timeout 15 --progress-bar -o "$tmp/$ASSET" "$url" ||
        die "download failed: check the Pi's internet access and clock, then run the command again"
    head -c 2 "$tmp/$ASSET" | grep -q '^#!' || die "the download is not a NetKey installer: $url"
    # The installer checks its own payload checksum, so a damaged download stops there.
    bash "$tmp/$ASSET" "$@" </dev/null
}

main "$@"
