"""Shell argument normalization for the Explorer context-menu workers.

Explorer substitutes `"%1"` literally, so a drive root arrives as `"G:\"` --
the trailing backslash escapes the closing quote and the worker's argv[1] is
`G:"`. Stripping the quote leaves `G:`, which is *drive-relative*, not the
drive root: `os.path.abspath('G:')` returns whatever this process's saved
current directory for G: happens to be. For workers that delete without the
Recycle Bin, silently scanning the wrong directory is the failure that matters.

Every worker takes its target through `shell_target` so the rule lives in one
place.
"""

import os
import re

_BARE_DRIVE = re.compile(r'^[A-Za-z]:$')


def shell_target(raw):
    """The absolute directory a `"%1"` / `"%V"` argument really means."""
    target = raw.strip('"').strip()
    if _BARE_DRIVE.match(target):
        # A drive letter with no separator is relative to that drive's current
        # directory. Explorer meant the root.
        target += os.sep
    return target


if __name__ == '__main__':
    # Self-check: `python Scripts\shell_arg.py` -> 0 on success, 1 on failure.
    import sys

    cases = [
        ('G:"', 'G:\\'),            # drive root as Explorer really passes it
        ('G:\\', 'G:\\'),           # already a root
        ('G:', 'G:\\'),             # bare drive, however it arrived
        ('"V:\\Example"', 'V:\\Example'),
        ('V:\\Example', 'V:\\Example'),
        ('V:\\a b\\c', 'V:\\a b\\c'),
        ('\\\\server\\share', '\\\\server\\share'),
    ]
    bad = [(raw, want, shell_target(raw)) for raw, want in cases
           if shell_target(raw) != want]
    for raw, want, got in bad:
        print('FAIL  %r -> %r, expected %r' % (raw, got, want))
    if bad:
        print('FAILED (%d failure(s))' % len(bad))
        sys.exit(1)
    print('PASS (0 failures)')
