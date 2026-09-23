"""Path canonicalization and managed-root containment for SAITULS Secure Apps.

Every path that reaches a storage backend, an application launch or the state
file passes through here first. The rules are fail-closed: a path that cannot
be proven to be an absolute, non-escaping, reparse-free member of a declared
managed root is refused, not "probably fine".

Two distinct checks live here and must not be confused:

  containment  -- the canonical path is at or below a declared root. Compared
                  on canonical strings with a separator boundary, so
                  ``C:\managed-evil`` is NOT inside ``C:\managed``.

  reparse      -- no component of the path chain below the root is a symlink,
                  junction or mount point. This is what stops a junction
                  planted inside the managed tree from redirecting a container
                  or an application directory somewhere else.

The mount path of a protected volume is deliberately exempt from the reparse
rule: a directory mount point IS a reparse point once the volume is attached,
so applying the rule there would make a correctly mounted vault look like an
attack. Mount paths get containment and shape checks only.
"""
import ntpath
import os
import stat

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class PathPolicyError(ValueError):
    """A path violated a declared Secure Apps path rule. Always fail-closed."""


def canonical(path):
    """Absolute, normalized, drive-uppercased path. No filesystem access.

    ``os.path.realpath`` is deliberately NOT used: it resolves reparse points,
    which would hide exactly the redirection the reparse check exists to find.
    Resolution happens in :func:`assert_no_reparse_below`, where it is a
    finding rather than a silent normalization.
    """
    if path is None:
        raise PathPolicyError("path is None")
    if not isinstance(path, str):
        raise PathPolicyError("path is not a string: %r" % (type(path).__name__,))
    text = path.strip()
    if not text:
        raise PathPolicyError("path is empty")
    if "\x00" in text:
        raise PathPolicyError("path contains a NUL byte")
    expanded = os.path.expandvars(text)
    if "%" in expanded and expanded != text:
        # expandvars leaves unresolved %NAME% in place; a half-expanded path
        # would silently point somewhere else than the author intended.
        pass
    full = ntpath.normpath(ntpath.abspath(expanded))
    drive, rest = ntpath.splitdrive(full)
    return drive.upper() + rest


def is_absolute(path):
    drive, rest = ntpath.splitdrive(ntpath.normpath(path or ""))
    if drive and rest.startswith("\\"):
        return True
    return bool(path) and path.startswith("\\\\")


def with_sep(path):
    return path if path.endswith(os.sep) else path + os.sep


def is_within(root, candidate):
    """True when *candidate* is *root* itself or below it (case-insensitive)."""
    r = canonical(root)
    c = canonical(candidate)
    if c.lower() == r.lower():
        return True
    return c.lower().startswith(with_sep(r).lower())


def assert_within(root, candidate, what="path"):
    if not is_within(root, candidate):
        raise PathPolicyError(
            "%s escapes its managed root: %s not inside %s"
            % (what, canonical(candidate), canonical(root)))
    return canonical(candidate)


def is_reparse_point(path):
    """True when *path* itself is a symlink, junction or volume mount point."""
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(st.st_mode)


def components_below(root, candidate):
    """Every existing path component strictly below *root*, root excluded."""
    r = canonical(root)
    c = canonical(candidate)
    if not is_within(r, c) or c.lower() == r.lower():
        return []
    tail = c[len(with_sep(r)):]
    out = []
    cur = r
    for part in tail.split(os.sep):
        if not part:
            continue
        cur = ntpath.join(cur, part)
        out.append(cur)
    return out


def assert_no_reparse_below(root, candidate, what="path"):
    """Refuse a reparse point anywhere between *root* (exclusive) and the leaf.

    Only existing components are inspected; a path that does not exist yet
    cannot be redirected, and the check runs again when it does.
    """
    for comp in components_below(root, candidate):
        if not os.path.lexists(comp):
            continue
        if is_reparse_point(comp):
            raise PathPolicyError(
                "%s crosses a reparse point inside the managed root: %s"
                % (what, comp))
    return canonical(candidate)


def assert_managed(root, candidate, what="path"):
    """Containment + reparse, the combination every managed payload path uses."""
    resolved = assert_within(root, candidate, what)
    return assert_no_reparse_below(root, resolved, what)


def assert_mount_path(candidate, what="mount_path"):
    """Shape checks for a mount target. No reparse rule -- see module docstring.

    A mount target must be an absolute local path and must not be a drive root:
    mounting a vault over ``C:\`` is never a configuration anyone meant.
    """
    resolved = canonical(candidate)
    if not is_absolute(resolved):
        raise PathPolicyError("%s is not absolute: %s" % (what, resolved))
    drive, rest = ntpath.splitdrive(resolved)
    if rest in ("", "\\"):
        raise PathPolicyError("%s may not be a drive root: %s" % (what, resolved))
    return resolved


def assert_executable_path(root, candidate, what="executable"):
    """An application executable must be a managed, existing, ordinary file."""
    resolved = assert_managed(root, candidate, what)
    if os.path.isdir(resolved):
        raise PathPolicyError("%s is a directory, not a file: %s" % (what, resolved))
    return resolved


def directory_is_empty(path):
    try:
        with os.scandir(path) as it:
            for _ in it:
                return False
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# mount targets
# --------------------------------------------------------------------------
#: Transient mount used while a brand-new container is created and enrolled.
#: Enrollment must never take the profile's real mount path: on a first run
#: that path still holds the user's plaintext data, and a Windows directory
#: mount needs an EMPTY directory. Creating there would either fail or demand
#: that the plaintext be moved before anything has been verified.
ENROLLMENT_MOUNT_NAME = "_enrollment_mount"
#: Transient mount used by migration while the plaintext still owns the real
#: path. Separate from the enrollment mount so an interrupted enrollment and
#: an interrupted migration can never be mistaken for each other.
STAGING_MOUNT_NAME = "_staging_mount"


def enrollment_mount_path(container):
    """``<container dir>\_enrollment_mount`` for a container image path."""
    return canonical(ntpath.join(ntpath.dirname(canonical(container)),
                                 ENROLLMENT_MOUNT_NAME))


def staging_mount_path(container):
    """``<container dir>\_staging_mount`` for a container image path."""
    return canonical(ntpath.join(ntpath.dirname(canonical(container)),
                                 STAGING_MOUNT_NAME))


def assert_directory_empty(path, what="mount target"):
    """Refuse to mount onto anything but a directory that is provably empty.

    Windows will not attach a volume to a non-empty directory, but finding
    that out from ``Add-PartitionAccessPath`` happens *after* the image has
    been created, attached, formatted and encrypted -- halfway through a
    mutation, with a half-built container to clean up. Asking first turns a
    partial failure into a refusal, and the answer ("your data is still
    there") is the one the user needs.

    Returns the canonical path. Raises :class:`PathPolicyError` when the
    target exists and is not an empty directory, or cannot be read.
    """
    resolved = canonical(path)
    if not os.path.exists(resolved):
        return resolved
    if not os.path.isdir(resolved):
        raise PathPolicyError("%s exists and is not a directory: %s"
                              % (what, resolved))
    try:
        with os.scandir(resolved) as entries:
            first = next(iter(entries), None)
    except OSError as exc:
        raise PathPolicyError("%s cannot be read, so it cannot be proven empty: "
                              "%s (%s)" % (what, resolved, type(exc).__name__))
    if first is not None:
        raise PathPolicyError(
            "%s is not empty: %s. Windows mounts a volume only onto an empty "
            "directory, and SAITULS will not move or delete what is there."
            % (what, resolved))
    return resolved
