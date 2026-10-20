#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Build the stage-2 aarch64 rootfs tarball rootlessly (podman + qemu binfmt).
#   rootfs/build.sh            -> rootfs/out/stage2-YYYYMMDD.tar.gz (+ .sha256, .manifest.txt)
#   rootfs/build.sh NAME       -> rootfs/out/NAME.tar.gz
# Output is .tar.gz, not .tar.zst: the target busybox 1.36.1 has no zstd support
# (tar -z/-J/-j/--lzma only). Needs podman and qemu-user binfmt for aarch64 (see host/host-setup.sh).
# Also updates the stage2-latest.tar.gz symlink.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=${1:-stage2-$(date +%Y%m%d)}
# Fixed stable Alpine tag; STAGE2_IMAGE (or BUILD_IMAGE) overrides it. ALPINE_BRANCH (default: the
# image's vX.Y) selects the apk repository branch. 3.23, not 3.24: the lavapipe self-test of
# vk-compute-smoke segfaults under qemu-user with 3.24's mesa 26.1.
IMAGE=${STAGE2_IMAGE:-${BUILD_IMAGE:-docker.io/library/alpine:3.23}}
mkdir -p "$HERE/out"
podman run --rm --arch arm64 --pull=newer \
	-e ALPINE_BRANCH="${ALPINE_BRANCH:-}" -e ALPINE_MIRROR="${ALPINE_MIRROR:-}" \
	-v "$HERE:/recipe:ro,Z" -v "$HERE/out:/out:Z" \
	"$IMAGE" /bin/sh /recipe/build-in-container.sh "$NAME"
ln -sfn "$NAME.tar.gz" "$HERE/out/stage2-latest.tar.gz"
ln -sfn "$NAME.tar.gz.sha256" "$HERE/out/stage2-latest.tar.gz.sha256"
echo "built $HERE/out/$NAME.tar.gz"
