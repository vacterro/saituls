"""One unchanged executable oracle for the original and corrected helper."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
import test_accounts_journal_authority as oracle

class JournalControl(unittest.TestCase):
    def __init__(self, evidence):
        super().__init__('runTest')
        self.evidence = evidence
    def id(self):
        return self.evidence['name']
    def shortDescription(self):
        return self.id()
    def runTest(self):
        self.assertTrue(self.evidence['passed'], str(self.evidence['detail']))

with tempfile.TemporaryDirectory(prefix='sai-journal-regression-') as tmp:
    oracle.BIN = Path(tmp)
    csc = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(csc), '-nologo', '-optimize+', '-out:' + str(oracle.BIN / 'sai-accounts-helper.exe'),
                    '-r:System.dll', '-r:System.Core.dll', '-r:System.Security.dll',
                    '-r:System.ServiceProcess.dll', '-r:System.Web.Extensions.dll',
                    str(ROOT / 'Scripts/accounts/SaiAccountsHelper.cs')], check=True,
                   capture_output=True, text=True, timeout=30)
    exe = oracle.BIN / 'probe.exe'
    oracle.compile_probe(exe)
    oracle.identity_controls(exe)
    oracle.publication_controls(exe)
    oracle.rollback_scope_control(exe)
    oracle.recovery_scope_control(exe)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(JournalControl(r) for r in oracle.RESULTS))
sys.exit(0 if result.wasSuccessful() else 1)
