"""Static checks on the web UI assets.

There is no build step and no framework, so nothing catches a typo in an element
id until the page loads and silently half-works. These tests do that catching:
every id app.js reaches for has to exist in index.html, every setting checkbox
has to name a real Settings field, and neither asset may reference anything off
this origin.

Run with:  py tests/test_webui.py
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from humanizer.core import Settings
from humanizer.web import BOOLEAN_FIELDS, STATIC_DIR
from test_humanizer import check

HTML = open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8").read()
CSS = open(os.path.join(STATIC_DIR, "app.css"), encoding="utf-8").read()
JS = open(os.path.join(STATIC_DIR, "app.js"), encoding="utf-8").read()

HTML_IDS = set(re.findall(r'\bid="([^"]+)"', HTML))


def test_every_id_the_script_wants_exists():
    wanted = set(re.findall(r"\$\('([^']+)'\)", JS))
    missing = sorted(wanted - HTML_IDS)
    check(not missing, "app.js reaches for ids that are not in the page: %s" % missing)


def test_every_id_in_the_page_is_used():
    """A stray id is usually a rename that only got applied on one side."""
    used = set(re.findall(r"\$\('([^']+)'\)", JS))
    used.update(re.findall(r'\bfor="([^"]+)"', HTML))
    orphans = sorted(HTML_IDS - used)
    check(not orphans, "ids in the page that nothing references: %s" % orphans)


def test_setting_checkboxes_name_real_fields():
    names = set(re.findall(r'data-setting="([^"]+)"', HTML))
    check(names, "no setting checkboxes found")
    unknown = sorted(names - set(BOOLEAN_FIELDS))
    check(not unknown, "checkboxes for fields the API will not accept: %s" % unknown)
    for name in names:
        check(hasattr(Settings(), name), "%s is not a Settings field" % name)


def test_every_boolean_setting_is_exposed():
    names = set(re.findall(r'data-setting="([^"]+)"', HTML))
    missing = sorted(set(BOOLEAN_FIELDS) - names)
    check(not missing, "settings the UI gives no control over: %s" % missing)


def test_checkbox_defaults_match_the_engine():
    """An unchecked box that defaults to True would silently change behaviour."""
    defaults = Settings()
    for match in re.finditer(r'<input type="checkbox" data-setting="([^"]+)"([^>]*)>', HTML):
        name, rest = match.group(1), match.group(2)
        check(("checked" in rest) == getattr(defaults, name),
              "%s is checked=%s in the page but %s in Settings"
              % (name, "checked" in rest, getattr(defaults, name)))


def test_assets_referenced_by_the_page_are_served():
    found = 0
    for reference in re.findall(r'(?:href|src)="([^":]+)"', HTML):
        if reference.startswith(("#", "data:")):
            continue
        found += 1
        check(not reference.startswith("/"),
              "%s is an absolute path; it breaks under a GitHub Pages "
              "project subpath" % reference)
        path = os.path.join(STATIC_DIR, reference)
        check(os.path.isfile(path), "%s is referenced but not present" % reference)
    check(found >= 2, "expected the page to reference its css and js")


def test_api_calls_are_relative():
    """Same reason: '/api/...' would miss under /humanizer/ on Pages."""
    absolute = re.findall(r"fetch\('(/[^']*)'", JS)
    check(not absolute, "absolute API paths: %s" % absolute)
    check(re.search(r"fetch\('api/", JS), "expected relative API calls")


def test_offline_notice_exists():
    """A static copy has no engine; the page has to say so rather than fail."""
    check('id="offline"' in HTML, "no offline notice in the page")
    check("checkEngine" in JS, "nothing probes for the engine")
    check("offline-mode" in CSS, "no styling for the disabled state")


def test_nothing_is_loaded_from_another_origin():
    """The CSP the server sends is 'self' only, so an external subresource
    would simply be blocked. Plain <a href> links are navigation, not a
    subresource, so they are allowed and are not checked here."""
    subresources = [
        (r'src\s*=\s*"([^"]+)"', "script or image"),
        (r'<link\b[^>]*\bhref\s*=\s*"([^"]+)"', "stylesheet"),
        (r"url\(\s*['\"]?([^'\")]+)", "css url"),
        (r"fetch\(\s*['\"]([^'\"]+)", "fetch"),
    ]
    for name, text in (("index.html", HTML), ("app.css", CSS), ("app.js", JS)):
        for pattern, what in subresources:
            for match in re.finditer(pattern, text):
                target = match.group(1)
                check(not re.match(r"(https?:)?//", target),
                      "%s loads a %s from another origin: %s" % (name, what, target))


def test_level_captions_cover_the_whole_slider():
    block = re.search(r"const LEVEL_CAPTIONS = \[(.*?)\];", JS, re.DOTALL)
    check(block, "LEVEL_CAPTIONS not found")
    entries = re.findall(r"'(?:[^'\\]|\\.)*'", block.group(1))
    check(len(entries) == 11, "expected a caption for levels 0-10, got %d" % len(entries))

    slider = re.search(r'<input type="range" id="level"([^>]*)>', HTML)
    check(slider, "level slider not found")
    check('min="0"' in slider.group(1) and 'max="10"' in slider.group(1),
          "level slider does not span 0-10: %s" % slider.group(1))


def _edit_kinds():
    from humanizer import typos as typo_module

    kinds = {"dash", "quote", "semicolon", "phrase", "word", "contraction",
             "hyphen", "opener", "hedge"}
    kinds.update("typo:" + kind for kind, _ in typo_module._WEIGHTS)
    return kinds


def test_stat_labels_cover_every_edit_kind():
    """Any kind the engine can emit needs a label, or the UI shows a raw key."""
    block = re.search(r"const LABELS = \{(.*?)\n\};", JS, re.DOTALL)
    check(block, "LABELS not found in app.js")
    labels = set(re.findall(r"'?([a-z][a-z:_]*)'?\s*:", block.group(1)))
    missing = sorted(k for k in _edit_kinds() if k not in labels)
    check(not missing, "no web UI label for: %s" % missing)


def test_cli_labels_cover_every_edit_kind():
    from humanizer.cli import _LABELS

    missing = sorted(k for k in _edit_kinds() if k not in _LABELS)
    check(not missing, "no CLI label for: %s" % missing)


def _run_all():
    failures = 0
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        try:
            function()
        except Exception as error:                      # noqa: BLE001
            failures += 1
            print("FAIL %s\n     %s: %s" % (name, type(error).__name__, error))
        else:
            print("ok   %s" % name)
    print("\n%s" % ("all passed" if not failures else "%d failed" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
