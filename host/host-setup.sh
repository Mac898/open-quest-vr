#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# One-time host setup for building and talking to the headset (Ubuntu/Debian). Needs root, so
# you run it yourself, explicitly:
#
#     sudo bash host/host-setup.sh [--project DIR] [--owner USER] [--nfs] [--nfs-dir DIR]
#
#   --project DIR   the repository checkout (default: the parent of host/)
#   --owner USER    the user that runs the build tools (default: $SUDO_USER; required if
#                   run as plain root). Gets subordinate uid/gid ranges for rootless podman
#   --nfs           also export an NFS work share to the USB link (off by default)
#   --nfs-dir DIR   directory to export with --nfs (default: PROJECT/nfs/work)
#
# Installs: qemu-user + binfmt (arm64 containers), podman, lz4, and with --nfs the NFS server.
# The USB link subnet (10.42.0.0/24) is the NetworkManager "shared" connection used by
# quest-kexec and the mainline init; this script does not create it.
# Idempotent. Review it before running: it installs packages and, with --nfs, edits
# /etc/exports.d and the firewall.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT=$(cd "$HERE/.." && pwd)
OWNER=${SUDO_USER:-}
USB_SUBNET=10.42.0.0/24
NFS=0
NFS_DIR=

while [[ $# -gt 0 ]]; do
    case $1 in
        --project) PROJECT=$(cd "$2" && pwd); shift 2 ;;
        --owner)   OWNER=$2; shift 2 ;;
        --nfs)     NFS=1; shift ;;
        --nfs-dir) NFS_DIR=$2; shift 2 ;;
        -h|--help) sed -n '3,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
NFS_DIR=${NFS_DIR:-$PROJECT/nfs/work}

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ -n $OWNER && $OWNER != root ]] || { echo "cannot tell which user to set up: pass --owner USER" >&2; exit 1; }
id "$OWNER" >/dev/null || { echo "no such user: $OWNER" >&2; exit 1; }

PKGS=(qemu-user qemu-user-binfmt podman uidmap lz4)
(( NFS )) && PKGS+=(nfs-kernel-server)
echo "== packages: ${PKGS[*]}"
apt-get update
apt-get install -y "${PKGS[@]}"

echo "== binfmt: aarch64 handler (lets rootless podman run arm64 containers)"
systemctl restart systemd-binfmt || true
ls /proc/sys/fs/binfmt_misc/ | grep -q aarch64 && echo "aarch64 binfmt OK" \
    || echo "WARNING: no aarch64 binfmt entry registered" >&2

echo "== rootless podman: subordinate ids for $OWNER"
grep -q "^$OWNER:" /etc/subuid || usermod --add-subuids 100000-165535 "$OWNER"
grep -q "^$OWNER:" /etc/subgid || usermod --add-subgids 100000-165535 "$OWNER"

if (( NFS )); then
    echo "== NFS work share $NFS_DIR (owned by $OWNER; headset root is squashed to $OWNER)"
    install -d -o "$OWNER" -g "$(id -gn "$OWNER")" "$NFS_DIR"
    install -d -m 755 /etc/exports.d
    cat >/etc/exports.d/quest-vr.exports <<EXPORTS
$NFS_DIR $USB_SUBNET(rw,sync,no_subtree_check,all_squash,anonuid=$(id -u "$OWNER"),anongid=$(id -g "$OWNER"))
EXPORTS
    exportfs -ra
    systemctl enable --now nfs-server
    exportfs -v | grep -F "$NFS_DIR"

    echo "== firewall: NFSv4 from the USB link only"
    if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
        ufw allow from "$USB_SUBNET" to any port 2049 proto tcp comment 'quest nfs'
    fi
fi

echo "done"
