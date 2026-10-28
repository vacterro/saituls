"""Thin compatibility wrapper for the recursive item tree (SAITULS T-165).

The real implementation lives in ``Scripts/item_tools.py`` (mode ``tree``).
This wrapper keeps the historical entry point name alive but no longer
operates on its own directory: the target is explicit, reparse points are
never descended and a cycle is refused by the engine.

    python _ITEM_LIST_RECURSIVE.py <target-dir> [output-file]

Without an output path the listing is written as ``item_tree.txt`` inside the
selected target (excluded from its own listing by the engine).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from item_tools import build, TREE_OUTPUT  # noqa: E402


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: _ITEM_LIST_RECURSIVE.py <target-dir> [output-file]", file=sys.stderr)
        return 1
    target = os.path.abspath(argv[0])
    out = os.path.abspath(argv[1]) if len(argv) > 1 else os.path.join(target, TREE_OUTPUT)
    code, summary = build("tree", target, out)
    print(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
