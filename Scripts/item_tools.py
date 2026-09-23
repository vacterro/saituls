#!/usr/bin/env python3
"""Deterministic directory item listing for SAITULS (T-165).

One first-party implementation owns both modes:

    flat  - directories first, then files, case-stable sorted
    tree  - recursive hierarchy with an explicit reparse-point guard

Contract:

  * the target directory is ALWAYS explicit; nothing silently operates on
    this file's own directory
  * output is published atomically (temp sibling -> flush/fsync -> replace),
    so a failed run leaves the previous valid output byte-for-byte untouched
  * PermissionError, a vanished path and an unreadable child are reported,
    never swallowed
  * Windows reparse points (junctions / symlinks) are rendered but never
    descended, and entering a directory twice is refused - no traversal loop
  * an output file located inside the target is excluded from its own listing

Exit states:

    0  OK
    1  invalid target (missing, not a directory, unreadable root)
    2  completed, but one or more children could not be read (reported)
    3  output write failure (previous output preserved)

The two legacy prototypes (``_ITEM_LIST.py`` / ``_ITEM_LIST_RECURSIVE.py``)
are thin wrappers over this module.
"""

import argparse
import os
import stat
import sys
import tempfile

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

FLAT_OUTPUT = "item_list.txt"
TREE_OUTPUT = "item_tree.txt"
DEFAULT_MAX_DEPTH = 64
TEMP_PREFIX = ".item_tools-"


def _sort_key(name):
    # Deterministic and locale-independent: casefold first, raw name as the
    # tie-breaker so "A" and "a" never swap between runs.
    return (name.casefold(), name)


def _norm(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _is_reparse(st):
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _classify(entry):
    """Return one of 'dir', 'file', 'link', 'other' for a scandir entry."""
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return "other"
    if _is_reparse(st):
        return "link"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "other"


def _scan(directory, excluded):
    """Return (dirs, files, links, others, errors) for one directory.

    ``excluded`` is a set of normalised absolute paths skipped from the
    listing (the output file itself, plus any other caller-supplied path).
    """
    dirs, files, links, others, errors = [], [], [], [], []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                full = os.path.join(directory, entry.name)
                if _norm(full) in excluded:
                    continue
                kind = _classify(entry)
                if kind == "dir":
                    dirs.append(entry.name)
                elif kind == "file":
                    files.append(entry.name)
                elif kind == "link":
                    links.append(entry.name)
                else:
                    others.append(entry.name)
    except PermissionError as exc:
        errors.append((directory, "PermissionError: %s" % (exc.strerror or exc)))
    except FileNotFoundError:
        errors.append((directory, "the directory disappeared during the scan"))
    except OSError as exc:
        errors.append((directory, "%s: %s" % (type(exc).__name__, exc)))
    return (sorted(dirs, key=_sort_key), sorted(files, key=_sort_key),
            sorted(links, key=_sort_key), sorted(others, key=_sort_key), errors)


def render_flat(target, excluded):
    """Flat listing: directories, then files; links/others reported inline."""
    errors = []
    dirs, files, links, others, errs = _scan(target, excluded)
    errors += errs
    lines = []
    for name in dirs:
        lines.append("[DIR] %s" % name)
    for name in files:
        lines.append(name)
    for name in links:
        lines.append("[LINK] %s" % name)
    for name in others:
        lines.append("[OTHER] %s" % name)
    return "\n".join(lines), errors


def render_tree(target, excluded, max_depth=DEFAULT_MAX_DEPTH):
    """Recursive hierarchy. A reparse point is shown, never entered."""
    errors = []
    visited = set()
    root_name = os.path.basename(os.path.abspath(target)) or os.path.abspath(target)
    lines = ["[%s]" % root_name]

    INDENT, BRANCH, LAST, PIPE = "    ", "|-- ", "`-- ", "|   "

    def walk(directory, prefix, depth):
        if depth > max_depth:
            errors.append((directory, "maximum depth %d reached; the subtree was not descended" % max_depth))
            lines.append("%s[ERROR] maximum depth reached" % prefix)
            return
        try:
            st = os.stat(directory)
        except OSError as exc:
            errors.append((directory, "could not stat: %s" % exc))
            lines.append("%s[ERROR] unreadable" % prefix)
            return
        key = (st.st_dev, st.st_ino)
        if key in visited:
            errors.append((directory, "already visited (link cycle); not descended again"))
            lines.append("%s[ERROR] cycle" % prefix)
            return
        visited.add(key)

        dirs, files, links, others, errs = _scan(directory, excluded)
        for err_dir, msg in errs:
            errors.append((err_dir, msg))
            lines.append("%s[ERROR] %s" % (prefix, msg))

        entries = ([(n, "dir") for n in dirs] + [(n, "file") for n in files] +
                   [(n, "link") for n in links] + [(n, "other") for n in others])
        for index, (name, kind) in enumerate(entries):
            is_last = index == len(entries) - 1
            connector = LAST if is_last else BRANCH
            child_prefix = prefix + (INDENT if is_last else PIPE)
            if kind == "dir":
                lines.append("%s%s[%s]" % (prefix, connector, name))
                walk(os.path.join(directory, name), child_prefix, depth + 1)
            elif kind == "link":
                lines.append("%s%s[LINK] %s" % (prefix, connector, name))
            elif kind == "other":
                lines.append("%s%s[OTHER] %s" % (prefix, connector, name))
            else:
                lines.append("%s%s%s" % (prefix, connector, name))

    walk(os.path.abspath(target), "", 0)
    return "\n".join(lines), errors


def publish_atomic(out_path, text):
    """Write ``text`` to a temp sibling, then replace ``out_path`` in one step."""
    out_path = os.path.abspath(out_path)
    out_dir = os.path.dirname(out_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            if text and not text.endswith("\n"):
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out_path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def build(mode, target, out_path, max_depth=DEFAULT_MAX_DEPTH):
    """Render the requested listing and publish it atomically.

    Returns (exit_code, summary_line). On a failed run the previously valid
    output is left untouched.
    """
    target = os.path.abspath(target)
    if not os.path.isdir(target):
        return 1, "INVALID_TARGET: not a directory: %s" % target

    excluded = {_norm(out_path)}
    try:
        if mode == "tree":
            body, errors = render_tree(target, excluded, max_depth)
        else:
            body, errors = render_flat(target, excluded)
    except PermissionError as exc:
        return 1, "INVALID_TARGET: PermissionError: %s" % exc

    if errors:
        report = ["", "[ERRORS] %d item(s) could not be listed:" % len(errors)]
        for path, msg in errors:
            report.append("[ERROR] %s: %s" % (path, msg))
        body = body + "\n".join(report)

    try:
        publish_atomic(out_path, body)
    except Exception as exc:
        return 3, "WRITE_FAILURE: %s: %s" % (type(exc).__name__, exc)

    if errors:
        return 2, "PARTIAL: %s (%d error(s) reported in the output)" % (out_path, len(errors))
    return 0, "OK: %s" % out_path


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="item_tools",
        description="Deterministic flat or recursive directory listing (SAITULS T-165).")
    parser.add_argument("--mode", choices=("flat", "tree"), required=True)
    parser.add_argument("--target", required=True, help="the directory to list")
    parser.add_argument("--out", required=True, help="the output file path")
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    code, summary = build(args.mode, args.target, args.out, args.max_depth)
    print(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
