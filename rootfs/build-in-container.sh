#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Runs inside an emulated arm64 Alpine container (stable tag, see build.sh) (see build.sh).
# Builds a clean rootfs in /build with apk --root, strips storage-writing tools,
# and packs /out/<name>.tar.gz plus a manifest and sha256.
# Usage: build-in-container.sh <name>
set -eu
NAME=$1
ROOT=/build
OUT=/out
MIRROR=${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}
# Repository branch pinned to the image's release (vX.Y), not edge; ALPINE_BRANCH overrides.
BRANCH=${ALPINE_BRANCH:-v$(cut -d. -f1,2 /etc/alpine-release)}

cat > /etc/apk/repositories <<EOF
$MIRROR/$BRANCH/main
$MIRROR/$BRANCH/community
EOF
# GNU tar writes plain ustar/GNU headers that busybox 1.36.1 tar extracts.
apk add --no-cache tar gzip >/dev/null

rm -rf "$ROOT"
mkdir -p "$ROOT/etc/apk"
cp -a /etc/apk/keys "$ROOT/etc/apk/"
cp /etc/apk/repositories "$ROOT/etc/apk/"
PKGS=$(grep -v '^#' /recipe/packages.txt | tr '\n' ' ')
apk --root "$ROOT" --initdb --no-cache --arch aarch64 add $PKGS

# Tools that can write hardware state are not shipped at all (busybox applet
# links included). This keeps the stage-2 rootfs from carrying them; it is not a
# complete guard on its own: tools/dev's denylist screens every command sent to the
# headset, and the initramfs busybox still has some of these applets.
STRIP='
mkfs mkfs.* mke2fs mkswap mkdosfs fdisk sfdisk cfdisk gdisk sgdisk parted
partx partprobe addpart delpart resizepart blkdiscard blkzone blkpr wipefs
tune2fs resize2fs fsck fsck.* e2fsck hdparm losetup swapon swapoff swaplabel
blockdev fsfreeze fstrim shred eject raidautorun nbd-client
flash_erase flash_eraseall flashcp nandwrite nanddump mtd_debug ubiformat fastboot
i2cset i2ctransfer setpci pcilmr
setserial microcom ldattach slattach
watchdog wdctl
fbset fbsplash vkcube vkcubepp
rtcwake hwclock chmem ctrlaltdel switch_root pivot_root
'
# Rationale per line: block devices / flash; I2C (PMIC, charger, fuel gauge)
# and PCI config writes; serial line tools (the display BLU MCU is ttyHS13);
# opening /dev/watchdog can arm a hardware watchdog; framebuffer modesets
# (display/SMMU hazard; vkcube/vkcubepp from vulkan-tools can drive a display through
# VK_KHR_display); RTC/suspend/memory-offline/reset/root switching.
removed=
for d in bin sbin usr/bin usr/sbin; do
	for pat in $STRIP; do
		for f in "$ROOT/$d"/$pat; do
			[ -e "$f" ] || [ -L "$f" ] || continue
			rm -f "$f"
			removed="$removed /$d/${f##*/}"
		done
	done
done

# Headless Vulkan compute smoke test (rootfs/vkcompute). Built in this build container (its
# compilers and the lavapipe used by --test are installed here, never into $ROOT); --test runs it
# once on lavapipe and checks that the default mode refuses a non-Qualcomm device.
mkdir -p "$ROOT/usr/local/bin"
sh /recipe/vkcompute/build.sh /recipe/vkcompute "$ROOT/usr/local/bin/vk-compute-smoke" --test

echo quest2-stage2 > "$ROOT/etc/hostname"
cat > "$ROOT/etc/profile.d/stage2.sh" <<'EOF'
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PS1='stage2:\w# '
EOF
rm -rf "$ROOT/var/cache/apk"/* "$ROOT/tmp"/* 2>/dev/null || true

{
	echo "# $NAME  built $(date -u +%Y-%m-%dT%H:%M:%SZ) from $MIRROR/$BRANCH"
	echo "# removed (storage-writing tools):$removed"
	echo "# /usr/local/bin/vk-compute-smoke sha256 $(sha256sum "$ROOT/usr/local/bin/vk-compute-smoke" | cut -d' ' -f1)"
	apk --root "$ROOT" info -v 2>/dev/null | sort
} > "$OUT/$NAME.manifest.txt"

tar --numeric-owner --format=gnu --sort=name -C "$ROOT" -cf - . | gzip -6 > "$OUT/$NAME.tar.gz.part"
mv "$OUT/$NAME.tar.gz.part" "$OUT/$NAME.tar.gz"
(cd "$OUT" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
du -sh "$ROOT" | sed 's/^/unpacked: /'
ls -l "$OUT/$NAME.tar.gz"
echo "removed:$removed"
