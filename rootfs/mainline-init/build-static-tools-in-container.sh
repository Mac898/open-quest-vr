#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Runs inside an emulated arm64 Alpine container (see build-static-tools.sh).
# Builds a static BusyBox 1.36.1 and static kexec-tools 2.0.32 from the upstream
# release tarballs (sha256-checked) and writes them to /out.
set -eu
BB_VER=1.36.1
BB_URL=https://busybox.net/downloads/busybox-$BB_VER.tar.bz2
BB_SHA256=b8cc24c9574d809e7279c3be349795c5d5ceb6fdf19ca709f80cde50e47de314
KX_VER=2.0.32
KX_URL=https://cdn.kernel.org/pub/linux/utils/kernel/kexec/kexec-tools-$KX_VER.tar.xz
KX_SHA256=8f81422a5fd2362cf6cb001b511e535565ed0f32c2f4451fb5eb68fed6710a5d
JOBS=${BUILD_JOBS:-4}
SRC=/build
OUT=/out

# Pin the apk repositories to the image's stable branch (vX.Y), never edge, unless ALPINE_BRANCH says so.
BRANCH=${ALPINE_BRANCH:-v$(cut -d. -f1,2 /etc/alpine-release)}
MIRROR=${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}
printf '%s\n' "$MIRROR/$BRANCH/main" "$MIRROR/$BRANCH/community" > /etc/apk/repositories
apk add --no-cache build-base linux-headers curl bzip2 xz perl >/dev/null
rm -rf "$SRC"; mkdir -p "$SRC"; cd "$SRC"

fetch() {  # url sha256 file
	curl -fsSL -o "$3" "$1"
	echo "$2  $3" | sha256sum -c - >/dev/null || { echo "sha256 mismatch: $3" >&2; exit 1; }
}

# ---- BusyBox ------------------------------------------------------------
fetch "$BB_URL" "$BB_SHA256" busybox.tar.bz2
tar -xjf busybox.tar.bz2
cd busybox-$BB_VER
make defconfig >/dev/null
# Overrides on top of defconfig (see busybox.config.fragment): static link, and the
# applet set of busybox-applets.txt.
while IFS= read -r line; do
	case $line in ''|'#'*) continue ;; esac
	key=${line%%=*}
	sed -i -e "/^$key=/d" -e "/^# $key is not set/d" .config
	case $line in
	*=n) echo "# $key is not set" >> .config ;;
	*) echo "$line" >> .config ;;
	esac
done < /recipe/busybox.config.fragment
yes "" | make oldconfig >/dev/null
make -j"$JOBS" busybox >/dev/null
strip -s busybox
./busybox --list | sort > "$OUT/busybox.applets"
cp busybox "$OUT/busybox"
cp .config "$OUT/busybox.config"
cd "$SRC"

# ---- kexec-tools ----------------------------------------------------------
fetch "$KX_URL" "$KX_SHA256" kexec-tools.tar.xz
tar -xJf kexec-tools.tar.xz
cd kexec-tools-$KX_VER
./configure --host=aarch64-alpine-linux-musl LDFLAGS=-static CFLAGS="-O2" >/dev/null
make -j"$JOBS" >/dev/null
strip -s build/sbin/kexec
cp build/sbin/kexec "$OUT/kexec"

{
	echo "built $(date -u +%Y-%m-%dT%H:%M:%SZ) in $(. /etc/os-release; echo "$PRETTY_NAME") $(uname -m)"
	echo "busybox $BB_VER source sha256 $BB_SHA256"
	echo "kexec-tools $KX_VER source sha256 $KX_SHA256"
	( cd "$OUT" && sha256sum busybox kexec )
	file_out=$(readelf -h "$OUT/busybox" | grep -E 'Machine|Type' | tr -s ' ' | tr '\n' ';')
	echo "busybox ELF: $file_out"
} > "$OUT/build-info.txt"
cat "$OUT/build-info.txt"
