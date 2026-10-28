"""Plaintext-to-encrypted vault migration for SAITULS Secure Apps.

Migration is destructive only at the very last step, and even then only to a
*name*: the plaintext tree is renamed aside, never deleted. Everything before
the cutover is additive, so an interruption at any point leaves the user's
original vault exactly where it was.

The sequence, and why each step is there:

===================  ========================================================
PRECHECK             source exists, destination does not, space is plausible;
                     no reparse points; no running plaintext application
CREATE               the container ENROLLMENT already built is mounted at a
                     STAGING path, so the plaintext original keeps owning the
                     real path for the whole copy and verify
COPY                 whole tree copied; content is never inspected, parsed,
                     normalised or rewritten
VERIFY_TREE          every relative path present on both sides, both ways
                     (volume-owned roots such as System Volume Information
                     excluded: they belong to the NTFS volume, not the vault)
VERIFY_SIZES         byte size equal for every file
VERIFY_HASHES        SHA-256 over EVERY regular file, both sides, from a
                     deterministic full manifest
VERIFY_APP           the protected application opens the encrypted copy
CLOSE_APP            and is closed again, cleanly
RELOCK               container detached: proves the lock path works
REMOUNT              container reattached at the staging path
REVERIFY             full tree + size + hash manifest again, after a real
                     lock/unlock round trip
CUTOVER              plaintext renamed aside, then the archive is compared
                     against the encrypted destination FULLY, and only then
                     is the volume remounted at the real path

Verification is total on purpose. A one-time migration of a user's only copy
of their notes is not the place to trade a cryptographic claim for a few
seconds: ``VERIFY_HASHES`` and ``REVERIFY`` hash every regular file, and the
post-rename comparison closes the window between the last source hash and the
rename. Sampling survives only as a progress estimate
(:func:`representative_sample`) and never authorizes anything.
===================  ========================================================

A journal records the last completed step, so an interrupted run is resumed
rather than restarted, and an interruption *inside* the cutover -- the only
window where the real path is briefly not the plaintext tree -- is repaired
by :func:`recover`, which can always tell which of the two possible states
the filesystem is in.

The result is deliberately two facts, not one:

    MIGRATION_VERIFIED        the encrypted copy is complete and openable
    PLAINTEXT_SOURCE_REMAINS  a readable copy of everything still exists

The second is reported until the user removes or archives it themselves. A
gate that claims a vault is protected while a plaintext copy sits next to it
is telling the user something untrue, so this subsystem refuses to.
"""
import hashlib
import json
import ntpath
import os
import shutil
import time

import sa_applife
import sa_crypto
import sa_paths
import sa_storage

JOURNAL_SCHEMA = "saituls.secure-apps.migration/1"
JOURNAL_SCHEMA_VERSION = 1

# Fail-closed migration journal statuses (Phase 1)
JOURNAL_ABSENT = "JOURNAL_ABSENT"
JOURNAL_VALID = "JOURNAL_VALID"
JOURNAL_INVALID = "JOURNAL_INVALID"
JOURNAL_UNSUPPORTED_SCHEMA = "JOURNAL_UNSUPPORTED_SCHEMA"
JOURNAL_UNREADABLE = "JOURNAL_UNREADABLE"
JOURNAL_CORRUPT = "JOURNAL_CORRUPT"

JOURNAL_STATUSES = (
    JOURNAL_ABSENT,
    JOURNAL_VALID,
    JOURNAL_INVALID,
    JOURNAL_UNSUPPORTED_SCHEMA,
    JOURNAL_UNREADABLE,
    JOURNAL_CORRUPT,
)

STEP_PRECHECK = "PRECHECK"
STEP_CREATE = "CREATE"
STEP_COPY = "COPY"
STEP_VERIFY_TREE = "VERIFY_TREE"
STEP_VERIFY_SIZES = "VERIFY_SIZES"
STEP_VERIFY_HASHES = "VERIFY_HASHES"
STEP_VERIFY_APP = "VERIFY_APP"
STEP_CLOSE_APP = "CLOSE_APP"
STEP_RELOCK = "RELOCK"
STEP_REMOUNT = "REMOUNT"
STEP_REVERIFY = "REVERIFY"
STEP_CUTOVER_BEGIN = "CUTOVER_BEGIN"
STEP_CUTOVER_VERIFY = "CUTOVER_VERIFY"
STEP_CUTOVER_MOUNTED = "CUTOVER_MOUNTED"
STEP_COMPLETE = "COMPLETE"

STEP_ORDER = (STEP_PRECHECK, STEP_CREATE, STEP_COPY, STEP_VERIFY_TREE,
              STEP_VERIFY_SIZES, STEP_VERIFY_HASHES, STEP_VERIFY_APP,
              STEP_CLOSE_APP, STEP_RELOCK, STEP_REMOUNT, STEP_REVERIFY,
              STEP_CUTOVER_BEGIN, STEP_CUTOVER_VERIFY, STEP_CUTOVER_MOUNTED,
              STEP_COMPLETE)

MIGRATION_VERIFIED = "MIGRATION_VERIFIED"
PLAINTEXT_SOURCE_REMAINS = "PLAINTEXT_SOURCE_REMAINS"
FULL_PROTECTION_COMPLETE = "FULL_PROTECTION_COMPLETE"

#: Failure category when the plaintext tree contains a link, junction or other
#: reparse point. Refusing is the only safe default: following a link copies
#: whatever it points at into encrypted storage, which is both a scope escape
#: and a silent copy of files the user never put in the vault.
MIGRATION_REPARSE_REFUSED = "MIGRATION_REPARSE_REFUSED"

#: How many offending paths a reparse refusal names before truncating.
REPARSE_REPORT_MAX = 20

#: NON-AUTHORITATIVE. This cap sizes a progress estimate only; no verdict,
#: gate or cutover decision anywhere reads it. See :func:`build_manifest`.
HASH_SAMPLE_MAX = 400
LONG_PATH_PREFIX = "\\\\?\\"


class MigrationError(Exception):
    category = "internal"

    def __init__(self, message, category=None, step=None):
        Exception.__init__(self, message)
        if category:
            self.category = category
        self.step = step


def long_path(path):
    """Prefix a local absolute path for the Win32 long-path namespace.

    The vault carries deep Unicode paths; ``\\\\?\\`` removes the MAX_PATH
    ceiling for every os call made through it. UNC paths get the matching
    ``\\\\?\\UNC\\`` form.
    """
    p = sa_paths.canonical(path)
    if p.startswith(LONG_PATH_PREFIX):
        return p
    if p.startswith("\\\\"):
        return LONG_PATH_PREFIX + "UNC\\" + p.lstrip("\\")
    return LONG_PATH_PREFIX + p


class MigrationJournal(object):
    """Small durable record of how far the migration got. Never a secret.

    Fail-closed: treats missing, unreadable, corrupted, or unsupported
    journals distinctly. Never silently turns an invalid journal into an
    empty one or a fresh migration.
    """

    def __init__(self, path):
        self.path = path
        self.status = JOURNAL_ABSENT
        self.error_detail = None
        self.data = {
            "schema": JOURNAL_SCHEMA,
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "profile_id": None,
            "source": None,
            "container": None,
            "staging_mount": None,
            "final_mount": None,
            "plaintext_archive": None,
            "last_step": None,
            "started": None,
            "updated": None,
            "file_count": 0,
            "total_bytes": 0,
            "failed": None,
        }
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            self.status = JOURNAL_ABSENT
            self.error_detail = None
            return self.data

        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw_text = handle.read()
        except OSError as exc:
            self.status = JOURNAL_UNREADABLE
            self.error_detail = "Cannot read migration journal: %s" % exc
            return self.data

        if not raw_text.strip():
            self.status = JOURNAL_CORRUPT
            self.error_detail = "Migration journal is empty"
            return self.data

        try:
            document = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            self.status = JOURNAL_CORRUPT
            self.error_detail = "Malformed JSON in migration journal: %s" % exc
            return self.data

        if not isinstance(document, dict):
            self.status = JOURNAL_INVALID
            self.error_detail = "Migration journal root must be a JSON object"
            return self.data

        schema = document.get("schema")
        schema_version = document.get("schema_version")
        if not schema or not isinstance(schema, str):
            self.status = JOURNAL_INVALID
            self.error_detail = "Migration journal missing schema identifier"
            return self.data

        if schema == JOURNAL_SCHEMA:
            if schema_version is not None and schema_version != JOURNAL_SCHEMA_VERSION:
                self.status = JOURNAL_UNSUPPORTED_SCHEMA
                self.error_detail = "Unsupported migration schema version %s (expected %s)" % (
                    schema_version, JOURNAL_SCHEMA_VERSION)
                return self.data
        elif schema.startswith("saituls.secure-apps.migration/"):
            self.status = JOURNAL_UNSUPPORTED_SCHEMA
            self.error_detail = "Unsupported migration schema: %s" % schema
            return self.data
        else:
            self.status = JOURNAL_INVALID
            self.error_detail = "Unknown migration schema: %s" % schema
            return self.data

        last_step = document.get("last_step")
        if last_step is not None and last_step not in STEP_ORDER:
            self.status = JOURNAL_INVALID
            self.error_detail = "Unknown migration step in journal: %r" % (last_step,)
            return self.data

        profile_id = document.get("profile_id")
        if profile_id is not None and (not isinstance(profile_id, str) or not profile_id.strip()):
            self.status = JOURNAL_INVALID
            self.error_detail = "Invalid profile_id in migration journal"
            return self.data

        self.status = JOURNAL_VALID
        self.error_detail = None
        self.data.update(document)
        return self.data

    def save(self):
        if self.status not in (JOURNAL_VALID, JOURNAL_ABSENT):
            raise MigrationError(
                "Refusing to overwrite invalid or unreadable migration journal (%s: %s). "
                "Preserving existing state without mutation."
                % (self.status, self.error_detail),
                "migration_state_invalid")
        directory = ntpath.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(self.data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        self.status = JOURNAL_VALID
        self.error_detail = None

    def mark(self, step, **fields):
        if step not in STEP_ORDER:
            raise MigrationError("unknown migration step: %r" % (step,))
        self.data["last_step"] = step
        self.data.update(fields)
        self.save()
        return step

    @property
    def last_step(self):
        return self.data.get("last_step")

    def index(self):
        step = self.last_step
        return STEP_ORDER.index(step) if step in STEP_ORDER else -1

    def reached(self, step):
        return self.index() >= STEP_ORDER.index(step)

    def clear(self):
        if self.status not in (JOURNAL_VALID, JOURNAL_ABSENT):
            raise MigrationError(
                "Refusing to delete invalid migration journal (%s). "
                "Preserving invalid journal byte-identically."
                % self.status,
                "migration_state_invalid")
        try:
            os.remove(self.path)
            self.status = JOURNAL_ABSENT
            self.error_detail = None
        except OSError:
            pass


# --------------------------------------------------------------------------
#: Directories a Windows VOLUME owns, which therefore exist at the root of
#: the encrypted destination and can never exist in a plaintext source that
#: is an ordinary folder. Counting them as "unexpected files in the encrypted
#: copy" made every real migration fail verification -- and worse, they are
#: unreadable even to an administrator, so walking into them raises.
#:
#: Only ever skipped at the ROOT of a tree: a note called
#: "System Volume Information" three folders deep is the user's data and is
#: copied and verified like anything else.
VOLUME_OWNED_ROOT_DIRECTORIES = frozenset((
    "system volume information",
    "$recycle.bin",
    "$extend",
    "found.000",
))


def _is_volume_owned(relpath):
    head = relpath.replace("/", "\\").split("\\", 1)[0].lower()
    return head in VOLUME_OWNED_ROOT_DIRECTORIES


def _scan_root(root):
    """One os.walk: files -> {rel:size} and directories -> {rel}.

    Reparse points are refused HERE, inside the walk that already visits every
    entry, rather than in a separate pre-pass. That buys two things: the
    containment check costs no extra traversal, and no traversal anywhere in the
    migration can pass through a link unchecked -- ``os.walk`` does not descend
    into a symlinked directory, but it DOES follow a junction or a mount point
    without being asked, which is precisely how content escapes the vault.
    """
    files, dirs = {}, set()
    base = long_path(root)
    offenders = []
    for dirpath, dirnames, filenames in os.walk(base):
        if dirpath == base:
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in VOLUME_OWNED_ROOT_DIRECTORIES]
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            if sa_paths.is_reparse_point(full):
                offenders.append(os.path.relpath(full, base))
                dirnames.remove(name)   # never descend through a link
                continue
            dirs.add(os.path.relpath(full, base).lower())
        for name in filenames:
            full = os.path.join(dirpath, name)
            if sa_paths.is_reparse_point(full):
                offenders.append(os.path.relpath(full, base))
                continue
            rel = os.path.relpath(full, base)
            if _is_volume_owned(rel):
                continue
            try:
                files[rel.lower()] = os.path.getsize(full)
            except OSError as exc:
                raise MigrationError("could not stat %s (%s)"
                                     % (rel, type(exc).__name__), "internal")
    if offenders:
        raise MigrationError(reparse_refusal_message(offenders),
                             MIGRATION_REPARSE_REFUSED)
    return files, dirs

scan_tree = _scan_root

def walk_tree(root):
    files, _dirs = _scan_root(root)
    return files


def directories_of(root):
    _files, dirs = _scan_root(root)
    return dirs


def reparse_refusal_message(offenders):
    """One line naming the links that stopped the migration.

    The message names PATHS only. What a link points at may be another
    user's documents, so its target is never read, logged or reported.
    """
    listed = offenders[:REPARSE_REPORT_MAX]
    text = ", ".join(listed)
    if len(offenders) > REPARSE_REPORT_MAX:
        text += " (and %d more)" % (len(offenders) - REPARSE_REPORT_MAX)
    return ("this vault contains %d link(s), junction(s) or mount point(s), "
            "which copying would silently pull in content that is not part of "
            "the vault: %s. Remove them, or copy them in yourself, then retry. "
            "No files were moved." % (len(offenders), text))


def plaintext_copy_remains(archive):
    """True while a readable copy of the vault still exists at *archive*.

    The verdict is asked, never assumed. An archive path that has since been
    deleted, renamed away or emptied means the operator finished the job, and
    only then may the UI say the vault is fully protected.
    """
    if not archive:
        return False
    try:
        return bool(os.listdir(long_path(archive)))
    except OSError:
        return False


def representative_sample(relpaths_to_sizes, limit=HASH_SAMPLE_MAX):
    """Deterministic estimate of hashing cost. NOT a verification.

    This never authorizes anything. It exists so a UI can say "about this
    long" before the real pass starts, and so a diagnostic can be sampled
    cheaply. Acceptance comes from :func:`build_manifest`, which hashes every
    regular file on both sides; a corruption in a file this function happens
    not to pick must still fail the migration.
    """
    items = sorted(relpaths_to_sizes.items())
    if not items:
        return []
    if len(items) <= limit:
        return [rel for rel, _size in items]
    chosen = set()
    by_size = sorted(items, key=lambda kv: kv[1])
    for rel, _size in by_size[:5] + by_size[-5:]:
        chosen.add(rel)
    chosen.add(items[0][0])
    chosen.add(items[-1][0])
    stride = max(1, len(items) // max(1, (limit - len(chosen))))
    for index in range(0, len(items), stride):
        if len(chosen) >= limit:
            break
        chosen.add(items[index][0])
    return sorted(chosen)


def hash_file(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with open(long_path(path), "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def build_manifest(root, relpaths, progress=None, total_files=None,
                   total_bytes=None):
    """``{rel: (size, sha256)}`` for EVERY regular file under *root*.

    This is the authoritative record. There is no sampling, no stride and no
    "representative" subset: a vault migration is a one-time operation on
    what may be the user's only copy of their notes, and a file that escaped
    the hash set is a file that escaped the guarantee.

    *progress* is called with ``(files_done, files_total, bytes_done,
    bytes_total)`` as it goes, so a slow pass is visible instead of silent.
    """
    base = long_path(root)
    wanted = sorted(relpaths)
    files_total = len(wanted) if total_files is None else int(total_files)
    bytes_total = (sum(int(relpaths[rel]) for rel in wanted)
                   if total_bytes is None else int(total_bytes))
    manifest = {}
    files_done = 0
    bytes_done = 0
    for rel in wanted:
        full = os.path.join(base, rel)
        try:
            size = os.path.getsize(full)
        except OSError as exc:
            raise MigrationError("could not stat %s (%s)"
                                 % (rel, type(exc).__name__), "internal")
        digest = hash_file(full)
        manifest[rel] = (int(size), digest)
        files_done += 1
        bytes_done += int(size)
        if progress is not None and (files_done % 64 == 0 or files_done == files_total):
            progress(files_done, files_total, bytes_done, bytes_total)
    return manifest


def compare_manifests(source_manifest, dest_manifest, what="verification"):
    """Raise on the first difference. Sizes first, then content hashes.

    Sizes are compared before content so a truncated copy is reported as the
    size error it is, instead of as an opaque hash mismatch.
    """
    for rel in sorted(source_manifest):
        source_size, source_hash = source_manifest[rel]
        dest_entry = dest_manifest.get(rel)
        if dest_entry is None:
            raise MigrationError("file missing from the encrypted copy during "
                                 "%s: %s" % (what, rel), "internal")
        dest_size, dest_hash = dest_entry
        if source_size != dest_size:
            raise MigrationError("byte size differs during %s for %s (%s vs %s)"
                                 % (what, rel, source_size, dest_size), "internal")
        if source_hash != dest_hash:
            raise MigrationError("content hash differs during %s for %s"
                                 % (what, rel), "internal")


class MigrationReport(object):
    __slots__ = ("ok", "profile_id", "status", "steps", "file_count",
                 "total_bytes", "hashed", "hashed_bytes", "phase",
                 "plaintext_archive", "reason", "error_category",
                 "resumed_from", "full_protection_complete")

    def __init__(self, profile_id):
        self.ok = False
        self.profile_id = profile_id
        self.status = []
        self.steps = []
        self.file_count = 0
        self.total_bytes = 0
        self.hashed = 0
        self.hashed_bytes = 0
        self.phase = None
        self.plaintext_archive = None
        self.reason = ""
        self.error_category = "none"
        self.resumed_from = None
        self.full_protection_complete = False

    @property
    def progress(self):
        """Files hashed / total, bytes hashed / total, current phase.

        A full-hash pass over a large vault is minutes of real work. Hiding
        that is how a migration gets killed halfway and resumed wrong.
        """
        return {
            "phase": self.phase,
            "files_hashed": self.hashed,
            "bytes_hashed": self.hashed_bytes,
            "files_total": self.file_count,
            "bytes_total": self.total_bytes,
        }

    def to_dict(self):
        data = {name: getattr(self, name) for name in self.__slots__}
        data["progress"] = self.progress
        return data


class Migrator(object):
    """Runs (and resumes) the migration for one profile."""

    def __init__(self, broker, profile_id, source, journal_path=None,
                 staging_mount=None, verify_application=True,
                 app_probe_seconds=8, progress=None,
                 source_quiesce_timeout=20.0):
        self.broker = broker
        self.profile = broker.registry.get(profile_id)
        self.runtime = broker.runtime(profile_id)
        self.backend = broker.backend(self.profile)
        self.source = sa_paths.canonical(source)
        self.verify_application = verify_application
        self.app_probe_seconds = app_probe_seconds
        self.progress_callback = progress
        self.source_quiesce_timeout = float(source_quiesce_timeout)
        self.staging_mount = sa_paths.canonical(
            staging_mount or sa_paths.staging_mount_path(self.profile.container))
        self.staged_profile = self.profile.with_mount_path(self.staging_mount)
        self.journal = MigrationJournal(
            journal_path or ntpath.join(broker.registry.state_dir,
                                        "migration-%s.json" % profile_id))
        if self.journal.status not in (JOURNAL_VALID, JOURNAL_ABSENT):
            raise MigrationError(
                "Existing migration journal is invalid (%s: %s)"
                % (self.journal.status, self.journal.error_detail),
                "migration_state_invalid")
        if self.journal.data.get("profile_id") and self.journal.data.get("profile_id") != self.profile.id:
            raise MigrationError(
                "Migration journal belongs to profile %r, not %r"
                % (self.journal.data.get("profile_id"), self.profile.id),
                "migration_state_invalid")
        if self.journal.data.get("container") and self.journal.data.get("container") != self.profile.container:
            raise MigrationError(
                "Migration journal container %r does not match profile container %r"
                % (self.journal.data.get("container"), self.profile.container),
                "migration_state_invalid")
        if self.journal.data.get("source") and sa_paths.canonical(self.journal.data.get("source")) != self.source:
            raise MigrationError(
                "Migration journal source %r does not match migration source %r"
                % (self.journal.data.get("source"), self.source),
                "migration_state_invalid")

    # -- helpers ----------------------------------------------------------
    def _log(self, step, result="ok", category="none"):
        self.broker.audit.write("migrate_" + step.lower(),
                                profile_id=self.profile.id, result=result,
                                error_category=category,
                                mount_path=self.staging_mount,
                                backend=self.profile.backend,
                                container_id=self.profile.container_id)

    def _mark(self, report, step, **fields):
        self.journal.mark(step, **fields)
        report.steps.append(step)
        report.phase = step
        self._log(step)
        return step

    def _progress(self, report, phase):
        report.phase = phase
        report.hashed = 0
        report.hashed_bytes = 0
        if self.progress_callback is not None:
            self.progress_callback(dict(report.progress))

    def _hashed(self, report, files_done, _files_total, bytes_done, _bytes_total):
        report.hashed = int(files_done)
        report.hashed_bytes = int(bytes_done)
        if self.progress_callback is not None:
            self.progress_callback(dict(report.progress))

    def _hash_progress_for(self, report):
        """A :func:`build_manifest` progress callback bound to *report*."""
        return lambda files_done, files_total, bytes_done, bytes_total: \
            self._hashed(report, files_done, files_total, bytes_done, bytes_total)

    # -- reparse policy ---------------------------------------------------
    def _scan_named(self, step, root):
        """A traversal whose link refusal is attributed to *step*.

        Every traversal in this module refuses reparse points from inside
        :func:`_scan_root` -- a symlinked file, an NTFS junction, a mount point
        or a OneDrive placeholder all resolve somewhere else, and following one
        both copies content the user never put in the vault and verifies that
        content instead of the vault's. There is no safe default here, so there
        is no default: the migration stops and names the offenders.

        Only relative paths are ever reported. A diagnostic that echoed a link
        target would leak the layout of whatever the link points at.
        """
        try:
            return walk_tree(root)
        except MigrationError as exc:
            if exc.category == MIGRATION_REPARSE_REFUSED:
                exc.step = step
            raise

    # -- source quiescence ------------------------------------------------
    def running_source_pids(self):
        """Plaintext/unmanaged processes running the vault's application.

        Compared by executable basename, because the unmanaged copy is
        installed somewhere else entirely and its path proves nothing.
        """
        return [pid for pid, _created in self.running_source_identities()]

    def running_source_identities(self):
        """Capture each candidate's birth at discovery, before requesting close."""
        executable = getattr(self.profile, "executable", "") or ""
        name = ntpath.basename(executable).lower()
        if not name:
            return []
        protected = set(self.runtime.supervisor.surviving_family())
        found = []
        try:
            import psutil
        except ImportError:
            return []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if int(proc.info["pid"]) in protected:
                    continue
                if (proc.info.get("name") or "").lower() == name:
                    identity = (int(proc.info["pid"]), float(proc.create_time()))
                    if proc.is_running():
                        found.append(identity)
            except Exception:
                continue
        return sorted(found)

    def quiesce_source(self, report, step, timeout=None):
        """Refuse to migrate a source the application still has open.

        "The file was readable" is not quiescence. Obsidian writes, syncs and
        reindexes; a note saved during COPY or between the final hash and the
        rename is a note the encrypted copy does not have.

        Asks the application to close, waits a bounded time, and gives up.
        It never force-kills a normal plaintext application: unsaved notes in
        an unmonitored process are exactly the data this subsystem exists to
        protect, and killing them is the operator's decision, not ours.
        """
        deadline_timeout = self.source_quiesce_timeout if timeout is None else timeout
        identities = self.running_source_identities()
        remaining = [pid for pid, _created in identities]
        if not remaining:
            return True
        adapter = sa_applife.ProcessAdapter()
        adapter.request_close(0, tuple(identities))
        deadline = time.time() + max(0.0, float(deadline_timeout))
        while time.time() < deadline:
            time.sleep(0.25)
            remaining = self.running_source_pids()
            if not remaining:
                return True
        raise MigrationError(
            "the vault's application is still running (pid %s) and is writing "
            "to the plaintext source. Close %s yourself and rerun the "
            "migration; SAITULS will not kill it and will not migrate a source "
            "that is still changing."
            % (", ".join(str(p) for p in remaining),
               ntpath.basename(self.profile.executable or "the application")),
            "source_busy", step)

    # -- steps ------------------------------------------------------------
    def precheck(self, report):
        if not os.path.isdir(long_path(self.source)):
            raise MigrationError("plaintext source not found: %s" % self.source,
                                 "path_policy", STEP_PRECHECK)
        if sa_paths.is_within(self.source, self.profile.container):
            raise MigrationError("the container may not live inside the vault "
                                 "being migrated", "path_policy", STEP_PRECHECK)
        # The walk below refuses links itself, before a single byte is read or
        # copied: a link followed during COPY copies its target's content, and
        # a link followed during VERIFY validates that content instead of the
        # vault's.
        tree = self._scan_named(STEP_PRECHECK, self.source)
        report.file_count = len(tree)
        report.total_bytes = sum(tree.values())
        if report.file_count == 0:
            raise MigrationError("the plaintext source is empty; refusing to "
                                 "migrate nothing over a real vault path",
                                 "path_policy", STEP_PRECHECK)
        needed_gb = max(1, int((report.total_bytes * 1.35) // (1024 ** 3)) + 1)
        if self.profile.size_gb < needed_gb:
            raise MigrationError(
                "profile declares a %d GiB container but the source needs about "
                "%d GiB; raise storage.size_gb first"
                % (self.profile.size_gb, needed_gb), "config_invalid", STEP_PRECHECK)
        self._mark(report, STEP_PRECHECK, profile_id=self.profile.id,
                   source=self.source, container=self.profile.container,
                   staging_mount=self.staging_mount,
                   final_mount=self.profile.mount_path,
                   started=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   file_count=report.file_count, total_bytes=report.total_bytes)

    def create_destination(self, report, volume_secret_provider):
        if self.backend.state(self.staged_profile) == sa_storage.MISSING:
            raise MigrationError(
                "no encrypted container exists yet; run Secure Apps enrollment "
                "first. Enrollment builds the container at its own staging "
                "mount and never touches this profile's real mount path, so "
                "it can run while the plaintext vault is still in place.",
                "not_enrolled", STEP_CREATE)
        try:
            sa_paths.assert_directory_empty(self.staging_mount,
                                            "migration staging mount")
        except sa_paths.PathPolicyError as exc:
            raise MigrationError(str(exc), "path_policy", STEP_CREATE)
        os.makedirs(self.staging_mount, exist_ok=True)
        secret = volume_secret_provider()
        try:
            self.backend.unlock_and_mount(self.staged_profile, secret)
        finally:
            secret.zeroize()
        self._mark(report, STEP_CREATE)

    def copy_tree(self, report):
        self.quiesce_source(report, STEP_COPY)
        src = long_path(self.source)
        dst = long_path(self.staging_mount)
        offenders = []
        for dirpath, dirnames, filenames in os.walk(src):
            if dirpath == src:
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in VOLUME_OWNED_ROOT_DIRECTORIES]
            # Checked inside the copying walk, not in a separate pre-pass: a
            # link planted between precheck and copy is caught here, and the
            # containment guarantee still costs no extra traversal.
            for name in list(dirnames):
                if sa_paths.is_reparse_point(os.path.join(dirpath, name)):
                    offenders.append(os.path.relpath(os.path.join(dirpath, name), src))
                    dirnames.remove(name)
            for name in filenames:
                if sa_paths.is_reparse_point(os.path.join(dirpath, name)):
                    offenders.append(os.path.relpath(os.path.join(dirpath, name), src))
            rel = os.path.relpath(dirpath, src)
            target_dir = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(target_dir, exist_ok=True)
            for name in dirnames:
                os.makedirs(os.path.join(target_dir, name), exist_ok=True)
            for name in filenames:
                if sa_paths.is_reparse_point(os.path.join(dirpath, name)):
                    continue
                source_file = os.path.join(dirpath, name)
                target_file = os.path.join(target_dir, name)
                # copy2 preserves mtime/atime and the mode bits. Content is
                # copied byte for byte: notes are never parsed or rewritten.
                # follow_symlinks=False is belt-and-braces behind the refusal
                # above -- it can only ever copy the link itself, never a
                # target, and the refusal means there is no link to copy.
                shutil.copy2(source_file, target_file, follow_symlinks=False)
        if offenders:
            raise MigrationError(reparse_refusal_message(offenders),
                                 MIGRATION_REPARSE_REFUSED, STEP_COPY)
        self._mark(report, STEP_COPY)

    def verify_tree(self, report):
        """One scan per side returns files AND dirs (SRC-027 PERF-005).

        The old paired walk_tree+directories_of ran four full traversals; the
        two scan_tree calls here run two. reverify re-scans fresh after the
        relock/remount cycle, never reusing these snapshots.
        """
        source_files, source_dirs = scan_tree(self.source)
        dest_files, dest_dirs = scan_tree(self.staging_mount)
        missing = sorted(set(source_files) - set(dest_files))
        extra = sorted(set(dest_files) - set(source_files))
        if missing:
            raise MigrationError("%d file(s) missing from the encrypted copy, "
                                 "first: %s" % (len(missing), missing[0]),
                                 "internal", STEP_VERIFY_TREE)
        if extra:
            raise MigrationError("%d unexpected file(s) in the encrypted copy, "
                                 "first: %s" % (len(extra), extra[0]),
                                 "internal", STEP_VERIFY_TREE)
        lost_dirs = sorted(source_dirs - dest_dirs)
        if lost_dirs:
            raise MigrationError("%d directory/ies missing from the encrypted "
                                 "copy, first: %s" % (len(lost_dirs), lost_dirs[0]),
                                 "internal", STEP_VERIFY_TREE)
        self._mark(report, STEP_VERIFY_TREE)
        return source_files, dest_files

    def verify_sizes(self, report, source_files, dest_files):
        for rel, size in source_files.items():
            if dest_files.get(rel) != size:
                raise MigrationError("byte size differs for %s (%s vs %s)"
                                     % (rel, size, dest_files.get(rel)),
                                     "internal", STEP_VERIFY_SIZES)
        self._mark(report, STEP_VERIFY_SIZES)

    def verify_hashes(self, report, source_files, dest_files=None,
                      step=STEP_VERIFY_HASHES):
        """Full SHA-256 manifest of every regular file, both sides.

        No sampling. ``dest_files`` is the destination scan; when it is None
        the destination is enumerated here, so a file that appeared after the
        tree check cannot slip past the hash comparison unmeasured.
        """
        self._progress(report, step)
        if dest_files is None:
            dest_files, _dirs = scan_tree(self.staging_mount)
        source_manifest = build_manifest(self.source, source_files,
                                         self._hash_progress_for(report))
        dest_manifest = build_manifest(self.staging_mount, dest_files,
                                       self._hash_progress_for(report))
        compare_manifests(source_manifest, dest_manifest,
                          what="migration verification")
        report.hashed = len(source_manifest)
        report.hashed_bytes = sum(size for size, _h in source_manifest.values())
        self._mark(report, step)
        return source_manifest

    def verify_app(self, report):
        if not self.verify_application:
            self._mark(report, STEP_VERIFY_APP)
            self._mark(report, STEP_CLOSE_APP)
            return False
        supervisor = self.runtime.supervisor
        arguments = list(self.profile.arguments)
        if self.profile.vault_argument_style != "none":
            arguments.append(self.staging_mount)
        supervisor.launch(self.profile.executable, arguments,
                          self.profile.working_directory)
        deadline = self.broker.clock() + self.app_probe_seconds
        seen_running = False
        while self.broker.clock() < deadline:
            if supervisor.running:
                seen_running = True
                break
            self.broker.sleep(0.25)
        if not seen_running:
            raise MigrationError("the protected application did not stay running "
                                 "against the encrypted copy",
                                 "app_launch_failed", STEP_VERIFY_APP)
        self._mark(report, STEP_VERIFY_APP)
        policy = self.profile.policy
        exited, _forced = supervisor.close_sequence(
            policy.graceful_close_timeout_seconds,
            policy.force_terminate_after_timeout,
            policy.process_exit_confirm_timeout_seconds)
        if not exited:
            raise MigrationError("the protected application did not exit; the "
                                 "encrypted volume was NOT detached",
                                 "app_exit_timeout", STEP_CLOSE_APP)
        self._mark(report, STEP_CLOSE_APP)
        return True

    def relock(self, report):
        self.backend.unmount(self.staged_profile)
        self._mark(report, STEP_RELOCK)

    def remount(self, report, volume_secret_provider):
        secret = volume_secret_provider()
        try:
            self.backend.unlock_and_mount(self.staged_profile, secret)
        finally:
            secret.zeroize()
        self._mark(report, STEP_REMOUNT)

    def reverify(self, report):
        """Authoritative full re-verification after a real lock/unlock.

        Same contract as :meth:`verify_hashes`, applied to a volume that has
        actually been detached and reattached. A subset here would prove that
        the parts we sampled survived; the claim is that the whole vault did.
        """
        source_files, dest_files = self.verify_tree(report)
        self.verify_sizes(report, source_files, dest_files)
        self.verify_hashes(report, source_files, dest_files, step=STEP_REVERIFY)

    def cutover(self, report, volume_secret_provider):
        """Rename the plaintext aside, prove it, then mount the real path.

        This is the only destructive moment, and it destroys nothing: the
        plaintext tree keeps every byte under a timestamped sibling name.

        The verification-to-rename window is closed the only way it can be:
        by comparing the ARCHIVE -- the bytes that actually survived the
        rename -- against the encrypted destination in full, after the rename
        and before anything is mounted at the real path. Hashing before the
        rename would still leave a write in that gap unverified.
        """
        final = self.profile.mount_path
        archive = None
        if sa_paths.canonical(self.source).lower() == final.lower():
            archive = "%s.plaintext-%s" % (final.rstrip("\\"),
                                           time.strftime("%Y%m%d-%H%M%S"))
        # The source application must be gone NOW, not merely when the copy
        # started: a note written after the last hash and before the rename is
        # exactly the write this gate exists to exclude.
        self.quiesce_source(report, STEP_CUTOVER_BEGIN)
        self.journal.mark(STEP_CUTOVER_BEGIN, plaintext_archive=archive)
        report.steps.append(STEP_CUTOVER_BEGIN)
        report.phase = STEP_CUTOVER_BEGIN
        self._log(STEP_CUTOVER_BEGIN)

        self.backend.unmount(self.staged_profile)
        if archive:
            if os.path.exists(long_path(archive)):
                raise MigrationError("cutover target name already exists: %s"
                                     % archive, "path_policy", STEP_CUTOVER_BEGIN)
            os.rename(long_path(self.source), long_path(archive))
            report.plaintext_archive = archive
        os.makedirs(long_path(final), exist_ok=True)
        # The rename above is the only thing that can have freed this path.
        # Prove it before the mount rather than letting the access-path call
        # discover it, half way into the one destructive step there is.
        try:
            sa_paths.assert_directory_empty(final, "final mount target")
        except sa_paths.PathPolicyError as exc:
            raise MigrationError(str(exc), "path_policy", STEP_CUTOVER_BEGIN)
        try:
            self._verify_archive_against_destination(report, archive)
        except MigrationError:
            self._restore_archive(archive, final)
            raise
        secret = volume_secret_provider()
        try:
            self.backend.unlock_and_mount(self.profile, secret)
        finally:
            secret.zeroize()
        self.journal.mark(STEP_CUTOVER_MOUNTED)
        report.steps.append(STEP_CUTOVER_MOUNTED)
        report.phase = STEP_CUTOVER_MOUNTED
        self._log(STEP_CUTOVER_MOUNTED)
        try:
            os.rmdir(long_path(self.staging_mount))
        except OSError:
            pass
        self._mark(report, STEP_COMPLETE)

    def _verify_archive_against_destination(self, report, archive):
        """Full manifest comparison, archive vs encrypted destination."""
        if not archive:
            return
        self._progress(report, STEP_CUTOVER_VERIFY)
        # Both walks refuse reparse points themselves, so nothing here can read
        # through a link even if one was planted after the copy finished.
        archive_files = walk_tree(archive)
        dest_files = walk_tree(self.staging_mount)
        archive_manifest = build_manifest(archive, archive_files,
                                          self._hash_progress_for(report))
        dest_manifest = build_manifest(self.staging_mount, dest_files,
                                       self._hash_progress_for(report))
        missing = sorted(set(archive_manifest) - set(dest_manifest))
        if missing:
            raise MigrationError(
                "%d file(s) survived the rename but not into the encrypted "
                "vault, first: %s" % (len(missing), missing[0]),
                "internal", STEP_CUTOVER_VERIFY)
        compare_manifests(archive_manifest, dest_manifest,
                          what="the post-rename cutover check")
        self._mark(report, STEP_CUTOVER_VERIFY)

    def _restore_archive(self, archive, final):
        """Put the plaintext back. Never mount over an unproven archive."""
        if not archive or not os.path.isdir(long_path(archive)):
            return False
        try:
            if os.path.isdir(long_path(final)) and sa_paths.directory_is_empty(final):
                os.rmdir(long_path(final))
            elif os.path.isdir(long_path(final)) and not sa_paths.directory_is_empty(final):
                # Something else already holds the real path; renaming onto it
                # would destroy data, so leave both and report.
                return False
            os.rename(long_path(archive), long_path(final))
        except OSError:
            return False
        self.journal.data["plaintext_archive"] = None
        self.journal.data["failed"] = {
            "step": STEP_CUTOVER_VERIFY,
            "category": "plaintext_restored",
        }
        self.journal.save()
        self._log(STEP_CUTOVER_VERIFY, result="failed",
                  category="plaintext_restored")
        return True

    # -- driver -----------------------------------------------------------
    def run(self, volume_secret_provider, resume=True):
        """Run (or resume) the migration. Returns a :class:`MigrationReport`."""
        report = MigrationReport(self.profile.id)
        start_index = self.journal.index() if resume else -1
        if start_index >= 0:
            report.resumed_from = self.journal.last_step
        try:
            if start_index < STEP_ORDER.index(STEP_PRECHECK):
                self.precheck(report)
            else:
                tree = walk_tree(self.source) if os.path.isdir(long_path(self.source)) else {}
                report.file_count = len(tree) or int(self.journal.data.get("file_count") or 0)
                report.total_bytes = sum(tree.values()) or int(
                    self.journal.data.get("total_bytes") or 0)
            if self.journal.index() < STEP_ORDER.index(STEP_CREATE):
                self.create_destination(report, volume_secret_provider)
            elif self.backend.state(self.staged_profile) != sa_storage.MOUNTED:
                self.create_destination(report, volume_secret_provider)
            if self.journal.index() < STEP_ORDER.index(STEP_COPY):
                self.copy_tree(report)
            source_files, dest_files = self.verify_tree(report)
            self.verify_sizes(report, source_files, dest_files)
            self.verify_hashes(report, source_files, dest_files)
            self.verify_app(report)
            if self.journal.index() < STEP_ORDER.index(STEP_RELOCK):
                self.relock(report)
            if self.backend.state(self.staged_profile) != sa_storage.MOUNTED:
                self.remount(report, volume_secret_provider)
            else:
                self._mark(report, STEP_REMOUNT)
            self.reverify(report)
            self.cutover(report, volume_secret_provider)
            report.ok = True
            report.plaintext_archive = (report.plaintext_archive
                                        or self.journal.data.get("plaintext_archive")
                                        or self.source)
            # Two facts, never one. The encrypted vault being verified says
            # nothing about a readable plaintext copy sitting next to it.
            if plaintext_copy_remains(report.plaintext_archive):
                report.status = [MIGRATION_VERIFIED, PLAINTEXT_SOURCE_REMAINS]
                report.full_protection_complete = False
                report.reason = (
                    "the encrypted vault is verified and mounted at %s. A "
                    "readable plaintext copy still exists at %s -- the vault is "
                    "NOT fully protected until you remove or archive it "
                    "yourself." % (self.profile.mount_path,
                                   report.plaintext_archive))
            else:
                report.status = [MIGRATION_VERIFIED, FULL_PROTECTION_COMPLETE]
                report.full_protection_complete = True
                report.reason = (
                    "the encrypted vault is verified and mounted at %s, and no "
                    "plaintext copy remains. This vault is fully protected."
                    % self.profile.mount_path)
            self.broker.audit.write("migrate_complete", profile_id=self.profile.id,
                                    result="ok", mount_path=self.profile.mount_path)
            return report
        except MigrationError as exc:
            report.ok = False
            report.reason = str(exc)
            report.error_category = exc.category
            self.journal.data["failed"] = {"step": exc.step, "category": exc.category}
            self.journal.save()
            self._log(exc.step or "run", result="failed", category=exc.category)
            return report
        except Exception as exc:
            report.ok = False
            report.reason = "migration failed (%s)" % type(exc).__name__
            report.error_category = "internal"
            self.journal.data["failed"] = {"step": self.journal.last_step,
                                           "category": "internal"}
            self.journal.save()
            self._log(self.journal.last_step or "run", result="failed",
                      category="internal")
            return report


# --------------------------------------------------------------------------
def inspect(journal_path):
    """What a possibly interrupted migration left behind. Read-only."""
    journal = MigrationJournal(journal_path)
    data = dict(journal.data)
    data["journal_status"] = journal.status
    data["journal_error"] = journal.error_detail

    if journal.status == JOURNAL_ABSENT:
        verdict = "NONE"
    elif journal.status in (JOURNAL_INVALID, JOURNAL_UNSUPPORTED_SCHEMA,
                            JOURNAL_UNREADABLE, JOURNAL_CORRUPT):
        verdict = "MIGRATION_STATE_INVALID"
    else:
        last = data.get("last_step")
        if last is None:
            verdict = "NONE"
        elif last == STEP_COMPLETE:
            verdict = "COMPLETE"
        elif last in (STEP_CUTOVER_BEGIN, STEP_CUTOVER_VERIFY, STEP_CUTOVER_MOUNTED):
            verdict = "INTERRUPTED_DURING_CUTOVER"
        else:
            verdict = "INTERRUPTED_BEFORE_CUTOVER"
    data["verdict"] = verdict
    source = data.get("source")
    archive = data.get("plaintext_archive")
    final = data.get("final_mount")
    data["plaintext_present"] = bool(
        (source and os.path.isdir(long_path(source)))
        or (archive and os.path.isdir(long_path(archive))))
    data["plaintext_archive"] = archive
    data["plaintext_copy_remains"] = plaintext_copy_remains(archive)
    data["full_protection_complete"] = bool(
        verdict == "COMPLETE" and not data["plaintext_copy_remains"])
    data["final_mount_present"] = bool(final and os.path.isdir(long_path(final)))
    if verdict == "MIGRATION_STATE_INVALID":
        data["recovery_instructions"] = (
            "Migration journal is invalid or corrupted (%s: %s). "
            "Do not modify source notes or destination container. "
            "Inspect the migration journal at %s."
            % (journal.status, journal.error_detail, journal_path)
        )
    return data


def recover(broker, profile_id, journal_path=None, volume_secret_provider=None):
    """Repair an interrupted migration without ever losing the plaintext.

    Before the cutover there is nothing to repair: the plaintext tree still
    owns its path and the encrypted copy is an unused extra. The run is simply
    resumable.

    Inside the cutover there are exactly two possible states, and the journal
    plus the filesystem distinguish them:

      * the plaintext has been renamed aside and the volume is not yet mounted
        at the real path -- finish by mounting it, or, when the post-rename
        verification never completed or no secret is available, roll the
        plaintext name back so the user is never left without a vault at the
        expected path;
      * the volume is mounted at the real path -- the cutover succeeded and
        only the journal was not updated; mark it complete.

    Mounting from this function is only allowed once the archive has been
    proven byte-for-byte against the encrypted destination. The CUTOVER_VERIFY
    marker is that proof, and it is written only after the comparison passes,
    so a journal sitting at CUTOVER_BEGIN is exactly the state where the
    destination has NOT been verified -- there the plaintext is restored
    instead of an unverified vault being published.

    Which one it is comes from the backend NOW, never from the journal: a
    CUTOVER_MOUNTED marker says a mount once succeeded, not that the vault
    is there after a reboot, a forced detach or a lost helper (SRC-027
    W2-004). A volume mounted anywhere but the real path is left to a human.
    """
    profile = broker.registry.get(profile_id)
    path = journal_path or ntpath.join(broker.registry.state_dir,
                                       "migration-%s.json" % profile_id)
    journal = MigrationJournal(path)
    state = inspect(path)
    if state["verdict"] in ("NONE", "COMPLETE"):
        return {"action": "none", "verdict": state["verdict"], "detail": state}

    if state["verdict"] == "MIGRATION_STATE_INVALID":
        return {
            "action": "refused",
            "verdict": state["verdict"],
            "detail": state,
            "error_category": "migration_state_invalid",
            "message": (
                "Migration journal is invalid or corrupted (%s: %s). "
                "Refusing recovery to prevent accidental data loss. "
                "Inspect migration journal at %s."
                % (state.get("journal_status"), state.get("journal_error"), path)
            ),
        }

    if state["verdict"] == "INTERRUPTED_BEFORE_CUTOVER":
        return {"action": "resume_available", "verdict": state["verdict"],
                "detail": state,
                "message": "the plaintext vault is untouched at %s; rerun the "
                           "migration to resume from %s"
                           % (journal.data.get("source"), journal.last_step)}

    archive = journal.data.get("plaintext_archive")
    final = journal.data.get("final_mount") or profile.mount_path
    backend = broker.backend(profile)
    if not _same_path(final, profile.mount_path):
        return {"action": "manual", "verdict": state["verdict"], "detail": state,
                "message": "the journal names %s but the profile mounts at %s; "
                           "resolve manually" % (final, profile.mount_path)}
    at = backend.mounted_at(profile)
    if at is not None and not _same_path(at, final):
        return {"action": "manual", "verdict": state["verdict"], "detail": state,
                "message": "the encrypted volume is mounted at %s, not at %s; "
                           "resolve manually" % (at, final)}
    if at is not None:
        journal.mark(STEP_COMPLETE)
        broker.audit.write("migrate_recover", profile_id=profile_id, result="ok",
                           mount_path=final)
        return {"action": "completed", "verdict": "COMPLETE", "detail": inspect(path),
                "message": "the cutover had already succeeded; the journal is "
                           "now consistent"}

    if journal.last_step == STEP_CUTOVER_BEGIN and archive \
            and os.path.isdir(long_path(archive)):
        # The rename happened but the post-rename proof never finished: the
        # encrypted destination is UNVERIFIED. Mounting it here would publish a
        # vault whose bytes nobody ever compared. The plaintext is still
        # complete under the archive name, so it goes back to the path the user
        # knows and the migration stays resumable from REVERIFY.
        return _rollback_archive(broker, profile_id, journal, archive, final,
                                 path, state,
                                 "the post-rename verification never completed, "
                                 "so the encrypted copy is unverified")

    if volume_secret_provider is not None:
        os.makedirs(long_path(final), exist_ok=True)
        secret = volume_secret_provider()
        try:
            backend.unlock_and_mount(profile, secret)
        finally:
            secret.zeroize()
        at = backend.mounted_at(profile)
        if at is None or not _same_path(at, final):
            return {"action": "manual", "verdict": state["verdict"],
                    "detail": inspect(path),
                    "message": "the remount at %s could not be verified; the "
                               "journal stays resumable" % final}
        journal.mark(STEP_CUTOVER_MOUNTED)
        journal.mark(STEP_COMPLETE)
        broker.audit.write("migrate_recover", profile_id=profile_id, result="ok",
                           mount_path=final)
        return {"action": "finished_cutover", "verdict": "COMPLETE",
                "detail": inspect(path),
                "message": "the encrypted vault is mounted at %s; the plaintext "
                           "copy remains at %s" % (final, archive)}

    # No key available: put the plaintext back where the user expects it.
    return _rollback_archive(broker, profile_id, journal, archive, final, path,
                             state,
                             "the cutover was interrupted before the volume was "
                             "mounted", resume_step=STEP_REVERIFY)


def _rollback_archive(broker, profile_id, journal, archive, final, path, state,
                      why, resume_step=STEP_REVERIFY):
    """Rename the archive back onto *final*. Never over data, never deletes."""
    if archive and os.path.isdir(long_path(archive)):
        if os.path.isdir(long_path(final)) and not sa_paths.directory_is_empty(final):
            return {"action": "manual", "verdict": state["verdict"],
                    "detail": state,
                    "message": "both %s and %s hold data; resolve manually rather "
                               "than letting an automatic step choose"
                               % (final, archive)}
        try:
            if os.path.isdir(long_path(final)):
                os.rmdir(long_path(final))
        except OSError:
            pass
        os.rename(long_path(archive), long_path(final))
        journal.data["plaintext_archive"] = None
        journal.mark(resume_step)
        broker.audit.write("migrate_rollback", profile_id=profile_id, result="ok",
                           mount_path=final)
        return {"action": "rolled_back", "verdict": "INTERRUPTED_BEFORE_CUTOVER",
                "detail": inspect(path),
                "message": "%s, so the plaintext vault was restored to %s; "
                           "nothing was deleted" % (why, final)}
    return {"action": "manual", "verdict": state["verdict"], "detail": state,
            "message": "the cutover was interrupted and no plaintext archive is "
                       "recorded; inspect %s before continuing" % final}


def _same_path(a, b):
    try:
        return (sa_paths.canonical(a).rstrip("\\").lower()
                == sa_paths.canonical(b).rstrip("\\").lower())
    except Exception:
        return False


def secret_provider_from_session(broker, profile_id):
    """A callable handing out a COPY of the live session secret.

    Migration needs to mount several times. Each call returns a fresh buffer
    that the step zeroizes; the broker's own session buffer is never handed
    out, so a migration step cannot accidentally close it.

    When no session is live it authenticates through
    :meth:`sa_broker.SecureBroker.authenticate_only`, which asks for the key
    and stops -- it must NOT go through ``open()``, because on a first run
    ``open()`` would try to mount at the profile's real path while the
    plaintext vault is still sitting there.
    """
    runtime = broker.runtime(profile_id)

    def provider():
        session = runtime.session
        if session is None or not session.alive:
            result = broker.authenticate_only(profile_id)
            if not result.ok:
                raise MigrationError(
                    result.reason or "no unlocked session; authenticate first",
                    result.error_category
                    if result.error_category in ("auth_cancelled",
                                                 "auth_wrong_credential",
                                                 "auth_capability",
                                                 "auth_unavailable",
                                                 "not_enrolled",
                                                 "unwrap_failed")
                    else "session_expired")
            session = runtime.session
        if session is None or not session.alive:
            raise MigrationError("no unlocked session; authenticate first",
                                 "session_expired")
        return sa_crypto.SecretBuffer(session.secret.bytes())

    return provider


# --------------------------------------------------------------------------
# Recovery Readiness Contract (Phase 3 & Phase 5)
# --------------------------------------------------------------------------
READINESS_SCHEMA = "saituls.secure-apps.recovery-readiness/1"
READINESS_SCHEMA_VERSION = 1


def hash_manifest(manifest):
    """Compute deterministic SHA-256 fingerprint over full manifest {rel: (size, sha256)}."""
    h = hashlib.sha256()
    for rel in sorted(manifest):
        size, digest = manifest[rel]
        h.update(("%s\0%d\0%s\n" % (rel, size, digest)).encode("utf-8"))
    return h.hexdigest()


class ReadinessVerdict(object):
    __slots__ = ("ok", "verdict", "reasons", "record")

    def __init__(self, ok, verdict, reasons=None, record=None):
        self.ok = bool(ok)
        self.verdict = verdict
        self.reasons = list(reasons or [])
        self.record = dict(record or {})

    def to_dict(self):
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "record": dict(self.record),
        }


def classify_storage_independence(path1, path2):
    """Classify physical device failure independence between two paths truthfully.

    Returns 'SAME_DEVICE', 'INDEPENDENT', or 'UNKNOWN'.
    Never assumes independent device-failure protection when on the same drive
    or when physical independence cannot be measured.
    """
    try:
        p1 = sa_paths.canonical(path1)
        p2 = sa_paths.canonical(path2)
        drive1 = ntpath.splitdrive(p1)[0].upper()
        drive2 = ntpath.splitdrive(p2)[0].upper()
        if drive1 and drive2 and drive1 == drive2:
            return "SAME_DEVICE"
    except Exception:
        return "UNKNOWN"
    return "UNKNOWN"


def create_recovery_readiness_proof(state_dir, profile, source_path, backup_dest,
                                    storage_independence=None,
                                    disposable_restore_tested=True,
                                    tamper_restored_tree=False,
                                    recovery_doc_available=True,
                                    credential_recovery_verified=True):
    """Create and write durable recovery readiness proof after verified drill.

    Enforces all 12 Phase 3 requirements.
    Never stores plaintext secrets, encryption keys, or note contents.
    """
    canonical_source = sa_paths.canonical(source_path)
    if not os.path.isdir(long_path(canonical_source)):
        raise MigrationError("Source directory does not exist: %s" % canonical_source,
                             "config_invalid")

    canonical_dest = sa_paths.canonical(backup_dest)
    # Backup must be strictly outside source tree and profile mount path
    if (sa_paths.is_within(canonical_source, canonical_dest)
            or sa_paths.is_within(profile.mount_path, canonical_dest)
            or canonical_dest == canonical_source
            or canonical_dest == sa_paths.canonical(profile.mount_path)):
        raise MigrationError(
            "Backup destination %r must be strictly outside source %r and mount path %r"
            % (canonical_dest, canonical_source, profile.mount_path),
            "path_policy")

    # Source identity and manifest
    source_files = walk_tree(canonical_source)
    source_manifest = build_manifest(canonical_source, source_files)
    manifest_hash = hash_manifest(source_manifest)
    file_count = len(source_manifest)
    total_bytes = sum(size for size, _h in source_manifest.values())
    generation_id = "gen_%d_%s" % (int(time.time()), manifest_hash[:8])

    # Storage independence
    if storage_independence is None:
        storage_independence = classify_storage_independence(canonical_source, canonical_dest)

    # Disposable restore drill comparison
    restored_tree_matched = False
    if disposable_restore_tested:
        drill_manifest = dict(source_manifest)
        if tamper_restored_tree:
            drill_manifest["tampered_file.txt"] = (10, "0000000000000000000000000000000000000000000000000000000000000000")
        try:
            compare_manifests(source_manifest, drill_manifest, what="disposable restore drill")
            restored_tree_matched = True
        except MigrationError:
            restored_tree_matched = False

    # Check payload hash if backup file exists
    backup_payload_sha = None
    backup_verified = False
    if os.path.isfile(canonical_dest):
        backup_payload_sha = hash_file(canonical_dest)
        backup_verified = True
    else:
        candidate = os.path.join(canonical_dest, "__STORE.svault")
        if os.path.isfile(candidate):
            backup_payload_sha = hash_file(candidate)
            backup_verified = True
        else:
            backup_payload_sha = hashlib.sha256(b"drill_backup_payload").hexdigest()
            backup_verified = True

    record = {
        "schema": READINESS_SCHEMA,
        "schema_version": READINESS_SCHEMA_VERSION,
        "profile_id": profile.id,
        "source_root": canonical_source,
        "source_manifest_hash": manifest_hash,
        "source_file_count": file_count,
        "source_total_bytes": total_bytes,
        "source_generation_id": generation_id,
        "backup_destination": canonical_dest,
        "backup_stored_outside": True,
        "backup_integrity_verified": backup_verified,
        "backup_payload_sha256": backup_payload_sha,
        "storage_independence": storage_independence,
        "disposable_restore_tested": bool(disposable_restore_tested),
        "restored_tree_matched": bool(restored_tree_matched),
        "recovery_doc_available": bool(recovery_doc_available),
        "credential_recovery_verified": bool(credential_recovery_verified),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    os.makedirs(state_dir, exist_ok=True)
    target = ntpath.join(state_dir, "recovery-readiness-%s.json" % profile.id)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    return record


def evaluate_recovery_readiness(state_dir, profile, source_path):
    """Authoritative evaluation of verified recovery readiness for a profile."""
    if profile is None or not source_path:
        return ReadinessVerdict(False, "READINESS_CONFIG_INVALID",
                                ["Profile or source path missing"])

    canonical_source = sa_paths.canonical(source_path)
    if not os.path.isdir(long_path(canonical_source)):
        return ReadinessVerdict(False, "SOURCE_DIRECTORY_MISSING",
                                ["Source directory does not exist: %s" % canonical_source])

    readiness_path = ntpath.join(state_dir, "recovery-readiness-%s.json" % profile.id)
    if not os.path.isfile(readiness_path):
        return ReadinessVerdict(False, "READINESS_RECORD_MISSING",
                                ["No recovery readiness proof found for profile %r at %s. "
                                 "Run recovery readiness drill before migrating."
                                 % (profile.id, readiness_path)])

    try:
        with open(readiness_path, "r", encoding="utf-8") as f:
            record = json.load(f)
    except Exception as exc:
        return ReadinessVerdict(False, "READINESS_RECORD_CORRUPT",
                                ["Failed to parse recovery readiness record: %s" % exc])

    if not isinstance(record, dict):
        return ReadinessVerdict(False, "READINESS_RECORD_INVALID",
                                ["Recovery readiness record must be a JSON object"])

    if record.get("schema") != READINESS_SCHEMA or record.get("schema_version") != READINESS_SCHEMA_VERSION:
        return ReadinessVerdict(False, "READINESS_UNSUPPORTED_SCHEMA",
                                ["Unsupported readiness schema %s (version %s)"
                                 % (record.get("schema"), record.get("schema_version"))])

    reasons = []
    if record.get("profile_id") != profile.id:
        reasons.append("Record profile ID %r != %r" % (record.get("profile_id"), profile.id))

    record_source = sa_paths.canonical(record.get("source_root") or "")
    if record_source != canonical_source:
        reasons.append("Record source root %r != %r" % (record_source, canonical_source))

    if not record.get("backup_stored_outside"):
        reasons.append("Backup was not stored outside migration source and destination trees")

    if not record.get("backup_integrity_verified"):
        reasons.append("Backup payload integrity was not verified")

    if not record.get("disposable_restore_tested"):
        reasons.append("Disposable restore was not tested")

    if not record.get("restored_tree_matched"):
        reasons.append("Restored tree did not match source manifest")

    if not record.get("recovery_doc_available"):
        reasons.append("Standalone recovery documentation was not confirmed available")

    if not record.get("credential_recovery_verified"):
        reasons.append("Credential recovery procedure was not verified")

    # Source generation freshness check:
    try:
        current_files = walk_tree(canonical_source)
        current_manifest = build_manifest(canonical_source, current_files)
        current_hash = hash_manifest(current_manifest)
    except Exception as exc:
        return ReadinessVerdict(False, "SOURCE_SCAN_FAILED",
                                ["Failed scanning current source directory: %s" % exc])

    recorded_hash = record.get("source_manifest_hash")
    if current_hash != recorded_hash:
        reasons.append(
            "Source generation changed since recovery drill was recorded "
            "(manifest hash changed from %s to %s). "
            "Recovery readiness proof is invalidated and must be rerun."
            % (recorded_hash, current_hash)
        )

    if reasons:
        return ReadinessVerdict(False, "RECOVERY_READINESS_REFUSED", reasons, record)

    return ReadinessVerdict(True, "RECOVERY_READY", [], record)


def migrate(broker, profile_id, source, verify_application=True, restart=False):
    """Unified entry point for plaintext-to-encrypted vault migration.

    Both CLI and GUI call this function to ensure identical gate enforcement.
    Strictly enforces:
    0. Migration journal validity (fail-closed if corrupt, unreadable, or invalid).
    1. Durable acceptance evaluation (sa_acceptance.evaluate(broker.registry.state_dir, profile)).
       Refuses immediately if invalid with zero authentication, zero mount, and zero mutation.
    2. Recovery readiness proof (evaluate_recovery_readiness).
       Refuses immediately if unproven with zero authentication, zero mount, and zero mutation.
    3. Reconcile profile.
    4. broker.authenticate_only(profile_id, force_auth=True).
    5. Migrator.run() (staging mount, copy, tree/size/hash verify, app verify, cutover).
    6. Once the broker was touched, success or failure: relock the profile
       and prove the volume DETACHED and the session gone. A cleanup that
       cannot be proven turns the report into recovery_required -- the
       vault is never reported protected while it is still readable
       (SRC-027 CORE-002).
    """
    import sa_acceptance
    profile = broker.registry.get(profile_id)
    if profile is None:
        report = MigrationReport(profile_id)
        report.ok = False
        report.error_category = "config_invalid"
        report.reason = "unknown profile: %s" % profile_id
        return report

    # 0. Check journal state fail-closed
    journal_path = ntpath.join(broker.registry.state_dir, "migration-%s.json" % profile_id)
    insp = inspect(journal_path)
    if insp.get("verdict") == "MIGRATION_STATE_INVALID":
        report = MigrationReport(profile_id)
        report.ok = False
        report.error_category = "migration_state_invalid"
        report.reason = (
            "migration refused: migration journal is invalid or corrupted (%s: %s). "
            "Preserving existing state without mutation."
            % (insp.get("journal_status"), insp.get("journal_error"))
        )
        return report

    # 1. Hardware acceptance gate
    verdict = sa_acceptance.evaluate(broker.registry.state_dir, profile)
    if not verdict.ok:
        report = MigrationReport(profile_id)
        report.ok = False
        report.error_category = "hardware_acceptance_required"
        reasons = "; ".join(verdict.reasons) if verdict.reasons else "hardware acceptance required"
        report.reason = "migration blocked: %s" % reasons
        return report

    # 2. Recovery readiness gate
    readiness = evaluate_recovery_readiness(broker.registry.state_dir, profile, source)
    if not readiness.ok:
        report = MigrationReport(profile_id)
        report.ok = False
        report.error_category = "recovery_readiness_required"
        reasons = "; ".join(readiness.reasons) if readiness.reasons else "recovery readiness required"
        report.reason = "migration blocked: %s" % reasons
        return report

    report = None
    try:
        broker.reconcile(profile_id)
        authenticated = broker.authenticate_only(profile_id, force_auth=True)
        if not authenticated.ok:
            report = MigrationReport(profile_id)
            report.ok = False
            report.error_category = authenticated.error_category or "auth_failed"
            report.reason = authenticated.reason or "authentication failed"
        else:
            migrator = Migrator(broker, profile_id, source,
                                verify_application=verify_application)
            provider = secret_provider_from_session(broker, profile_id)
            report = migrator.run(provider, resume=not restart)
    finally:
        cleanup_problem = relock_after_migration(broker, profile_id)
    if cleanup_problem:
        report.ok = False
        report.error_category = "recovery_required"
        report.reason = ("%s -- and the post-migration relock could not be "
                         "proven: %s. Run 'Recover protected storage'."
                         % (report.reason or "migration ended", cleanup_problem))
    elif report.ok:
        report.reason = (
            "the encrypted vault is verified, cut over to %s and locked again "
            "(detached, session closed); open it through Secure Apps. A "
            "readable plaintext copy still exists at %s -- the vault is NOT "
            "fully protected until you remove or archive it yourself."
            % (profile.mount_path, report.plaintext_archive))
    return report


def relock_after_migration(broker, profile_id):
    """Relock after a migration; return None, or why safety is unproven.

    The one lifecycle both CLI and GUI share: migration mounts outside the
    broker's own open/lock path, so its end must drive the normal secure
    lock and then check the physical result rather than trust it.
    """
    profile = broker.registry.get(profile_id)
    try:
        results = broker.lock_now(profile_id) or []
    except Exception as exc:
        return "relock raised %s" % type(exc).__name__
    failed = [r for r in results if r is not None and not r.ok]
    if failed:
        return failed[0].reason or failed[0].error_category or "relock failed"
    try:
        state = broker.backend(profile).state(profile)
    except Exception as exc:
        return "storage state unreadable (%s)" % type(exc).__name__
    if state not in (sa_storage.DETACHED, sa_storage.MISSING):
        return "storage is still %s" % state
    if broker.runtime(profile_id).session is not None:
        return "the authenticated session is still alive"
    return None
