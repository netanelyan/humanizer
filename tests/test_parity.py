# -*- coding: utf-8 -*-
"""Python and JavaScript must produce the same rewrite.

This is the test that makes two implementations safe to keep. The lexicons are
generated from one source so they cannot drift; everything else is hand-ported,
and this is what proves the port is faithful. It feeds the same texts and seeds
to both engines and compares the output byte for byte, along with the edit
counts and the span offsets the UI highlights from.

Needs node on PATH. Skips with a loud message if it is missing rather than
passing quietly, because a silently skipped parity test is worse than none.

Run with:  py tests/test_parity.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from humanizer.core import Humanizer, Settings, apply_edits_with_spans
from test_humanizer import check

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HARNESS = os.path.join(HERE, "parity_harness.mjs")

ENGLISH = (
    "In today's fast-paced world, it is important to note that organisations must "
    "utilize a wide range of digital tools in order to facilitate collaboration "
    "across distributed teams — the long-term benefits are substantial. "
    "Furthermore, these solutions demonstrate a comprehensive approach that plays "
    "a crucial role in maintaining productivity. One of the most significant "
    "obstacles is the potential for isolation; therefore, it is essential to "
    "foster a culture that prioritizes regular check-ins and well-being."
)

HEBREW = (
    "בעולם המהיר של ימינו, יש לציין כי ארגונים נדרשים לעשות שימוש במגוון רחב "
    "של כלים דיגיטליים על מנת לאפשר שיתוף פעולה בין צוותים מרוחקים — היתרונות "
    "בטווח הארוך הם משמעותיים. יתרה מכך, פתרונות אלה מדגימים גישה מקיפה אשר "
    "ממלאת תפקיד מרכזי בשמירה על הפרודוקטיביות. לסיכום, ניתן לומר כי מדובר "
    "באפשרות טובה עבור ארגונים רבים."
)

MIXED = ENGLISH + "\n\n" + HEBREW + "\n\nSee https://example.com/in-order-to and `utilize()`."

EDGE = (
    "Short. A—B. 5–10 items. “Quoted” and ‘single’ … done. "
    "x-ray, e-mail, long-term. Dr. Smith's e-mail is a.b@c.co.uk — utilize it. "
    "[1] {placeholder} %s Foo.Bar() in order to test. "
    "שלום־עולם, בית-ספר, וכאשר הגיע הזמן. אפשר לומר כי זה חשוב מאוד."
)


def _cases():
    texts = [("english", ENGLISH), ("hebrew", HEBREW),
             ("mixed", MIXED), ("edge", EDGE)]
    for name, text in texts:
        for level in (0, 1, 3, 5, 7, 9, 10):
            for seed in (0, 1, 7, 12345):
                yield {
                    "name": "%s-l%d-s%d" % (name, level, seed),
                    "text": text,
                    "settings": {"level": level, "seed": seed},
                }
    # the switches, so a disabled pass is checked in both engines too
    for flag in ("em_dashes", "hyphens", "contractions", "lexical",
                 "seasoning", "typos", "quotes", "semicolons",
                 "protect_first_sentence"):
        yield {
            "name": "off-" + flag,
            "text": MIXED,
            "settings": {"level": 9, "seed": 3, flag: False},
        }
    # explicit typo rates, including a very high one
    for rate in (0, 2, 25, 120):
        yield {
            "name": "typo-rate-%d" % rate,
            "text": MIXED,
            "settings": {"level": 4, "seed": 9, "typo_rate": rate},
        }
    # a preserve pattern
    yield {
        "name": "preserve",
        "text": ENGLISH,
        "settings": {"level": 10, "seed": 2, "preserve": ["utilize", "crucial role"]},
    }


_SNAKE_TO_CAMEL = {
    "typo_rate": "typoRate",
    "em_dashes": "emDashes",
    "protect_first_sentence": "protectFirstSentence",
}


def _js_settings(payload):
    return {_SNAKE_TO_CAMEL.get(key, key): value for key, value in payload.items()}


def _run_node(cases):
    payload = json.dumps(
        {"cases": [{"text": c["text"], "settings": _js_settings(c["settings"])}
                   for c in cases]},
        ensure_ascii=False)
    process = subprocess.run(
        ["node", HARNESS], input=payload.encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
    if process.returncode != 0:
        raise AssertionError("node harness failed:\n%s"
                             % process.stderr.decode("utf-8", "replace"))
    return json.loads(process.stdout.decode("utf-8"))["results"]


def _run_python(cases):
    results = []
    for case in cases:
        humanizer = Humanizer(Settings(**case["settings"]))
        edits = humanizer.plan(case["text"])
        text, spans = apply_edits_with_spans(case["text"], edits)
        results.append({
            "text": text,
            "stats": dict(humanizer.stats),
            "spans": [{"start": s, "end": e, "kind": k, "old": o}
                      for s, e, k, o in spans],
        })
    return results


def test_engines_agree():
    if not shutil.which("node"):
        raise AssertionError(
            "node is not on PATH, so the Python/JavaScript parity test cannot "
            "run. Install node or the two engines are unverified.")

    cases = list(_cases())
    check(len(cases) >= 100, "only %d parity cases" % len(cases))

    expected = _run_python(cases)
    actual = _run_node(cases)
    check(len(expected) == len(actual),
          "node returned %d results for %d cases" % (len(actual), len(cases)))

    mismatches = []
    for case, want, got in zip(cases, expected, actual):
        if want["text"] != got["text"]:
            mismatches.append("%s: text\n   py: %r\n   js: %r"
                              % (case["name"], want["text"], got["text"]))
        elif want["stats"] != got["stats"]:
            mismatches.append("%s: stats\n   py: %s\n   js: %s"
                              % (case["name"], want["stats"], got["stats"]))
        elif want["spans"] != got["spans"]:
            mismatches.append("%s: spans differ" % case["name"])

    if mismatches:
        raise AssertionError("%d of %d cases diverged:\n\n%s"
                             % (len(mismatches), len(cases),
                                "\n\n".join(mismatches[:5])))
    print("     %d cases, both engines identical" % len(cases))


def test_docx_engines_agree(tmp_path):
    """The .docx written in the browser must match the one written by Python.

    Same seed, same document, so the text has to come out identical, and the
    archive the browser builds by hand has to be one Python's zipfile can open
    with the structure intact.
    """
    if not shutil.which("node"):
        raise AssertionError("node is not on PATH; the docx parity test cannot run")

    import zipfile
    from humanizer.docxio import extract_docx_text, humanize_docx
    from test_humanizer import HEADER_XML, RICH_DOCUMENT

    source = os.path.join(tmp_path, "in.docx")
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships/>')
        archive.writestr("word/document.xml", RICH_DOCUMENT)
        archive.writestr("word/header1.xml", HEADER_XML)
        archive.writestr("word/settings.xml", "<settings/>")
        archive.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\n" + bytes(range(64)))

    for level, seed in ((6, 4), (9, 11), (3, 0)):
        py_out = os.path.join(tmp_path, "py-%d-%d.docx" % (level, seed))
        js_out = os.path.join(tmp_path, "js-%d-%d.docx" % (level, seed))

        report = humanize_docx(source, py_out,
                               Humanizer(Settings(level=level, seed=seed)))

        process = subprocess.run(
            ["node", os.path.join(HERE, "parity_docx.mjs"), source, js_out,
             json.dumps({"level": level, "seed": seed})],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
        if process.returncode != 0:
            raise AssertionError("node docx harness failed:\n%s"
                                 % process.stderr.decode("utf-8", "replace"))
        js_report = json.loads(process.stdout.decode("utf-8"))

        label = "level %d seed %d" % (level, seed)
        check(report.rewritten == js_report["rewritten"],
              "%s: rewrote %d paragraphs in Python, %d in JS"
              % (label, report.rewritten, js_report["rewritten"]))
        check(report.stats == js_report["stats"],
              "%s: stats differ\n   py: %s\n   js: %s"
              % (label, report.stats, js_report["stats"]))

        # the archive the browser built has to be readable, and identical in text
        with zipfile.ZipFile(js_out) as archive:
            check(archive.testzip() is None, "%s: JS archive is corrupt" % label)
            names = archive.namelist()
            with zipfile.ZipFile(source) as original:
                check(names == original.namelist(),
                      "%s: entry list changed: %s" % (label, names))
                for name in names:
                    if name in ("word/document.xml", "word/header1.xml"):
                        continue
                    check(archive.read(name) == original.read(name),
                          "%s: %s was altered" % (label, name))
            xml = archive.read("word/document.xml").decode("utf-8")

        check(extract_docx_text(py_out) == extract_docx_text(js_out),
              "%s: document text differs\n   py: %r\n   js: %r"
              % (label, extract_docx_text(py_out)[:300],
                 extract_docx_text(js_out)[:300]))

        for marker in ('<w:pStyle w:val="Heading1"/>', "<w:tbl>", "<w:tab/>",
                       "<w:br/>", "<w:sectPr>", "<w:delText>"):
            check(marker in xml, "%s: %s lost by the JS writer" % (label, marker))
        check("REF _Ref123 \\h utilize in order to " in xml,
              "%s: a field code was rewritten" % label)


def test_lexicon_js_is_current():
    """The generated file must match what the Python tables would produce."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import build_lexicon

    check(build_lexicon.main(["--check"]) == 0,
          "engine/lexicon.js is stale; run py tools/build_lexicon.py")


def test_engine_has_no_external_imports():
    """The page is served from one origin with a strict CSP, and is meant to work
    offline, so nothing in the engine may reach for a package or a URL."""
    import re

    directory = os.path.join(ROOT, "humanizer", "webui", "engine")
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".js"):
            continue
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            source = handle.read()
        for match in re.finditer(r"""from\s+['"]([^'"]+)['"]""", source):
            target = match.group(1)
            check(target.startswith("./") or target.startswith("../"),
                  "%s imports %r, which is not a relative file" % (name, target))


def _run_all():
    import tempfile
    failures = 0
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        try:
            if "tmp_path" in function.__code__.co_varnames[:function.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as directory:
                    function(directory)
            else:
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
