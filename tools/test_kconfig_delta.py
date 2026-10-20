#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for tools/kconfig_delta.py.

Run: python3 -m unittest tools/test_kconfig_delta.py   (or python3 -I tools/test_kconfig_delta.py)
"""
import importlib.util
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("kconfig_delta", os.path.join(HERE, "kconfig_delta.py"))
kd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kd)


class KconfigDelta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kconfig-delta-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_report_and_exit_codes(self):
        k1 = self.write("k1.config", "CONFIG_FOO=y\n# CONFIG_KEXEC is not set\nCONFIG_LOG_BUF_SHIFT=17\n")
        k2 = self.write("k2.config", "CONFIG_FOO=y\nCONFIG_KEXEC=y\nCONFIG_LOG_BUF_SHIFT=18\n")
        r = kd.report(k1, k2)
        self.assertIn("2 symbol(s) changed, 1 flagged", r[0])
        self.assertTrue(any("FLAG CONFIG_KEXEC: n -> y" in l for l in r))
        self.assertEqual(kd.main([k1, k1]), 0)
        self.assertEqual(kd.main([k1, k2]), 1)

    def test_cmdline_and_clock_flagged(self):
        self.assertEqual(kd.flags_for("CONFIG_CMDLINE"), ["cmdline"])
        self.assertIn("clock", kd.flags_for("CONFIG_SM_GPUCC_8250"))
        self.assertIn("clock", kd.flags_for("CONFIG_COMMON_CLK_QCOM"))
        self.assertEqual(kd.flags_for("CONFIG_LOG_BUF_SHIFT"), [])

    def test_reset_panic_flagged(self):
        for sym in ("CONFIG_QUEST2_WARM_RESET", "CONFIG_POWER_RESET_QCOM_PON", "CONFIG_POWER_RESET_MSM",
                    "CONFIG_QCOM_WDT", "CONFIG_PANIC_TIMEOUT", "CONFIG_PANIC_ON_OOPS", "CONFIG_DETECT_HUNG_TASK",
                    "CONFIG_SOFTLOCKUP_DETECTOR", "CONFIG_BOOTPARAM_SOFTLOCKUP_PANIC", "CONFIG_QCOM_PON",
                    "CONFIG_INPUT_PM8941_PWRKEY", "CONFIG_MAGIC_SYSRQ", "CONFIG_RESET_CONTROLLER",
                    "CONFIG_ARM_PSCI_FW_RESTART"):
            self.assertIn("reset-panic", kd.flags_for(sym), sym)
        self.assertEqual(kd.flags_for("CONFIG_LOG_BUF_SHIFT"), [])
        self.assertNotIn("reset-panic", kd.flags_for("CONFIG_PRINTK"))

    def test_storage_block_word_matching(self):
        # BLOCK matches as a whole word part only, not inside DETECT_HUNG_TASK_BLOCKER.
        self.assertEqual(kd.flags_for("CONFIG_DETECT_HUNG_TASK_BLOCKER"), ["reset-panic"])
        for sym in ("CONFIG_BLOCK", "CONFIG_BLOCK_LEGACY_AUTOLOAD", "CONFIG_BLOCK_HOLDER_DEPRECATED",
                    "CONFIG_MTD_BLOCK", "CONFIG_BLK_DEV_INITRD", "CONFIG_SCSI_UFSHCD", "CONFIG_MMC_BLOCK_MINORS",
                    "CONFIG_DM_CRYPT"):
            self.assertIn("storage", kd.flags_for(sym), sym)
        self.assertNotIn("storage", kd.flags_for("CONFIG_FOO_BLOCKER"))
        self.assertNotIn("storage", kd.flags_for("CONFIG_UNBLOCKED_X"))


if __name__ == "__main__":
    unittest.main()
