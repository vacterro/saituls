# -*- coding: utf-8 -*-
# T-167 CLEAN-PACKAGE PROOF (authority repaired by SRC-021).
#
# SRC-021 supersedes ONLY the SRC-020 "PACKAGE PROOF" clause that demanded
# heavyweight Bin/FFMPEG.EXE + Bin/FFPROBE.EXE inside the normal AUDAPACK
# audit representation (hard ~30 MiB budget; the two binaries alone are
# ~247 MB). All other SRC-020 requirements remain authoritative.
#
# Three distinct artifacts/contracts:
#   A. AUDAPACK AUDIT REPRESENTATION - small review surface only
#      (registry closure, runner scripts, source, tests). NO media exes.
#   B. RELEASE PAYLOAD ARTIFACT - dist/SAITULS-payload-<VERSION>.zip built
#      from PAYLOAD_MANIFEST.txt via Scripts/build_payload.ps1 from the
#      full authoritative working tree. Contains the heavyweight runtime.
#   C. CLEAN ASSEMBLED RUNTIME PROOF (this file) - fresh AUDAPACK extracted
#      into a brand-new temporary directory, fresh payload ZIP overlaid on
#      top, media validation suites run FROM that assembled tree. No
#      implementation/source/test files are copied from the live working
#      tree after AUDAPACK extraction.
#
# Proves:  audit source artifact + declared runtime payload artifact
#       -> executable validated media tree
# NOT:     arbitrary files copied from the developer working tree
#       -> green tests.
#
# ISOLATION CONTRACT (poison-marker, not rename-based):
#   The live ROOT cannot be renamed here (its directory handle is held by
#   long-running processes; killing them is out of scope and unsafe). The
#   isolation contract is enforced with equal strength:
#   1. Every suite must resolve ROOT from the SAITULS_ROOT environment
#      variable - never from CWD or relative fallbacks. Each suite (in the
#      ASSEMBLED tree, never the live tree) is rewritten to honor
#      SAITULS_ROOT first, and the rewrite is asserted: a suite that loses
#      the contract FAILS here rather than silently hydrating from the
#      live checkout.
#   2. The assembled Bin/FFMPEG.EXE is byte-overwritten with a poison
#      marker while the suites run; any resolution outside the assembled
#      tree produces a DIFFERENT result and fails the deterministic matrix.
#      The binary is restored afterwards and proven to execute.
#   3. Suites run with cwd=extract and SAITULS_ROOT=extract, so relative
#      resolution lands in the assembled tree only.
#   4. The assembled FFMPEG_RUN.PS1 must reference no absolute live-root
#      path at all.
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
parser = argparse.ArgumentParser(description="Validate a clean source ZIP plus release payload")
parser.add_argument('--source-archive', required=True,
                    help='Fresh source/audit ZIP with repository files at its root')
parser.add_argument('--payload-archive',
                    help='Payload ZIP to validate; omitted means build it from ROOT')
args = parser.parse_args()

POISON_SENTINEL = b'T167_POISON_SENTINEL_DO_NOT_READ'

# Contract A: the small audit surface a fresh normal AUDAPACK must carry.
REQUIRED_AUDAPACK = [
    'Registry/FFMPEG_MENU.REG',
    'Registry/FFMPEG_MENU_REM.REG',
    'Registry/MKV_FIX.REG',
    'Registry/MKV_FIX_REM.REG',
    'Registry/MERGE_AUD.REG',
    'Registry/MERGE_AUD_REM.REG',
    'Bin/FFMPEG_RUN.BAT',
    'Bin/FFMPEG_RUN.PS1',
    'Scripts/DL_YT.CMD',
    'Scripts/DL_YT.PS1',
    'Scripts/MERGE_AUD.CMD',
    'Scripts/MERGE_AUD.PS1',
    'Scripts/media_payload.ps1',
    'Scripts/saipatch/clipboard+.pyw',
    'Registry/DL_YT.REG',
    'Registry/DL_YT_REM.REG',
    'setup.ps1',
    'SAITULS.cs',
    'PAYLOAD_MANIFEST.txt',
    'tests/test_media_conversions.py',
    'tests/test_merge_aud_topology.py',
    'tests/test_registry_sentinel.py',
    'tests/test_clean_package.py',
]

# Contract A negative: heavyweight media executables are NOT required
# (and per policy are not present) in the AUDAPACK audit representation.
FORBIDDEN_IN_AUDAPACK = [
    'Bin/FFMPEG.EXE',
    'Bin/FFPROBE.EXE',
]

# Contract B: payload members that make the assembled tree executable.
REQUIRED_PAYLOAD = [
    'Bin/FFMPEG.EXE',
    'Bin/FFPROBE.EXE',
    'Bin/FFMPEG_RUN.BAT',
    'Bin/FFMPEG_RUN.PS1',
]

REQUIRED_TESTS = [
    'test_media_conversions.py',
    'test_merge_aud_topology.py',
    'test_registry_sentinel.py',
]

# T-164/T-167: the assembled tree must also carry the ordinary-tools surface
# its registry verbs and shell buttons point at -- the download worker and the
# Clipboard+ subsystem -- otherwise verbs resolve to files that do not exist
# on a fresh install.
REQUIRED_ORDINARY_TOOLS = [
    'Scripts/DL_YT.PS1',
    'Scripts/DL_YT.CMD',
    'Scripts/saipatch/clipboard+.pyw',
    'Registry/DL_YT.REG',
]

failures = 0
passed = 0


def check(name, ok, detail=''):
    global failures, passed
    print(('PASS  ' if ok else 'FAIL  ') + name + (('  ' + str(detail)) if detail else ''))
    if ok:
        passed += 1
    else:
        failures += 1


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', **kw)


def metric(out, key):
    for line in out.splitlines():
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip()
    return None


version = open(os.path.join(ROOT, 'VERSION'), encoding='utf-8').read().strip()
work = tempfile.mkdtemp(prefix='t167_clean_')
extract = os.path.join(work, 'saituls_assembled')
try:
    # ---- Contract A: fresh normal AUDAPACK audit representation ------------
    audapack_zip = os.path.abspath(args.source_archive)
    check('source archive exists', os.path.isfile(audapack_zip))
    if not os.path.isfile(audapack_zip):
        print('CLEAN_PACKAGE_GREEN=FALSE (no source artifact)')
        sys.exit(1)

    with zipfile.ZipFile(audapack_zip) as z:
        names = {n.replace('\\', '/') for n in z.namelist() if not n.endswith('/')}
    missing = [p for p in REQUIRED_AUDAPACK if p not in names]
    check('AUDAPACK carries the complete small audit surface',
          not missing, 'missing=%s' % missing)
    present_forbidden = [p for p in FORBIDDEN_IN_AUDAPACK if p in names]
    check('AUDAPACK carries no heavyweight media executables',
          not present_forbidden, 'present=%s' % present_forbidden)
    size = os.path.getsize(audapack_zip)
    check('AUDAPACK inside normal budget (~30 MiB)',
          size <= 30 * 1024 * 1024, 'bytes=%s' % size)

    # ---- 1. extract the AUDAPACK into a brand-new temporary directory ------
    with zipfile.ZipFile(audapack_zip) as z:
        z.extractall(extract)
    for rel in REQUIRED_AUDAPACK:
        check('extracted AUDAPACK has ' + rel, os.path.isfile(os.path.join(extract, rel)))
    missing_tools = [p for p in REQUIRED_ORDINARY_TOOLS
                     if not os.path.isfile(os.path.join(extract, p))]
    check('assembled tree carries the ordinary-tools workers (DL_YT, Clipboard+)',
          not missing_tools, 'missing=%s' % missing_tools)

    # ---- 2. fresh release payload ZIP independently from the full ROOT -----
    if args.payload_archive:
        payload_zip = os.path.abspath(args.payload_archive)
    else:
        r = run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-File', os.path.join(ROOT, 'Scripts', 'build_payload.ps1'),
                 '-Root', ROOT])
        check('fresh payload build exits 0', r.returncode == 0,
              (r.stderr or r.stdout).strip()[-300:])
        payload_zip = os.path.join(ROOT, 'dist', 'SAITULS-payload-%s.zip' % version)
    check('payload artifact exists', os.path.isfile(payload_zip), payload_zip)
    if failures:
        print('CLEAN_PACKAGE_GREEN=FALSE (artifact build failed)')
        sys.exit(1)

    # ---- 3. overlay ONLY the payload ZIP into the extracted AUDAPACK tree --
    with zipfile.ZipFile(payload_zip) as z:
        z.extractall(extract)
    for rel in REQUIRED_PAYLOAD:
        check('assembled tree has payload member ' + rel,
              os.path.isfile(os.path.join(extract, rel)))
    ffv = run([os.path.join(extract, 'Bin', 'FFMPEG.EXE'), '-version'])
    check('assembled FFMPEG.EXE executes', ffv.returncode == 0)
    fpv = run([os.path.join(extract, 'Bin', 'FFPROBE.EXE'), '-version'])
    check('assembled FFPROBE.EXE executes', fpv.returncode == 0)
    if failures:
        print('CLEAN_PACKAGE_GREEN=FALSE (assembly incomplete)')
        sys.exit(1)

    # ---- ISOLATION step 1: SAITULS_ROOT contract on the assembled suites ---
    patched = []
    for name in REQUIRED_TESTS:
        p = os.path.join(extract, 'tests', name)
        with open(p, encoding='utf-8') as f:
            src = f.read()
        old_lines = [l for l in src.splitlines()
                     if l.startswith('ROOT = os.path.normpath')]
        if len(old_lines) != 1:
            check('suite %s has a single ROOT resolution line' % name,
                  False, 'found %d' % len(old_lines))
            continue
        new_src = src.replace(
            old_lines[0],
            "ROOT = os.environ.get('SAITULS_ROOT') or "
            + old_lines[0].replace('ROOT = ', '', 1),
            1)
        with open(p, 'w', encoding='utf-8', newline='') as f:
            f.write(new_src)
        patched.append(name)
        check('suite %s isolated via SAITULS_ROOT' % name, True)
    if len(patched) != len(REQUIRED_TESTS):
        print('CLEAN_PACKAGE_GREEN=FALSE (isolation contract not enforceable)')
        sys.exit(1)

    # ---- run the suites from the assembled tree --------------------------
    env_clean = {**os.environ, 'SAITULS_ROOT': extract}
    results = {}
    for name in REQUIRED_TESTS:
        print('\n--- %s (from assembled AUDAPACK+payload tree) ---' % name)
        r = run([sys.executable, os.path.join(extract, 'tests', name)],
                cwd=extract, env=env_clean)
        out = (r.stdout or '') + (r.stderr or '')
        print(out.strip()[-1500:] if r.returncode != 0 else out.strip()[-400:])
        results[name] = (r.returncode, out)

    assembled_ffmpeg = os.path.join(extract, 'Bin', 'FFMPEG.EXE')
    ffmpeg_bytes = open(assembled_ffmpeg, 'rb').read()
    # Poison the assembled binary and re-run the media suite: it MUST fail
    # (nonzero / no green matrix). A suite that silently fell back to the
    # live checkout's binary would stay green here.
    with open(assembled_ffmpeg, 'wb') as f:
        f.write(POISON_SENTINEL)
    try:
        nc = run([sys.executable, os.path.join(extract, 'tests',
                 'test_media_conversions.py')], cwd=extract, env=env_clean)
    finally:
        with open(assembled_ffmpeg, 'wb') as f:
            f.write(ffmpeg_bytes)
    nc_out = (nc.stdout or '') + (nc.stderr or '')
    check('negative control: poisoned assembled binary fails the media suite',
          nc.returncode != 0 and 'MEDIA_MATRIX_GREEN=TRUE' not in nc_out,
          'exit=%s' % nc.returncode)

    # ---- ISOLATION step 5: assembled binary really executes (post-restore) -
    ffv = run([assembled_ffmpeg, '-version'])
    check('assembled FFMPEG.EXE executes post-restore', ffv.returncode == 0)

    # ---- media matrix gates -------------------------------------------------
    rc_media, out_media = results['test_media_conversions.py']
    discovered = metric(out_media, 'MEDIA_ACTIONS_DISCOVERED')
    executed = metric(out_media, 'MEDIA_ACTIONS_EXECUTED')
    matrix_passed = metric(out_media, 'MEDIA_ACTIONS_PASSED')
    failed = metric(out_media, 'MEDIA_ACTIONS_FAILED')
    skipped = metric(out_media, 'MEDIA_ACTIONS_SKIPPED')
    green = metric(out_media, 'MEDIA_MATRIX_GREEN')

    check('media matrix discovered == executed',
          discovered is not None and discovered == executed,
          'discovered=%s executed=%s' % (discovered, executed))
    check('media matrix executed == passed',
          executed is not None and executed == matrix_passed,
          'passed=%s' % matrix_passed)
    check('media matrix failed == 0', failed == '0', 'failed=%s' % failed)
    check('media matrix skipped == 0', skipped == '0', 'skipped=%s' % skipped)
    check('media matrix green flag', green == 'TRUE', 'MEDIA_MATRIX_GREEN=%s' % green)
    check('media suite exit 0 from assembled tree', rc_media == 0, 'exit=%s' % rc_media)

    rc_merge, out_merge = results['test_merge_aud_topology.py']
    check('MERGE_AUD topology green from assembled tree',
          rc_merge == 0 and 'FAIL' not in out_merge, 'exit=%s' % rc_merge)

    rc_reg, out_reg = results['test_registry_sentinel.py']
    check('registry sentinel green from assembled tree', rc_reg == 0,
          'exit=%s' % rc_reg)

    # ---- ISOLATION step 6: runner path containment --------------------------
    with open(os.path.join(extract, 'Bin', 'FFMPEG_RUN.PS1'),
              encoding='utf-8', errors='replace') as f:
        runner = f.read()
    check('assembled FFMPEG_RUN.PS1 has no live-root absolute path',
          ROOT not in runner)
finally:
    shutil.rmtree(work, ignore_errors=True)

print()
print('CLEAN_PACKAGE_GREEN=%s' % ('TRUE' if failures == 0 else 'FALSE'))
sys.exit(0 if failures == 0 else 1)
