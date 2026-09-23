"""Plaintext-to-encrypted vault migration for SAITULS Secure Apps.

Migration is destructive only at the very last step, and even then only to a
*name*: the plaintext tree is renamed aside, never deleted. Everything before
the cutover is additive, so an interruption at any point leaves the user's
original vault exactly where it was.

The sequence, and why each step is there:

===================  ========================================================
PRECHECK             source exists, destination does not, space is plausible
CREATE               the container ENROLLMENT already built is mounted at a
                     STAGING path, so the plaintext original keeps owning the
                     real path for the whole copy and verify
COPY                 whole tree copied; content is never inspected, parsed,
                     normalised or rewritten
VERIFY_TREE          every relative path present on both sides, both ways
                     (volume-owned roots such as System Volume Information
                     excluded: they belong to the NTFS volume, not the vault)
VERIFY_SIZES         byte size equal for every file
VERIFY_HASHES        SHA-256 over a deterministic representative sample
VERIFY_APP           the protected application opens the encrypted copy
CLOSE_APP            and is closed again, cleanly
RELOCK               container detached: proves the lock path works
REMOUNT              container reattached at the staging path
REVERIFY             tree + sizes again, after a real lock/unlock round trip
CUTOVER              plaintext renamed aside, volume remounted at the real
                     path, original left in place
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

import sa_crypto
import sa_paths
import sa_storage

JOURNAL_SCHEMA = "saituls.secure-apps.migration/1"
JOURNAL_SCHEMA_VERSION = 1

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
STEP_CUTOVER_MOUNTED = "CUTOVER_MOUNTED"
STEP_COMPLETE = "COMPLETE"

STEP_ORDER = (STEP_PRECHECK, STEP_CREATE, STEP_COPY, STEP_VERIFY_TREE,
              STEP_VERIFY_SIZES, STEP_VERIFY_HASHES, STEP_VERIFY_APP,
              STEP_CLOSE_APP, STEP_RELOCK, STEP_REMOUNT, STEP_REVERIFY,
              STEP_CUTOVER_BEGIN, STEP_CUTOVER_MOUNTED, STEP_COMPLETE)

MIGRATION_VERIFIED = "MIGRATION_VERIFIED"
PLAINTEXT_SOURCE_REMAINS = "PLAINTEXT_SOURCE_REMAINS"

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
    """Small durable record of how far the migration got. Never a secret."""

    def __init__(self, path):
        self.path = path
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
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return self.data
        if isinstance(document, dict) and document.get("schema") == JOURNAL_SCHEMA:
            self.data.update(document)
        return self.data

    def save(self):
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
        try:
            os.remove(self.path)
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
    """One os.walk: files -> {rel:size} and directories -> {rel}."""
    files, dirs = {}, set()
    base = long_path(root)
    for dirpath, dirnames, filenames in os.walk(base):
        if dirpath == base:
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in VOLUME_OWNED_ROOT_DIRECTORIES]
        for name in dirnames:
            full = os.path.join(dirpath, name)
            dirs.add(os.path.relpath(full, base).lower())
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, base)
            if _is_volume_owned(rel):
                continue
            try:
                files[rel.lower()] = os.path.getsize(full)
            except OSError as exc:
                raise MigrationError("could not stat %s (%s)"
                                     % (rel, type(exc).__name__), "internal")
    return files, dirs

scan_tree = _scan_root

def walk_tree(root):
    files, _dirs = _scan_root(root)
    return files


def directories_of(root):
    _files, dirs = _scan_root(root)
    return dirs


def representative_sample(relpaths_to_sizes, limit=HASH_SAMPLE_MAX):
    """Deterministic sample: extremes plus an even stride over sorted paths.

    Deterministic so a re-verification after the lock/unlock round trip hashes
    the SAME files, which is what makes the second pass meaningful.
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


class MigrationReport(object):
    __slots__ = ("ok", "profile_id", "status", "steps", "file_count",
                 "total_bytes", "hashed", "plaintext_archive", "reason",
                 "error_category", "resumed_from")

    def __init__(self, profile_id):
        self.ok = False
        self.profile_id = profile_id
        self.status = []
        self.steps = []
        self.file_count = 0
        self.total_bytes = 0
        self.hashed = 0
        self.plaintext_archive = None
        self.reason = ""
        self.error_category = "none"
        self.resumed_from = None

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}


class Migrator(object):
    """Runs (and resumes) the migration for one profile."""

    def __init__(self, broker, profile_id, source, journal_path=None,
                 staging_mount=None, verify_application=True,
                 app_probe_seconds=8):
        self.broker = broker
        self.profile = broker.registry.get(profile_id)
        self.runtime = broker.runtime(profile_id)
        self.backend = broker.backend(self.profile)
        self.source = sa_paths.canonical(source)
        self.verify_application = verify_application
        self.app_probe_seconds = app_probe_seconds
        self.staging_mount = sa_paths.canonical(
            staging_mount or sa_paths.staging_mount_path(self.profile.container))
        self.staged_profile = self.profile.with_mount_path(self.staging_mount)
        self.journal = MigrationJournal(
            journal_path or ntpath.join(broker.registry.state_dir,
                                        "migration-%s.json" % profile_id))

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
        self._log(step)
        return step

    # -- steps ------------------------------------------------------------
    def precheck(self, report):
        if not os.path.isdir(long_path(self.source)):
            raise MigrationError("plaintext source not found: %s" % self.source,
                                 "path_policy", STEP_PRECHECK)
        if sa_paths.is_within(self.source, self.profile.container):
            raise MigrationError("the container may not live inside the vault "
                                 "being migrated", "path_policy", STEP_PRECHECK)
        tree = walk_tree(self.source)
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
        src = long_path(self.source)
        dst = long_path(self.staging_mount)
        for dirpath, dirnames, filenames in os.walk(src):
            if dirpath == src:
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in VOLUME_OWNED_ROOT_DIRECTORIES]
            rel = os.path.relpath(dirpath, src)
            target_dir = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(target_dir, exist_ok=True)
            for name in dirnames:
                os.makedirs(os.path.join(target_dir, name), exist_ok=True)
            for name in filenames:
                source_file = os.path.join(dirpath, name)
                target_file = os.path.join(target_dir, name)
                # copy2 preserves mtime/atime and the mode bits. Content is
                # copied byte for byte: notes are never parsed or rewritten.
                shutil.copy2(source_file, target_file)
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

    def verify_hashes(self, report, source_files, step=STEP_VERIFY_HASHES):
        sample = representative_sample(source_files)
        for rel in sample:
            src = os.path.join(long_path(self.source), rel)
            dst = os.path.join(long_path(self.staging_mount), rel)
            if hash_file(src) != hash_file(dst):
                raise MigrationError("content hash differs for %s" % rel,
                                     "internal", step)
        report.hashed = len(sample)
        self._mark(report, step)

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
        source_files, dest_files = self.verify_tree(report)
        self.verify_sizes(report, source_files, dest_files)
        sample = representative_sample(source_files)
        for rel in sample:
            src = os.path.join(long_path(self.source), rel)
            dst = os.path.join(long_path(self.staging_mount), rel)
            if hash_file(src) != hash_file(dst):
                raise MigrationError("content hash differs after the lock/unlock "
                                     "round trip for %s" % rel, "internal",
                                     STEP_REVERIFY)
        self._mark(report, STEP_REVERIFY)

    def cutover(self, report, volume_secret_provider):
        """Rename the plaintext aside, mount the volume at the real path.

        This is the only destructive moment, and it destroys nothing: the
        plaintext tree keeps every byte under a timestamped sibling name.
        """
        final = self.profile.mount_path
        archive = None
        if sa_paths.canonical(self.source).lower() == final.lower():
            archive = "%s.plaintext-%s" % (final.rstrip("\\"),
                                           time.strftime("%Y%m%d-%H%M%S"))
        self.journal.mark(STEP_CUTOVER_BEGIN, plaintext_archive=archive)
        report.steps.append(STEP_CUTOVER_BEGIN)
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
        secret = volume_secret_provider()
        try:
            self.backend.unlock_and_mount(self.profile, secret)
        finally:
            secret.zeroize()
        self.journal.mark(STEP_CUTOVER_MOUNTED)
        report.steps.append(STEP_CUTOVER_MOUNTED)
        self._log(STEP_CUTOVER_MOUNTED)
        try:
            os.rmdir(long_path(self.staging_mount))
        except OSError:
            pass
        self._mark(report, STEP_COMPLETE)

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
            self.verify_hashes(report, source_files)
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
            report.status = [MIGRATION_VERIFIED, PLAINTEXT_SOURCE_REMAINS]
            report.plaintext_archive = (report.plaintext_archive
                                        or self.journal.data.get("plaintext_archive")
                                        or self.source)
            report.reason = (
                "the encrypted vault is verified and mounted at %s. A readable "
                "plaintext copy still exists at %s -- the vault is NOT fully "
                "protected until you remove or archive it yourself."
                % (self.profile.mount_path, report.plaintext_archive))
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
    last = data.get("last_step")
    source = data.get("source")
    archive = data.get("plaintext_archive")
    final = data.get("final_mount")
    verdict = "NONE"
    if last is None:
        verdict = "NONE"
    elif last == STEP_COMPLETE:
        verdict = "COMPLETE"
    elif last in (STEP_CUTOVER_BEGIN, STEP_CUTOVER_MOUNTED):
        verdict = "INTERRUPTED_DURING_CUTOVER"
    else:
        verdict = "INTERRUPTED_BEFORE_CUTOVER"
    data["verdict"] = verdict
    data["plaintext_present"] = bool(
        (source and os.path.isdir(long_path(source)))
        or (archive and os.path.isdir(long_path(archive))))
    data["final_mount_present"] = bool(final and os.path.isdir(long_path(final)))
    return data


def recover(broker, profile_id, journal_path=None, volume_secret_provider=None):
    """Repair an interrupted migration without ever losing the plaintext.

    Before the cutover there is nothing to repair: the plaintext tree still
    owns its path and the encrypted copy is an unused extra. The run is simply
    resumable.

    Inside the cutover there are exactly two possible states, and the journal
    plus the filesystem distinguish them:

      * the plaintext has been renamed aside and the volume is not yet mounted
        at the real path -- finish by mounting it, or, when no secret is
        available, roll the plaintext name back so the user is never left
        without a vault at the expected path;
      * the volume is mounted at the real path -- the cutover succeeded and
        only the journal was not updated; mark it complete.

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
        journal.mark(STEP_REVERIFY)
        broker.audit.write("migrate_rollback", profile_id=profile_id, result="ok",
                           mount_path=final)
        return {"action": "rolled_back", "verdict": "INTERRUPTED_BEFORE_CUTOVER",
                "detail": inspect(path),
                "message": "the plaintext vault was restored to %s; nothing was "
                           "deleted" % final}
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


def migrate(broker, profile_id, source, verify_application=True, restart=False):
    """Unified entry point for plaintext-to-encrypted vault migration.

    Both CLI and GUI call this function to ensure identical gate enforcement.
    Strictly enforces:
    1. Durable acceptance evaluation (sa_acceptance.evaluate(broker.registry.state_dir, profile)).
       Refuses immediately if invalid with zero authentication, zero mount, and zero mutation.
    2. Reconcile profile.
    3. broker.authenticate_only(profile_id, force_auth=True).
    4. Migrator.run() (staging mount, copy, tree/size/hash verify, app verify, cutover).
    5. Once the broker was touched, success or failure: relock the profile
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

    verdict = sa_acceptance.evaluate(broker.registry.state_dir, profile)
    if not verdict.ok:
        report = MigrationReport(profile_id)
        report.ok = False
        report.error_category = "hardware_acceptance_required"
        reasons = "; ".join(verdict.reasons) if verdict.reasons else "hardware acceptance required"
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

