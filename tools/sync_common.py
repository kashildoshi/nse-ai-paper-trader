#!/usr/bin/env python3
"""Copy shared/nse_common.py into every .agent/skills/trader-*/ folder (byte for byte).

    py tools/sync_common.py          # copy
    py tools/sync_common.py --check  # exit 1 if any copy differs from the master
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, "shared", "nse_common.py")


def main():
    check = "--check" in sys.argv
    with open(MASTER, "rb") as f:
        src = f.read()
    bad = 0
    for d in sorted(glob.glob(os.path.join(ROOT, ".agent", "skills", "trader-*"))):
        dst = os.path.join(d, "nse_common.py")
        cur = open(dst, "rb").read() if os.path.exists(dst) else None
        if cur == src:
            continue
        if check:
            print("out of date:", os.path.relpath(dst, ROOT))
            bad += 1
        else:
            with open(dst, "wb") as f:
                f.write(src)
            print("synced:", os.path.relpath(dst, ROOT))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
