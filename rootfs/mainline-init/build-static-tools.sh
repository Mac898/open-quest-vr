#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Build the two static aarch64 tools the initramfs needs (BusyBox and kexec-tools)
# rootlessly in an emulated arm64 Alpine container (podman + qemu binfmt).
#
#   rootfs/mainline-init/build-static-tools.sh [OUTDIR]
#
# Output (default rootfs/out/static/): busybox, kexec, busybox.applets (the built
# `busybox --list`), build-info.txt (versions, tarball and binary sha256). These are
# the defaults of build.py's --busybox and --kexec.
#
# Env: BUILD_IMAGE   container image (default docker.io/library/alpine:3.24, a fixed stable tag)
#      BUILD_JOBS    make -j (default 4)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:-$HERE/../out/static}
IMAGE=${BUILD_IMAGE:-docker.io/library/alpine:3.24}
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
podman run --rm --arch arm64 --pull=newer \
	-e BUILD_JOBS="${BUILD_JOBS:-4}" -e ALPINE_BRANCH="${ALPINE_BRANCH:-}" \
	-v "$HERE:/recipe:ro,Z" -v "$OUT:/out:Z" \
	"$IMAGE" /bin/sh /recipe/build-static-tools-in-container.sh
echo "built static tools in $OUT:"
ls -l "$OUT"
