# encoding: utf-8
"""Tests for FreeFileSync integration and audit/hardening (sa_ffs)."""

import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))

import sa_ffs

SAMPLE_FFS_XML = """<?xml version="1.0" encoding="utf-8"?>
<FreeFileSync XmlType="GUI" XmlVersion="19">
    <MainConfig>
        <Comparison>
            <Variant>TimeAndSize</Variant>
        </Comparison>
        <SyncConfig>
            <Variant>Update</Variant>
        </SyncConfig>
        <Filter>
            <Include>
                <Item>*</Item>
            </Include>
            <Exclude>
                <Item>\\System Volume Information\\</Item>
                <Item>\\$Recycle.Bin\\</Item>
                <Item>*.tmp</Item>
            </Exclude>
            <TimeSpan Type="None">0</TimeSpan>
            <SizeMin Unit="None">0</SizeMin>
            <SizeMax Unit="None">0</SizeMax>
        </Filter>
        <FolderPairs>
            <Pair>
                <Left>V:\\___VAC\\__K</Left>
                <Right>R:\\__SAVE_R\\__K</Right>
            </Pair>
        </FolderPairs>
    </MainConfig>
</FreeFileSync>
"""


class TestSecureAppsFFS(unittest.TestCase):
    """Test FFS audit, hardening, idempotency, and restoration."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="sa_test_ffs_")
        self.ffs_path = os.path.join(self.temp_dir, "test_sync.ffs_gui")
        self.batch_path = os.path.join(self.temp_dir, "test_sync.ffs_batch")
        with open(self.ffs_path, "w", encoding="utf-8") as f:
            f.write(SAMPLE_FFS_XML)
        with open(self.batch_path, "w", encoding="utf-8") as f:
            f.write(SAMPLE_FFS_XML)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_audit_detects_unhardened(self):
        report = sa_ffs.audit_ffs_configuration(gui_path=self.ffs_path, batch_path=self.batch_path)
        self.assertFalse(report.get("overall_safe"))
        gui_details = report["details"]["gui"]
        self.assertTrue(gui_details["exists"])
        self.assertFalse(gui_details["store_excluded"])
        self.assertFalse(gui_details["safe"])

    def test_harden_applies_exclusions_and_creates_backup(self):
        h_res = sa_ffs.apply_ffs_hardening(gui_path=self.ffs_path, batch_path=self.batch_path)
        self.assertTrue(h_res.get("ok"))
        self.assertEqual(len(h_res.get("modified")), 2)
        self.assertTrue(os.path.isfile(self.ffs_path + ".bak"))

        # Audit again - should now be safe
        report = sa_ffs.audit_ffs_configuration(gui_path=self.ffs_path, batch_path=self.batch_path)
        self.assertTrue(report.get("overall_safe"))
        gui_details = report["details"]["gui"]
        self.assertTrue(gui_details["safe"])
        self.assertTrue(gui_details["store_excluded"])
        self.assertTrue(gui_details["svault_included"])
        self.assertTrue(gui_details["partial_excluded"])

        # Idempotency: hardening again should make no modifications
        h_res2 = sa_ffs.apply_ffs_hardening(gui_path=self.ffs_path, batch_path=self.batch_path)
        self.assertTrue(h_res2.get("ok"))
        self.assertEqual(len(h_res2.get("modified")), 0)

    def test_false_green_regression_modifies_mainconfig_not_root(self):
        """P0-1 regression oracle: assert MainConfig/Filter/Exclude is mutated and NO root Filter created."""
        h_res = sa_ffs.apply_ffs_hardening(gui_path=self.ffs_path, batch_path=self.batch_path)
        self.assertTrue(h_res.get("ok"))

        # Parse XML directly to inspect DOM
        tree = ET.parse(self.ffs_path)
        root = tree.getroot()

        # 1. Assert root-level Filter DOES NOT exist in wrapped schema
        root_filter = root.find("Filter")
        self.assertIsNone(root_filter, "Root-level <Filter> must NOT be created when MainConfig exists!")

        # 2. Assert MainConfig/Filter/Exclude exists and has required items
        main_filter = root.find("MainConfig/Filter")
        self.assertIsNotNone(main_filter, "MainConfig/Filter must be present")
        exc_el = main_filter.find("Exclude")
        self.assertIsNotNone(exc_el)
        items = [x.text.strip().lower() for x in exc_el.findall("Item") if x.text]
        self.assertIn("\\__store\\", items)
        self.assertIn("*\\__store\\", items)
        self.assertIn("*.partial", items)

    def test_missing_both_configs_fails_closed(self):
        """Assert that if all configured files are missing, overall_safe is False."""
        p1 = os.path.join(self.temp_dir, "missing1.ffs_gui")
        p2 = os.path.join(self.temp_dir, "missing2.ffs_batch")
        report = sa_ffs.audit_ffs_configuration(gui_path=p1, batch_path=p2)
        self.assertFalse(report.get("overall_safe"), "Missing configs must fail closed and return overall_safe=False")

    def test_missing_one_config_fails_closed(self):
        """Assert that if one required config is missing, overall_safe is False."""
        missing = os.path.join(self.temp_dir, "missing.ffs_batch")
        report = sa_ffs.audit_ffs_configuration(gui_path=self.ffs_path, batch_path=missing)
        self.assertFalse(report.get("overall_safe"))

    def test_unsupported_schema_refuses_mutation(self):
        """Assert that unsupported XML structures fail closed and refuse mutation."""
        bad_xml = "<NotFreeFileSync><SomeTag>val</SomeTag></NotFreeFileSync>"
        bad_path = os.path.join(self.temp_dir, "bad.xml")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write(bad_xml)

        with self.assertRaises(sa_ffs.FFSError):
            sa_ffs.apply_ffs_hardening(gui_path=bad_path, batch_path=bad_path)

        report = sa_ffs.audit_ffs_configuration(gui_path=bad_path, batch_path=bad_path)
        self.assertFalse(report.get("overall_safe"))
        self.assertTrue(report["details"]["gui"].get("schema_unsupported"))

    def test_include_semantics_respected(self):
        """Assert that restricted Include list not covering .svault marks configuration unsafe."""
        restricted_xml = """<?xml version="1.0" encoding="utf-8"?>
<FreeFileSync XmlType="GUI" XmlVersion="19">
    <MainConfig>
        <Filter>
            <Include>
                <Item>*.docx</Item>
            </Include>
            <Exclude>
                <Item>\\__STORE\\</Item>
                <Item>*\\__STORE\\</Item>
                <Item>*.partial</Item>
            </Exclude>
        </Filter>
    </MainConfig>
</FreeFileSync>
"""
        restricted_path = os.path.join(self.temp_dir, "restricted.ffs_gui")
        with open(restricted_path, "w", encoding="utf-8") as f:
            f.write(restricted_xml)

        report = sa_ffs.audit_ffs_configuration(gui_path=restricted_path, batch_path=restricted_path)
        gui_details = report["details"]["gui"]
        self.assertFalse(gui_details["svault_included"], ".svault should not be considered included when Include is restricted")
        self.assertFalse(gui_details["safe"])
        self.assertFalse(report.get("overall_safe"))

    def test_folder_pair_custom_filter_hardened(self):
        """Assert that custom filters on folder pairs syncing __K are also hardened."""
        pair_filter_xml = """<?xml version="1.0" encoding="utf-8"?>
<FreeFileSync XmlType="GUI" XmlVersion="19">
    <MainConfig>
        <Filter>
            <Include><Item>*</Item></Include>
            <Exclude><Item>*.tmp</Item></Exclude>
        </Filter>
        <FolderPairs>
            <Pair>
                <Left>V:\\___VAC\\__K</Left>
                <Right>R:\\__SAVE_R\\__K</Right>
                <Filter>
                    <Include><Item>*</Item></Include>
                    <Exclude><Item>*.bak</Item></Exclude>
                </Filter>
            </Pair>
        </FolderPairs>
    </MainConfig>
</FreeFileSync>
"""
        pair_path = os.path.join(self.temp_dir, "pair_custom.ffs_gui")
        with open(pair_path, "w", encoding="utf-8") as f:
            f.write(pair_filter_xml)

        h_res = sa_ffs.apply_ffs_hardening(gui_path=pair_path, batch_path=pair_path)
        self.assertTrue(h_res.get("ok"))

        tree = ET.parse(pair_path)
        root = tree.getroot()
        pair_exc = root.find("MainConfig/FolderPairs/Pair/Filter/Exclude")
        pair_items = [x.text.strip().lower() for x in pair_exc.findall("Item") if x.text]
        self.assertIn("\\__store\\", pair_items)
        self.assertIn("*\\__store\\", pair_items)

    def test_restore_ffs_config(self):
        h_res = sa_ffs.apply_ffs_hardening(gui_path=self.ffs_path, batch_path=self.batch_path)
        self.assertEqual(len(h_res.get("modified")), 2)

        # Restore from backup
        r_res = sa_ffs.restore_ffs_backup(gui_path=self.ffs_path, batch_path=self.batch_path)
        self.assertTrue(r_res.get("ok"))
        self.assertIn(self.ffs_path, r_res.get("restored"))

        # Audit should show unhardened again
        report = sa_ffs.audit_ffs_configuration(gui_path=self.ffs_path, batch_path=self.batch_path)
        self.assertFalse(report["details"]["gui"]["safe"])

    def test_scan_plaintext_leakage(self):
        # Create a mock pair directory with a mock __STORE
        mock_target = os.path.join(self.temp_dir, "target_drive")
        mock_store = os.path.join(mock_target, "__STORE")
        os.makedirs(mock_store, exist_ok=True)
        with open(os.path.join(mock_store, "secret.md"), "w") as f:
            f.write("# sensitive note")

        # 1. Default sanitized audit (R8): must NOT leak filenames
        inventory = sa_ffs.audit_plaintext_leakage_on_destinations(
            destinations=[mock_target], max_files=10
        )
        self.assertIn(mock_target, inventory)
        target_info = inventory[mock_target]
        self.assertTrue(target_info["store_exists"])
        self.assertFalse(target_info["clean"])
        self.assertEqual(target_info["file_count"], 1)
        self.assertNotIn("sample_files", target_info)
        self.assertNotIn("top_entries", target_info)

        # 2. Explicit opt-in for local detailed view
        inv_detailed = sa_ffs.audit_plaintext_leakage_on_destinations(
            destinations=[mock_target], max_files=10, include_samples=True
        )
        self.assertIn("sample_files", inv_detailed[mock_target])
        self.assertIn("secret.md", inv_detailed[mock_target]["sample_files"][0])


if __name__ == "__main__":
    unittest.main()
