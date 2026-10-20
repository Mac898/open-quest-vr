#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Build vk-compute-smoke inside the arm64 Alpine build container (called by
# rootfs/build-in-container.sh). Usage: build.sh SRC_DIR OUT_BINARY [--test]
# --test: also run it once on lavapipe (mesa-vulkan-swrast, installed in the build container only,
# never in the stage-2 rootfs) with --any-device, and check the default mode refuses a CPU device.
set -eu
SRC=$1; OUT=$2; TEST=${3:-}
apk add --no-cache build-base vulkan-loader-dev vulkan-headers glslang >/dev/null
W=$(mktemp -d)
glslangValidator -V --target-env vulkan1.1 --vn smoke_spv -o "$W/smoke_spv.h" "$SRC/smoke.comp" >/dev/null
cc -O2 -Wall -Wextra -Werror -std=c11 -D_POSIX_C_SOURCE=200809L -I"$W" -o "$OUT" "$SRC/vk_compute_smoke.c" -lvulkan
strip "$OUT"
if [ "$TEST" = --test ]; then
	apk add --no-cache mesa-vulkan-swrast >/dev/null
	"$OUT" --any-device --n 65536
	if "$OUT" --n 4096; then echo "vk-compute-smoke: default mode accepted a non-Qualcomm device"; exit 1; fi
	"$OUT" --n 1 && { echo "vk-compute-smoke: bad --n accepted"; exit 1; } || true
	apk del mesa-vulkan-swrast >/dev/null
fi
rm -rf "$W"
