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


def test_the_page_makes_no_network_calls():
    """The engine runs in the page. A fetch would mean it had crept back to a
    server, and the deployed copy has none."""
    for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource",
                      "navigator.sendBeacon", "import("):
        check(forbidden not in JS, "app.js uses %s" % forbidden)


def test_the_page_loads_the_engine_as_a_module():
    check('type="module"' in HTML, "app.js must be a module to import the engine")
    check(re.search(r"from\s+'\./engine/index\.js'", JS),
          "app.js does not import the engine")


def test_engine_files_are_present_and_relative():
    engine = os.path.join(STATIC_DIR, "engine")
    expected = {"index.js", "core.js", "edit.js", "typos.js",
                "lexicon.js", "rng.js", "docx.js"}
    present = {n for n in os.listdir(engine) if n.endswith(".js")}
    check(expected <= present, "missing engine modules: %s" % sorted(expected - present))

    for name in sorted(present):
        with open(os.path.join(engine, name), encoding="utf-8") as handle:
            source = handle.read()
        for match in re.finditer(r"""from\s+['"]([^'"]+)['"]""", source):
            target = match.group(1)
            check(target.startswith("./") or target.startswith("../"),
                  "engine/%s imports %r, which is not a relative file" % (name, target))
            resolved = os.path.join(engine, target)
            check(os.path.isfile(resolved),
                  "engine/%s imports %r, which does not exist" % (name, target))


def test_the_hidden_attribute_actually_hides():
    """The UA rule [hidden]{display:none} loses to any author rule that sets
    display, whatever its specificity. That is how the drop overlay ended up
    permanently on screen covering the page. Either the reset is present, or no
    rule may set display on something the markup toggles with hidden."""
    reset = re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", CSS)
    if reset:
        return

    toggled = set()
    for tag in re.findall(r"<[^>]*\bhidden\b[^>]*>", HTML):
        toggled.update(re.findall(r'\bid="([^"]+)"', tag))
        for value in re.findall(r'\bclass="([^"]+)"', tag):
            toggled.update(value.split())

    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", CSS):
        selector, body = match.group(1).strip(), match.group(2)
        if not re.search(r"(^|[;\s])display\s*:", body):
            continue
        for name in re.findall(r"[.#]([A-Za-z0-9_-]+)", selector):
            check(name not in toggled,
                  "%r sets display on %r, which the markup hides with the hidden "
                  "attribute. Add [hidden]{display:none!important} or drop the "
                  "display declaration." % (selector, name))


def test_the_drop_overlay_starts_hidden():
    match = re.search(r'<div id="drop"[^>]*>', HTML)
    check(match, "the drop overlay is gone")
    check("hidden" in match.group(0), "the drop overlay is visible on page load")


def test_drag_tracking_cannot_desync():
    """It was a dragenter/dragleave counter, which drifted and stranded the
    overlay. Whatever replaces it must have a way out."""
    check("dragover" in JS, "nothing listens for dragover")
    check("Escape" in JS, "no key dismisses the drop overlay")
    check("hideDrop" in JS, "no single place hides the drop overlay")


def test_generated_lexicon_is_not_hand_edited():
    path = os.path.join(STATIC_DIR, "engine", "lexicon.js")
    with open(path, encoding="utf-8") as handle:
        head = handle.read(200)
    check("GENERATED FILE" in head, "lexicon.js lost its generated-file notice")


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
