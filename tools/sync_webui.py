"""Copy the web UI to the repository root, for the `webui` branch.

GitHub Pages serves a branch root, but the Python server needs these files
inside the package, so the `webui` branch carries both: the originals under
``humanizer/webui/`` and a copy at the root that Pages can publish. This script
refreshes the copy.

    git checkout webui
    git merge main
    py tools/sync_webui.py
    git commit -am "sync web ui"

The files use relative paths, so the same bytes work at a root, under a Pages
project subpath, and under the local server. Nothing is rewritten here; it is a
plain copy, and the check below is what keeps it honest.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "humanizer", "webui")
FILES = ("index.html", "app.css", "app.js")


def sync(check_only: bool = False) -> int:
    stale = []
    for name in FILES:
        source = os.path.join(SOURCE, name)
        target = os.path.join(ROOT, name)
        if not os.path.isfile(source):
            sys.stderr.write("missing source: %s\n" % source)
            return 1
        same = os.path.isfile(target) and filecmp.cmp(source, target, shallow=False)
        if same:
            continue
        stale.append(name)
        if not check_only:
            shutil.copy2(source, target)

    if check_only:
        if stale:
            sys.stderr.write(
                "the root copies are out of date: %s\n"
                "run: py tools/sync_webui.py\n" % ", ".join(stale))
            return 1
        print("root copies are up to date")
        return 0

    print("synced %d file(s)%s" % (len(stale), (": " + ", ".join(stale)) if stale else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(sync(check_only="--check" in sys.argv))
