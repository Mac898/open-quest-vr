#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for tools/dtb_delta.py (needs `dtc`; skipped without it).

Run: python3 -m unittest tools/test_dtb_delta.py   (or python3 -I tools/test_dtb_delta.py)
"""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dtb_delta", os.path.join(HERE, "dtb_delta.py"))
dd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dd)

DTS = """/dts-v1/;
/ { compatible = "t"; #address-cells = <1>; #size-cells = <1>;
  soc { #address-cells = <1>; #size-cells = <1>;
    gpu@3d00000 { reg = <0x3d00000 0x1000>; status = "okay"; };
    regulator@1 { reg = <0x1 0x1>; regulator-min-microvolt = <%d>; };
    chosen_stub { val = <1>; };
  };
};
"""

# An added node with a regulator supply, a voltage and a download-mode property,
# and a changed /memreserve/ entry.
ADDED_A = """/dts-v1/;
/memreserve/ 0x80000000 0x1000;
/ {
	soc {
		foo@1000 { compatible = "x,foo"; status = "okay"; };
	};
};
"""
ADDED_B = """/dts-v1/;
/memreserve/ 0x90000000 0x2000;
/ {
	soc {
		foo@1000 { compatible = "x,foo"; status = "okay"; };
		bar@2000 { compatible = "x,bar"; vdd-supply = <1>; regulator-min-microvolt = <3300000>; qcom,download-mode = <1>; };
	};
};
"""


@unittest.skipUnless(shutil.which("dtc"), "dtc not installed")
class DtbDelta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dtb-delta-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def dtb(self, name, text):
        src, out = os.path.join(self.tmp, name + ".dts"), os.path.join(self.tmp, name + ".dtb")
        with open(src, "w") as f:
            f.write(text)
        subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", out, src], check=True, timeout=30)
        return out

    def test_disable_only_is_remove_only(self):
        a = self.dtb("a", DTS % 1800000)
        b = self.dtb("b", (DTS % 1800000).replace('status = "okay"', 'status = "disabled"'))
        r = dd.report(a, b)
        self.assertIn("remove-only: yes", r[0])
        self.assertIn("0 flagged", r[0])

    def test_regulator_change_flagged_and_exit_codes(self):
        a = self.dtb("a", DTS % 1800000)
        c = self.dtb("c", DTS % 1900000)
        r = dd.report(a, c)
        self.assertIn("remove-only: no", r[0])
        self.assertTrue(any(l.startswith("  FLAG") and "regulator" in l for l in r[1:]))
        self.assertEqual(dd.main([a, c]), 1)
        self.assertEqual(dd.main([a, a]), 0)
        self.assertEqual(dd.main([a]), 2)

    def test_added_node_properties_and_memreserve(self):
        a, b = self.dtb("a", ADDED_A), self.dtb("b", ADDED_B)
        r = dd.report(a, b)
        body = "\n".join(r[1:])
        self.assertEqual(dd.main([a, b]), 1)
        for prop, fl in (("vdd-supply", "regulator"), ("regulator-min-microvolt", "regulator"),
                         ("qcom,download-mode", "secure")):
            self.assertTrue(any(l.startswith("  FLAG node-added") and f"/soc/bar@2000 {prop}:" in l and fl in l
                                for l in r[1:]), prop)
        self.assertIn(' /soc/bar@2000 compatible: (absent) -> "x,bar"', body)
        self.assertEqual(sum(1 for l in r[1:] if l.startswith("  FLAG memreserve") and "reserved-memory" in l), 2)
        self.assertIn("0x80000000 0x1000: present -> (absent)", body)
        self.assertIn("0x90000000 0x2000: (absent) -> present", body)

    def test_removed_node_properties_listed(self):
        a, b = self.dtb("a", ADDED_A), self.dtb("b", ADDED_B)
        r = dd.report(b, a)
        self.assertTrue(any(l.startswith("  FLAG node-removed") and "regulator-min-microvolt" in l for l in r[1:]))

    def test_removing_disabled_node_is_remove_only(self):
        dis = ADDED_B.replace("qcom,download-mode = <1>;", 'qcom,download-mode = <1>; status = "disabled";')
        c = self.dtb("c", dis)
        a2 = self.dtb("a2", ADDED_A.replace("0x80000000 0x1000", "0x90000000 0x2000"))
        r = dd.report(c, a2)
        self.assertIn("remove-only: yes", r[0])
        self.assertTrue(any("FLAG disable" in l and "vdd-supply" in l for l in r[1:]))


if __name__ == "__main__":
    unittest.main()
