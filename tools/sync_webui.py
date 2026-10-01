"""Copy the web UI to the repository root, for the `webui` branch.

GitHub Pages serves a branch root, but the local server serves these files from
inside the package, so the `webui` branch carries both: the originals under
``humanizer/webui/`` and a copy at the root that Pages publishes. This refreshes
the copy.

    git checkout webui
    git merge main
    py tools/sync_webui.py
    git commit -am "sync web ui"

Everything uses relative paths, so the same bytes work at a root, under a Pages
project subpath, and under the local server. Nothing is rewritten here; it is a
plain copy, and ``--check`` is what keeps it honest.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "humanizer", "webui")
PAGE_FILES = ("index.html", "app.css", "app.js")
ENGINE = "engine"


def _engine_files():
    directory = os.path.join(SOURCE, ENGINE)
    if not os.path.isdir(directory):
        return []
    return [os.path.join(ENGINE, name)
            for name in sorted(os.listdir(directory)) if name.endswith(".js")]


def _wanted():
    return list(PAGE_FILES) + _engine_files()


def sync(check_only: bool = False) -> int:
    stale = []
    for relative in _wanted():
        source = os.path.join(SOURCE, relative)
        target = os.path.join(ROOT, relative)
        if not os.path.isfile(source):
            sys.stderr.write("missing source: %s\n" % source)
            return 1
        same = os.path.isfile(target) and filecmp.cmp(source, target, shallow=False)
        if same:
            continue
        stale.append(relative)
        if not check_only:
            os.makedirs(os.path.dirname(target) or ROOT, exist_ok=True)
            shutil.copy2(source, target)

    # A module left behind at the root would still be served and could shadow a
    # renamed one, so stale copies are removed rather than ignored.
    orphans = []
    root_engine = os.path.join(ROOT, ENGINE)
    if os.path.isdir(root_engine):
        expected = {os.path.basename(f) for f in _engine_files()}
        for name in sorted(os.listdir(root_engine)):
            if name.endswith(".js") and name not in expected:
                orphans.append(os.path.join(ENGINE, name))
                if not check_only:
                    os.remove(os.path.join(root_engine, name))

    if check_only:
        if stale or orphans:
            sys.stderr.write(
                "the root copies are out of date.\n"
                + ("  stale: %s\n" % ", ".join(stale) if stale else "")
                + ("  left over: %s\n" % ", ".join(orphans) if orphans else "")
                + "run: py tools/sync_webui.py\n")
            return 1
        print("root copies are up to date (%d files)" % len(_wanted()))
        return 0

    changed = len(stale) + len(orphans)
    print("synced %d of %d file(s)%s%s" % (
        len(stale), len(_wanted()),
        (": " + ", ".join(stale)) if stale else "",
        (", removed " + ", ".join(orphans)) if orphans else ""))
    return 0 if changed >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(sync(check_only="--check" in sys.argv))
