# -*- coding: utf-8 -*-
"""Hebrew engine tests. Run with:  py tests/test_hebrew.py

The two things that go wrong in Hebrew and have no English equivalent are
prefix letters that must close up against the next word, and gender or number
agreement between a noun and the words around it. Most of what is here guards
one of those.
"""

from __future__ import annotations

import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from humanizer import lexicon, lexicon_he
from humanizer.core import Humanizer, Settings, _ends_with_clitic
from humanizer.docxio import extract_docx_text, humanize_docx, xml_escape
from humanizer.typos import HEBREW_KINDS, TypoEngine, is_hebrew
from test_humanizer import check

HEBREW = (
    "בעולם המהיר של ימינו, יש לציין כי ארגונים נדרשים לעשות שימוש במגוון רחב "
    "של כלים דיגיטליים על מנת לאפשר שיתוף פעולה בין צוותים מרוחקים — היתרונות "
    "בטווח הארוך הם משמעותיים. יתרה מכך, פתרונות אלה מדגימים גישה מקיפה אשר "
    "ממלאת תפקיד מרכזי בשמירה על הפרודוקטיביות. לסיכום, ניתן לומר כי מדובר "
    "באפשרות טובה עבור ארגונים רבים."
)

ENGLISH = ("It is important to note that we must utilize this — daily. "
           "Furthermore, the long-term benefits are substantial.")


def rewrite(text, **kwargs):
    kwargs.setdefault("seed", 5)
    return Humanizer(Settings(**kwargs)).run(text)


# -- script detection -------------------------------------------------------

def test_is_hebrew():
    check(is_hebrew("שלום"), "Hebrew not detected")
    check(is_hebrew("hello שלום"), "mixed text not detected")
    check(not is_hebrew("hello"), "English wrongly detected as Hebrew")
    check(not is_hebrew("12.5 %"), "digits wrongly detected as Hebrew")


# -- prefixes ---------------------------------------------------------------

def test_clitic_detection():
    for text in ("אפשר לומר ש", "ש", "כדי ל", "חוץ מ", "כש", "רק ש"):
        check(_ends_with_clitic(text), "%r should end in a prefix" % text)
    for text in ("וגם", "אבל", "של", "כל", "מה", "לו", "בסוף", "חוץ מזה"):
        check(not _ends_with_clitic(text), "%r is a word, not a prefix" % text)


def test_prefixes_close_up_against_the_next_word():
    """‏אשר‎ -> ‏ש‎ has to give ‏שעובדים‎, never ‏ש עובדים‎."""
    text = "מחקרים מראים כי עובדים אשר עובדים מרחוק מדווחים על שביעות רצון גבוהה."
    for seed in range(30):
        out = rewrite(text, level=9, seed=seed, typos=False)
        for prefix in "ושבלכמה":
            check(" %s " % prefix not in out,
                  "stranded prefix %r at seed %d: %s" % (prefix, seed, out))


def test_no_stranded_prefix_anywhere():
    for seed in range(25):
        out = rewrite(HEBREW, level=10, seed=seed)
        stranded = re.findall(r"(?:^|\s)([ושבלכמה])\s", out)
        check(not stranded, "stranded prefixes %s at seed %d" % (stranded, seed))


# -- lexicon hygiene --------------------------------------------------------

def test_no_identity_replacements_survive():
    """An option equal to its key is a no-op that would burn a probability roll."""
    humanizer = Humanizer(Settings(level=10))
    for key, (options, kind) in humanizer._table.items():
        check(key not in options, "%r still lists itself as a replacement" % key)
        check(options, "%r has no replacements left" % key)


def test_hebrew_and_english_keys_never_collide():
    hebrew = set(lexicon_he.phrases_for_level(10)) | set(lexicon_he.words_for_level(10))
    english = set(lexicon.phrases_for_level(10)) | set(lexicon.words_for_level(10))
    check(not (hebrew & english), "shared keys: %s" % (hebrew & english))
    for key in hebrew:
        check(is_hebrew(key), "%r is in the Hebrew tables but has no Hebrew" % key)


def test_gendered_pairs_are_not_mixed():
    """A spot check on the pairs that agreement actually depends on."""
    masculine_plural = {"אתגרים": "קשיים", "גורמים": "דברים", "היבטים": "צדדים",
                        "מרכיבים": "חלקים", "פרטים": "אנשים"}
    for key, expected in masculine_plural.items():
        options = lexicon_he.WORDS_CASUAL.get(key)
        check(options and expected in options, "%s -> %s missing" % (key, expected))
        for option in options:
            check(not option.endswith("ות"),
                  "%s is masculine plural but %r is feminine" % (key, option))

    # the entries that cannot be done safely must stay out
    for absent in ("מהווה", "מהווים", "מדובר", "פתרונות", "קשיים"):
        for table in (lexicon_he.WORDS_SAFE, lexicon_he.WORDS_CASUAL,
                      lexicon_he.WORDS_LOOSE):
            check(absent not in table,
                  "%r needs agreement the table cannot provide" % absent)


# -- rewriting --------------------------------------------------------------

def test_hebrew_gets_rewritten():
    out = rewrite(HEBREW, level=8, typos=False)
    check(out != HEBREW, "nothing changed")
    # Any single marker is a coin flip, so the assertion is on the aggregate.
    changed = sum(1 for tell in ("על מנת", "יש לציין כי", "יתרה מכך", "לסיכום",
                                 "ניתן לומר כי", "בטווח הארוך", "מגוון רחב של")
                  if tell not in out)
    check(changed >= 4, "only %d of 7 formal markers were touched:\n%s" % (changed, out))


def test_em_dashes_go_in_hebrew_too():
    for seed in range(25):
        out = rewrite(HEBREW, level=5, seed=seed, typos=False)
        check("—" not in out and "–" not in out, "em dash survived at seed %d" % seed)


def test_level_zero_leaves_hebrew_alone():
    check(rewrite(HEBREW, level=0, typos=False) == HEBREW)


def test_mixed_document_keeps_each_language_to_its_own_rules():
    text = HEBREW + "\n\n" + ENGLISH
    for seed in range(15):
        out = rewrite(text, level=9, seed=seed)
        # no Hebrew letter may appear inside the English half and vice versa
        head, _, tail = out.partition("\n\n")
        check(not re.search(r"[A-Za-z]{3,}", head),
              "English leaked into the Hebrew paragraph at seed %d" % seed)
        check(not is_hebrew(tail),
              "Hebrew leaked into the English paragraph at seed %d" % seed)


def test_hebrew_output_has_no_double_punctuation():
    for seed in range(25):
        out = rewrite(HEBREW, level=8, seed=seed, typos=False)
        check(",," not in out and ", ," not in out, "doubled comma at seed %d" % seed)
        check(" ," not in out, "floating comma at seed %d: %r" % (seed, out))
        check("  " not in out, "double space with typos off at seed %d" % seed)


def test_hebrew_length_stays_sane():
    for seed in range(20):
        out = rewrite(HEBREW, level=9, seed=seed)
        ratio = len(out.split()) / len(HEBREW.split())
        check(0.70 < ratio < 1.30, "length drifted at seed %d (%.2f)" % (seed, ratio))


# -- typos ------------------------------------------------------------------

def test_hebrew_typo_kinds_fire():
    seen = set()
    for seed in range(80):
        humanizer = Humanizer(Settings(level=1, typo_rate=70, seed=seed,
                                       lexical=False, em_dashes=False,
                                       hyphens=False, contractions=False,
                                       quotes=False, semicolons=False,
                                       seasoning=False))
        humanizer.plan(HEBREW * 2)
        seen.update(humanizer.stats)
    for kind in HEBREW_KINDS:
        check("typo:" + kind in seen, "%s never fired; saw %s" % (kind, sorted(seen)))


def test_english_typos_never_touch_hebrew_words():
    """No lowercase-start or apostrophe slips on a Hebrew word."""
    for seed in range(40):
        humanizer = Humanizer(Settings(level=0, typo_rate=90, seed=seed))
        edits = humanizer.plan(HEBREW * 2)
        for edit in edits:
            if is_hebrew(edit.old):
                kind = edit.kind.replace("typo:", "")
                check(kind not in ("lowercase_start", "shift_held",
                                   "lost_apostrophe", "homophone", "key_slip"),
                      "English typo %r applied to Hebrew %r" % (kind, edit.old))


def test_hebrew_typos_never_touch_english_words():
    for seed in range(40):
        humanizer = Humanizer(Settings(level=0, typo_rate=90, seed=seed))
        for edit in humanizer.plan(ENGLISH * 3):
            kind = edit.kind.replace("typo:", "")
            check(kind not in HEBREW_KINDS,
                  "Hebrew typo %r applied to %r" % (kind, edit.old))


def test_final_form_typo_is_the_right_shape():
    engine = TypoEngine(__import__("random").Random(1))
    check(engine._mutate("he_final_form", "שלום") == "שלומ")
    check(engine._mutate("he_final_form", "מרוחקים") == "מרוחקימ")
    check(engine._mutate("he_alef_he", "לקרוא") == "לקרוה")
    check(engine._mutate("he_alef_he", "הרבה") == "הרבא")
    check(engine._mutate("he_prefix_split", "וכאשר") == "ו כאשר")


def test_ktiv_typo_drops_only_vav_or_yod():
    engine = TypoEngine(__import__("random").Random(3))
    for _ in range(40):
        out = engine._mutate("he_ktiv", "דיגיטליים")
        if out is None:
            continue
        check(len(out) == len("דיגיטליים") - 1, out)
        missing = [c for c in "דיגיטליים" if c not in out or
                   "דיגיטליים".count(c) != out.count(c)]
        check(all(c in "וי" for c in missing), "dropped %s, not a vav or yod" % missing)


# -- docx -------------------------------------------------------------------

def test_hebrew_docx_keeps_rtl_settings(tmp_path):
    """Right-to-left markup lives in the paragraph properties, which we never
    touch, so it has to survive untouched."""
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:pPr><w:bidi/><w:jc w:val="right"/></w:pPr>'
        '<w:r><w:rPr><w:rtl/></w:rPr><w:t xml:space="preserve">%s</w:t></w:r></w:p>'
        '<w:sectPr><w:bidi/></w:sectPr></w:body></w:document>' % xml_escape(HEBREW)
    )
    path = os.path.join(tmp_path, "he.docx")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("word/document.xml", document)

    out = os.path.join(tmp_path, "out.docx")
    report = humanize_docx(path, out, Humanizer(Settings(level=8, seed=2)))
    check(report.rewritten == 1, "the Hebrew paragraph was not rewritten")

    with zipfile.ZipFile(out) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    for marker in ("<w:bidi/>", '<w:jc w:val="right"/>', "<w:rtl/>"):
        check(marker in xml, "%s was lost" % marker)
    check("—" not in xml, "em dash survived")
    check(is_hebrew(extract_docx_text(out)), "the Hebrew text is gone")


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
