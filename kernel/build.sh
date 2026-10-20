#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# Build the Oculus Quest 2 (hollywood) mainline kernel: apply the patch series
# in kernel/patches/ (and optionally kernel/patches-debug/) to Linux v6.18.55
# with git am, configure it with the quest2 config fragments and build Image,
# the hollywood DTB and the modules.
#
# Needs: git, make, a cross toolchain for arm64 (or LLVM=1), the usual kernel
# build dependencies (bc, bison, flex, libssl-dev, libelf-dev, ...), and for
# --fetch/--tarball network access to kernel.org. No root needed.

set -euo pipefail

BASE_TAG=v6.18.55
BASE_VERSION=6.18.55
STABLE_GIT=https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git
TARBALL_URL=https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-${BASE_VERSION}.tar.xz
DTB=qcom/sm8250-oculus-hollywood.dtb

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

usage() {
	cat <<EOF
Usage: $0 (--src DIR | --fetch DIR | --tarball DIR) [options]

Source (exactly one):
  --src DIR       existing Linux git checkout that has the tag $BASE_TAG
                  (e.g. linux-stable). Its working tree must be clean; a new
                  branch is created at $BASE_TAG and the patches are applied
                  there.
  --fetch DIR     shallow-clone tag $BASE_TAG from git.kernel.org into DIR
                  (must not exist yet).
  --tarball DIR   download linux-$BASE_VERSION.tar.xz from kernel.org, unpack
                  it into DIR (must not exist yet) and make it a git repository.

Options:
  --out DIR       output directory (default: ./out). Gets Image, the DTB,
                  .config, System.map, modules/ and the build tree build/.
  --jobs N        parallel make jobs (default: nproc).
  --debug         also apply kernel/patches-debug/ (watchdog-timing beacon)
                  and add quest2-diag.config. Diagnostic builds only.
  --branch NAME   branch to create in the source tree
                  (default: quest2-$BASE_VERSION, or quest2-$BASE_VERSION-debug).
  --no-apply      do not create a branch or apply patches; build the tree as it
                  is checked out (it must already carry the series).
  -h, --help      this text.

Toolchain (environment):
  CROSS_COMPILE   GNU cross prefix (default: aarch64-linux-gnu- unless LLVM is set)
  LLVM=1          build with clang/LLVM instead
EOF
}

die() { echo "build.sh: $*" >&2; exit 1; }

SRC= FETCH= TARBALL= OUT=$PWD/out JOBS=$(nproc 2>/dev/null || echo 4)
DEBUG=0 BRANCH= APPLY=1

while [ $# -gt 0 ]; do
	case "$1" in
	--src) SRC=${2:?}; shift 2 ;;
	--fetch) FETCH=${2:?}; shift 2 ;;
	--tarball) TARBALL=${2:?}; shift 2 ;;
	--out) OUT=${2:?}; shift 2 ;;
	--jobs|-j) JOBS=${2:?}; shift 2 ;;
	--debug) DEBUG=1; shift ;;
	--branch) BRANCH=${2:?}; shift 2 ;;
	--no-apply) APPLY=0; shift ;;
	-h|--help) usage; exit 0 ;;
	*) usage >&2; die "unknown argument: $1" ;;
	esac
done

n=0
[ -n "$SRC" ] && n=$((n + 1))
[ -n "$FETCH" ] && n=$((n + 1))
[ -n "$TARBALL" ] && n=$((n + 1))
[ "$n" -eq 1 ] || { usage >&2; die "give exactly one of --src, --fetch, --tarball"; }
case "$JOBS" in ''|*[!0-9]*) die "--jobs needs a number" ;; esac

if [ -z "$BRANCH" ]; then
	BRANCH=quest2-$BASE_VERSION
	[ "$DEBUG" -eq 1 ] && BRANCH=$BRANCH-debug
fi

# git am needs a committer identity; use a neutral one only if none is set.
git_am() {
	if git -C "$SRC" config user.email >/dev/null 2>&1; then
		git -C "$SRC" am "$@"
	else
		git -C "$SRC" -c user.name="quest2 build" -c user.email="build@localhost" \
			am "$@"
	fi
}

# --- get the source --------------------------------------------------------

if [ -n "$FETCH" ]; then
	[ -e "$FETCH" ] && die "$FETCH already exists"
	echo ">> cloning $BASE_TAG from $STABLE_GIT into $FETCH"
	git clone --depth 1 --branch "$BASE_TAG" "$STABLE_GIT" "$FETCH"
	SRC=$FETCH
elif [ -n "$TARBALL" ]; then
	[ -e "$TARBALL" ] && die "$TARBALL already exists"
	mkdir -p "$TARBALL"
	echo ">> downloading $TARBALL_URL"
	if command -v curl >/dev/null 2>&1; then
		curl -fL "$TARBALL_URL" | tar -xJ --strip-components=1 -C "$TARBALL"
	else
		wget -O- "$TARBALL_URL" | tar -xJ --strip-components=1 -C "$TARBALL"
	fi
	git -C "$TARBALL" init -q
	git -C "$TARBALL" add -A
	git -C "$TARBALL" -c user.name="quest2 build" -c user.email="build@localhost" \
		commit -q -m "Linux $BASE_VERSION (from $TARBALL_URL)"
	git -C "$TARBALL" tag "$BASE_TAG"
	SRC=$TARBALL
fi

SRC=$(cd "$SRC" && pwd)
[ -f "$SRC/Makefile" ] && [ -d "$SRC/arch/arm64" ] || die "$SRC is not a Linux source tree"
git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1 || die "$SRC is not a git repository"

# --- apply the series --------------------------------------------------------

apply_series() {
	local dir=$1 p
	[ -f "$dir/series" ] || die "$dir/series not found"
	while read -r p; do
		case "$p" in ''|'#'*) continue ;; esac
		[ -f "$dir/$p" ] || die "$dir/$p not found"
		git_am "$dir/$p" || die "git am failed on $p (run 'git -C $SRC am --abort' to undo)"
	done < "$dir/series"
}

if [ "$APPLY" -eq 1 ]; then
	[ -z "$(git -C "$SRC" status --porcelain --untracked-files=no)" ] ||
		die "$SRC has uncommitted changes"
	git -C "$SRC" rev-parse -q --verify "refs/tags/$BASE_TAG^{commit}" >/dev/null ||
		die "$SRC has no tag $BASE_TAG (git fetch it from $STABLE_GIT)"
	git -C "$SRC" rev-parse -q --verify "refs/heads/$BRANCH" >/dev/null &&
		die "branch $BRANCH already exists in $SRC (use --branch NAME, or --no-apply to build it)"
	echo ">> creating branch $BRANCH at $BASE_TAG in $SRC"
	git -C "$SRC" checkout -q -b "$BRANCH" "$BASE_TAG"
	echo ">> applying kernel/patches"
	apply_series "$HERE/patches"
	if [ "$DEBUG" -eq 1 ]; then
		echo ">> applying kernel/patches-debug"
		apply_series "$HERE/patches-debug"
	fi
fi

[ -f "$SRC/arch/arm64/configs/quest2.config" ] ||
	die "$SRC does not carry the quest2 series (arch/arm64/configs/quest2.config missing)"
if [ "$DEBUG" -eq 1 ]; then
	[ -f "$SRC/arch/arm64/configs/quest2-diag.config" ] ||
		die "--debug, but $SRC does not carry kernel/patches-debug"
fi

# --- configure and build -----------------------------------------------------

FRAGMENTS="quest2.config"
[ "$DEBUG" -eq 1 ] && FRAGMENTS="$FRAGMENTS quest2-diag.config"
FRAGMENTS="$FRAGMENTS quest2-iter.config quest2-gpu.config quest2-smmu.config"

mkdir -p "$OUT/build"
OUT=$(cd "$OUT" && pwd)

MAKEARGS=(-C "$SRC" O="$OUT/build" ARCH=arm64 -j"$JOBS")
if [ -n "${LLVM:-}" ]; then
	MAKEARGS+=(LLVM="$LLVM")
	[ -n "${CROSS_COMPILE:-}" ] && MAKEARGS+=(CROSS_COMPILE="$CROSS_COMPILE")
else
	MAKEARGS+=(CROSS_COMPILE="${CROSS_COMPILE:-aarch64-linux-gnu-}")
fi

echo ">> configuring: defconfig $FRAGMENTS"
# shellcheck disable=SC2086 # FRAGMENTS is a list of make targets
make "${MAKEARGS[@]}" defconfig $FRAGMENTS

echo ">> building Image, $DTB and modules"
make "${MAKEARGS[@]}" Image "$DTB" modules

rm -rf "$OUT/modules"
make "${MAKEARGS[@]}" INSTALL_MOD_PATH="$OUT/modules" INSTALL_MOD_STRIP=1 modules_install

cp "$OUT/build/arch/arm64/boot/Image" "$OUT/Image"
cp "$OUT/build/arch/arm64/boot/dts/$DTB" "$OUT/"
cp "$OUT/build/.config" "$OUT/config"
cp "$OUT/build/System.map" "$OUT/System.map"

release=$(make -s "${MAKEARGS[@]}" kernelrelease 2>/dev/null || true)
echo
echo ">> done: $release"
echo "   $OUT/Image"
echo "   $OUT/$(basename "$DTB")"
echo "   $OUT/modules/lib/modules/$release"
