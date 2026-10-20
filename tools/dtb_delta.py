#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""dtc-normalised DTB delta: what changed between two device trees, risky changes flagged. Host only.

    python3 -I tools/dtb_delta.py OLD.dtb NEW.dtb    exit 0 = nothing flagged, 1 = flagged changes, 2 = error

Both blobs go through `dtc -I dtb -O dts -s` (sorted, so node order is irrelevant) and are compared as
{node path: {property: value}}. Each added/removed node, property and /memreserve/ entry is one line:
  disable      status became "disabled", or a disabled node was removed
  enable       status became "okay", or the node was added enabled
  node-added   a node added, plus one line per property
  node-removed an enabled node removed, plus one line per property
  memreserve   a /memreserve/ entry added or removed
  property     any other property change
A change is FLAGGED when the node path or property name touches regulators and supplies, clocks, IOMMU/SMMU,
storage (ufs, sdhc, mmc), SWD/syncboss/MCU, reserved-memory/no-map, SCM/download mode/qfprom/SDAM,
PMIC/charger/fuel gauge/PON, watchdog, OPP/cpufreq/thermal, reserved GPIO ranges, firmware, or /chosen
bootargs. The tool never clears a flag; a person judges it. phandle values are dropped (renumbering would be
noise). The summary says whether the delta is remove-only (every change is `disable`).
"""
import re
import subprocess
import sys

FLAGS = [
    ("regulator", re.compile(r"regulator|vreg|-supply$|^vdd|^vcc|microvolt|microamp", re.I)),
    ("clock", re.compile(r"clock|^clocks$|assigned-clock|clk", re.I)),
    ("iommu", re.compile(r"iommu|smmu|^iommus$", re.I)),
    ("storage", re.compile(r"ufs|sdhc|mmc|nand|spi-nor|flash", re.I)),
    ("mcu", re.compile(r"swd|syncboss|nrf|mcu@|oculus,blu|ttyHS13", re.I)),
    ("reserved-memory", re.compile(r"reserved-memory|no-map|^memory@|memory-region", re.I)),
    ("secure", re.compile(r"scm|dload|download|qfprom|efuse|sdam|tz|secure", re.I)),
    ("power", re.compile(r"pmic|charger|fuel|bq27|battery|smb\d|pon|qpnp|spmi", re.I)),
    ("watchdog", re.compile(r"watchdog|wdt", re.I)),
    ("thermal-cpufreq", re.compile(r"opp|cpufreq|thermal|trip|cooling", re.I)),
    ("gpio-reserved", re.compile(r"gpio-reserved-ranges", re.I)),
    ("firmware", re.compile(r"firmware|firmware-name|memory-region", re.I)),
    ("cmdline", re.compile(r"^bootargs$")),
]
DROP = {"phandle", "linux,phandle"}


def decompile(path):
    p = subprocess.run(["dtc", "-q", "-I", "dtb", "-O", "dts", "-s", path], capture_output=True, text=True,
                       timeout=60)
    if p.returncode:
        raise RuntimeError(f"dtc failed on {path}: {p.stderr.strip()[:200]}")
    return p.stdout


MEMRESERVE = "/memreserve/"                # pseudo node path for the DTB header's memory reservation map


def parse(dts):
    """{node path: {prop: value text}} from dtc's sorted output (one statement per line). The
    /memreserve/ entries become the pseudo node MEMRESERVE with one property per `address size` entry."""
    nodes, stack = {MEMRESERVE: {}}, []
    for raw in dts.splitlines():
        line = raw.strip()
        m = re.match(r"^/memreserve/\s+(\S+)\s+(\S+);$", line)
        if m:
            nodes[MEMRESERVE][f"{int(m.group(1), 0):#x} {int(m.group(2), 0):#x}"] = "<reserved>"
            continue
        if not line or line.startswith(("/dts-v1/", "//")):
            continue
        m = re.match(r"^(?:[\w,.+-]+:\s*)*([^\s{]+)\s*\{$", line)
        if m:
            name = m.group(1)
            stack.append(name)
            path = "/" if name == "/" else "/" + "/".join(x for x in stack if x != "/")
            nodes.setdefault(path, {})
            continue
        if line == "};":
            stack.pop()
            continue
        m = re.match(r"^([^\s=;]+)\s*(?:=\s*(.*))?;$", line)
        if m and stack:
            path = "/" if stack == ["/"] else "/" + "/".join(x for x in stack if x != "/")
            if m.group(1) not in DROP:
                nodes[path][m.group(1)] = m.group(2) if m.group(2) is not None else "<true>"
    return nodes


def flags_for(path, prop=None):
    if path == MEMRESERVE:
        return ["reserved-memory"]
    out = []
    for name, rx in FLAGS:
        if rx.search(path) or (prop and rx.search(prop)):
            out.append(name)
    return out


def changes(old, new):
    """[(kind, path, prop, old, new, flags)]"""
    out = []
    for path in sorted(set(old) | set(new)):
        if path == MEMRESERVE:
            for e in sorted(set(old[path]) ^ set(new[path])):
                out.append(("memreserve", path, e, "present" if e in old[path] else "(absent)",
                            "present" if e in new[path] else "(absent)", flags_for(path)))
            continue
        if path not in new:
            # every property of a removed node is listed and flagged on its own (a node that was
            # already disabled keeps the `disable` kind, so a pure removal of disabled nodes is still remove-only)
            kind = "disable" if old[path].get("status") == '"disabled"' else "node-removed"
            out.append((kind, path, None, "present", "absent", flags_for(path)))
            out += [(kind, path, prop, v, "(absent)", flags_for(path, prop)) for prop, v in sorted(old[path].items())]
            continue
        if path not in old:
            # an added node is never one opaque line: each property is listed and flagged
            out.append(("node-added", path, None, "absent", "present", flags_for(path)))
            out += [("node-added", path, prop, "(absent)", v, flags_for(path, prop)) for prop, v in sorted(new[path].items())]
            continue
        for prop in sorted(set(old[path]) | set(new[path])):
            a, b = old[path].get(prop), new[path].get(prop)
            if a == b:
                continue
            if prop == "status":
                kind = "disable" if b == '"disabled"' else "enable"
            else:
                kind = "property"
            out.append((kind, path, prop, a if a is not None else "(absent)", b if b is not None else "(absent)",
                        flags_for(path, prop) if kind != "disable" else []))
    return out


def report(old_path, new_path):
    """Report lines (raises RuntimeError if dtc fails)."""
    ch = changes(parse(decompile(old_path)), parse(decompile(new_path)))
    flagged = [c for c in ch if c[5]]
    remove_only = bool(ch) and all(c[0] == "disable" for c in ch)
    lines = [f"dtb_delta: {old_path} -> {new_path}: {len(ch)} change(s), {len(flagged)} flagged; "
             f"remove-only: {'yes' if remove_only else 'no'}"]
    for kind, path, prop, a, b, fl in ch:
        where = path + (f" {prop}" if prop else "")
        lines.append(f"  {'FLAG ' if fl else '     '}{kind:12} {where}: {a} -> {b}"
                     + (f"   [{', '.join(fl)}]" if fl else ""))
    return lines


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    try:
        lines = report(argv[0], argv[1])
    except (OSError, RuntimeError) as e:
        print(f"dtb_delta: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 1 if any(l.startswith("  FLAG") for l in lines[1:]) else 0


if __name__ == "__main__":
    sys.exit(main())
