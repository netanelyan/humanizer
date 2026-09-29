"""Tests. Run with:  py -m pytest -q   (or  py tests/test_humanizer.py)"""

from __future__ import annotations

import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from humanizer.core import Edit, EditBuffer, Humanizer, Settings, apply_edits
from humanizer.docxio import (extract_docx_text, humanize_docx, humanize_part,
                              xml_escape, xml_unescape)

SAMPLE = (
    "In today's fast-paced world, it is important to note that organisations must "
    "utilize a wide range of tools in order to facilitate collaboration — the "
    "long-term benefits are substantial. Furthermore, these solutions demonstrate "
    "a comprehensive approach that plays a crucial role in the realm of modern "
    "business. It is worth noting that they do not require extensive training, "
    "and they can be implemented on a regular basis without difficulty."
)


def check(condition, message="assertion failed"):
    if not condition:
        raise AssertionError(message)


# -- edit plumbing ----------------------------------------------------------

def test_apply_edits():
    text = "hello world"
    out = apply_edits(text, [Edit(0, 5, "goodbye"), Edit(6, 11, "there")])
    check(out == "goodbye there", out)


def test_edit_buffer_composes_passes():
    buffer = EditBuffer("the quick brown fox")
    buffer.apply([Edit(4, 9, "slow")])            # -> "the slow brown fox"
    check(buffer.text() == "the slow brown fox", buffer.text())
    buffer.apply([Edit(9, 14, "red")])            # -> "the slow red fox"
    check(buffer.text() == "the slow red fox", buffer.text())
    edits = buffer.edits()
    check(apply_edits("the quick brown fox", edits) == "the slow red fox", edits)


def test_edit_buffer_edit_inside_an_earlier_edit():
    buffer = EditBuffer("please utilize this")
    buffer.apply([Edit(7, 14, "use")])            # -> "please use this"
    buffer.apply([Edit(7, 10, "sue")])            # typo inside the new word
    check(buffer.text() == "please sue this", buffer.text())
    check(apply_edits("please utilize this", buffer.edits()) == "please sue this")


# -- rewriting --------------------------------------------------------------

def test_level_zero_is_a_no_op():
    for settings in (Settings(level=0, typos=False, seed=1),
                     Settings(level=0, typos=True, seed=1)):
        out = Humanizer(settings).run(SAMPLE)
        check(out == SAMPLE, "level 0 changed the text (typos=%s)" % settings.typos)
    # ...unless a typo rate is asked for explicitly
    out = Humanizer(Settings(level=0, typo_rate=50, seed=1)).run(SAMPLE)
    check(out != SAMPLE, "explicit --typos was ignored at level 0")


def test_hard_wrapped_text_keeps_its_line_breaks():
    wrapped = ("Teams must utilize a wide range of tools in order\n"
               "to facilitate the work — the benefits are clear.\n"
               "It is important to note that this is not free.\n")
    for seed in range(20):
        out = Humanizer(Settings(level=10, seed=seed)).run(wrapped)
        check(out.count("\n") == wrapped.count("\n"),
              "line count changed at seed %d:\n%s" % (seed, out))


def test_em_dashes_are_removed():
    for seed in range(30):
        out = Humanizer(Settings(level=5, seed=seed, typos=False)).run(SAMPLE)
        check("—" not in out and "–" not in out, "em dash survived at seed %d" % seed)


def test_em_dashes_can_be_kept():
    out = Humanizer(Settings(level=5, seed=3, typos=False, em_dashes=False)).run(SAMPLE)
    check("—" in out, "--keep-em-dashes did not keep it")


def test_hyphens_loosen_sometimes():
    hits = 0
    for seed in range(40):
        out = Humanizer(Settings(level=9, seed=seed, typos=False)).run(
            "The long-term, well-known e-mail set-up is user-friendly.")
        if "long term" in out or "email" in out or "set up" in out or "user friendly" in out:
            hits += 1
    check(hits > 5, "hyphens almost never loosened (%d/40)" % hits)


def test_protected_hyphens_survive():
    for seed in range(40):
        out = Humanizer(Settings(level=10, seed=seed, typos=False)).run(
            "An x-ray of the t-shirt showed well-being.")
        check("x-ray" in out, "x-ray was split at seed %d" % seed)


def test_ai_tells_get_rewritten():
    out = Humanizer(Settings(level=8, seed=7, typos=False)).run(SAMPLE)
    for tell in ("utilize", "in order to", "plays a crucial role"):
        check(tell not in out.lower(), "%r survived: %s" % (tell, out))


def test_urls_and_code_are_left_alone():
    text = ("Please utilize https://example.com/in-order-to/utilize?x=1 and the "
            "`utilize_it()` helper, then e-mail me. " + SAMPLE)
    for seed in range(25):
        out = Humanizer(Settings(level=10, seed=seed)).run(text)
        check("https://example.com/in-order-to/utilize?x=1" in out,
              "URL was rewritten at seed %d:\n%s" % (seed, out))
        check("`utilize_it()`" in out, "code span was rewritten at seed %d" % seed)


def test_typos_appear_and_scale():
    settings = Settings(level=5, typo_rate=40, seed=11, lexical=False,
                        em_dashes=False, hyphens=False, contractions=False,
                        quotes=False, semicolons=False, seasoning=False)
    out = Humanizer(settings).run(SAMPLE * 2)
    check(out != SAMPLE * 2, "no typos introduced at rate 40")

    sparse = Humanizer(Settings(level=5, typo_rate=1, seed=11, lexical=False,
                                em_dashes=False, hyphens=False, contractions=False,
                                quotes=False, semicolons=False, seasoning=False))
    few = len(sparse.plan(SAMPLE * 2))
    many = len(Humanizer(settings).plan(SAMPLE * 2))
    check(many > few, "raising the typo rate did not add typos (%d vs %d)" % (many, few))


def test_typo_kinds_the_user_asked_for():
    """Lowercase sentence starts, dropped letters and doubled spaces all fire."""
    seen = set()
    for seed in range(60):
        humanizer = Humanizer(Settings(level=1, typo_rate=60, seed=seed,
                                       lexical=False, em_dashes=False,
                                       hyphens=False, contractions=False,
                                       quotes=False, semicolons=False,
                                       seasoning=False))
        humanizer.plan(SAMPLE * 2)
        seen.update(humanizer.stats)
    for kind in ("typo:lowercase_start", "typo:drop_letter", "typo:double_space"):
        check(kind in seen, "%s never fired; saw %s" % (kind, sorted(seen)))


def test_output_stays_readable():
    """A rewrite must not mangle word count or leave stray whitespace."""
    for seed in range(20):
        out = Humanizer(Settings(level=7, seed=seed)).run(SAMPLE)
        check("  \n" not in out, "stray whitespace at seed %d" % seed)
        check(not re.search(r"\s[,.]", out.replace(" .", "")),
              "floating punctuation at seed %d: %r" % (seed, out))
        # The sample is dense with removable filler, so it can lose a third of
        # its words legitimately. Growth is the suspicious direction.
        ratio = len(out.split()) / len(SAMPLE.split())
        check(0.60 < ratio < 1.30, "length drifted too far at seed %d (%.2f)" % (seed, ratio))


def test_seed_is_reproducible():
    a = Humanizer(Settings(level=7, seed=99)).run(SAMPLE)
    b = Humanizer(Settings(level=7, seed=99)).run(SAMPLE)
    check(a == b, "same seed produced different output")
    c = Humanizer(Settings(level=7, seed=100)).run(SAMPLE)
    check(a != c, "different seeds produced identical output")


# -- docx -------------------------------------------------------------------

def _docx(tmp_path, paragraphs):
    """Minimal but structurally real .docx."""
    body = []
    for runs in paragraphs:
        parts = "".join(
            '<w:r><w:rPr>%s</w:rPr><w:t xml:space="preserve">%s</w:t></w:r>'
            % (style, xml_escape(text)) for text, style in runs)
        body.append("<w:p><w:pPr><w:pStyle w:val=\"Normal\"/></w:pPr>%s</w:p>" % parts)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>%s<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body>'
        '</w:document>' % "".join(body)
    )
    path = os.path.join(tmp_path, "sample.docx")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships/>')
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", "<styles/>")
    return path


def test_docx_roundtrip_preserves_everything_else(tmp_path):
    source = _docx(tmp_path, [
        [("In order to utilize this, we must ", "<w:b/>"),
         ("demonstrate", "<w:i/>"),
         (" a comprehensive approach — it is important to note that.", "")],
        [("Short.", "")],
    ])
    destination = os.path.join(tmp_path, "out.docx")
    humanizer = Humanizer(Settings(level=8, seed=5))
    report = humanize_docx(source, destination, humanizer)

    check(report.rewritten >= 1, "no paragraph was rewritten")

    with zipfile.ZipFile(source) as a, zipfile.ZipFile(destination) as b:
        check(a.namelist() == b.namelist(), "zip entries changed")
        for name in a.namelist():
            if name != "word/document.xml":
                check(a.read(name) == b.read(name), "%s was modified" % name)
        xml = b.read("word/document.xml").decode("utf-8")

    # formatting and structure survive
    check(xml.count("<w:p>") == 2, "paragraph count changed")
    check("<w:b/>" in xml and "<w:i/>" in xml, "run formatting was dropped")
    check('<w:pStyle w:val="Normal"/>' in xml, "paragraph style was dropped")
    check("<w:sectPr>" in xml, "section properties were dropped")
    check(xml.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'),
          "XML declaration changed")
    check("—" not in xml, "em dash survived in the docx")
    check(any(k in report.stats for k in ("phrase", "word", "contraction")),
          "no wording was changed, only punctuation: %s" % report.stats)
    check(extract_docx_text(destination) != extract_docx_text(source),
          "document text is unchanged")


def test_docx_edit_spanning_runs_keeps_run_count(tmp_path):
    """'in order to' split across three runs is still matched and replaced."""
    source = _docx(tmp_path, [[("We did this in or", ""), ("der", "<w:b/>"),
                               (" to utilize the system properly.", "")]])
    replaced = 0
    for seed in range(10):
        destination = os.path.join(tmp_path, "out%d.docx" % seed)
        humanize_docx(source, destination,
                      Humanizer(Settings(level=9, seed=seed, typos=False)))
        with zipfile.ZipFile(destination) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        check(xml.count("<w:r>") == 3, "runs were merged or dropped at seed %d" % seed)
        check("<w:b/>" in xml, "bold run was lost at seed %d" % seed)
        text = extract_docx_text(destination)
        check(" ".join(text.split()) == text.strip(),
              "whitespace broke at seed %d: %r" % (seed, text))
        if "in order to" not in text.lower():
            replaced += 1
    check(replaced >= 7, "cross-run phrase matched only %d/10 times" % replaced)


def test_docx_in_place(tmp_path):
    source = _docx(tmp_path, [[("It is important to note that we utilize it — daily.", "")]])
    before = extract_docx_text(source)
    humanize_docx(source, source, Humanizer(Settings(level=7, seed=4)))
    after = extract_docx_text(source)
    check(before != after, "in-place edit did nothing")
    check(zipfile.ZipFile(source).testzip() is None, "archive is corrupt")


def test_docx_double_space_survives_the_xml(tmp_path):
    source = _docx(tmp_path, [[(SAMPLE, "")]])
    destination = os.path.join(tmp_path, "out.docx")
    settings = Settings(level=1, typo_rate=80, seed=3, lexical=False,
                        em_dashes=False, hyphens=False, contractions=False,
                        quotes=False, semicolons=False, seasoning=False)
    humanize_docx(source, destination, Humanizer(settings))
    text = extract_docx_text(destination)
    check(text != SAMPLE, "no typos landed in the docx")
    with zipfile.ZipFile(destination) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    check('xml:space="preserve"' in xml, "space preservation was dropped")


def test_self_closing_text_node_is_not_swallowed():
    xml = ('<w:p><w:r><w:t/></w:r><w:r><w:t xml:space="preserve"/></w:r>'
           '<w:r><w:t>We must utilize it in order to win.</w:t></w:r></w:p>')
    from humanizer.docxio import DocxReport
    out = humanize_part(xml, Humanizer(Settings(level=9, seed=1, typos=False)), DocxReport())
    check(out.count("<w:r>") == 3, "runs lost: %s" % out)
    check("<w:t/>" in out, "empty node was rewritten: %s" % out)


RICH_DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Utilize The System</w:t></w:r></w:p>
<w:p><w:r><w:t xml:space="preserve">In order to demonstrate this, we utilize a comprehensive approach </w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>and it is important to note that R&amp;D matters.</w:t></w:r></w:p>
<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> REF _Ref123 \\h utilize in order to </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>see above</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>
<w:p><w:del w:id="1" w:author="A"><w:r><w:delText>We must utilize in order to win.</w:delText></w:r></w:del><w:r><w:t xml:space="preserve"> Furthermore, it is essential to facilitate the process.</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>It is crucial to utilize numerous resources in order to succeed.</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>Approximately 5-10 items &lt;per&gt; batch.</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
<w:p><w:hyperlink r:id="rId5"><w:r><w:t>utilize this link in order to read more</w:t></w:r></w:hyperlink></w:p>
<w:p><w:r><w:t xml:space="preserve">Before the break</w:t></w:r><w:r><w:br/></w:r><w:r><w:t>after the break we utilize it.</w:t></w:r></w:p>
<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>"""

HEADER_XML = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
              '<w:p><w:r><w:t>It is important to note that this is a header.</w:t></w:r></w:p>'
              '</w:hdr>')


def _rich_docx(tmp_path):
    path = os.path.join(tmp_path, "rich.docx")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships/>')
        archive.writestr("word/document.xml", RICH_DOCUMENT)
        archive.writestr("word/header1.xml", HEADER_XML)
        archive.writestr("word/settings.xml", "<settings/>")
        archive.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\nnot-really")
    return path


def test_rich_docx_survives_intact(tmp_path):
    import xml.etree.ElementTree as ElementTree

    source = _rich_docx(tmp_path)
    destination = os.path.join(tmp_path, "out.docx")
    report = humanize_docx(source, destination, Humanizer(Settings(level=9, seed=8)))

    with zipfile.ZipFile(destination) as archive:
        check(archive.testzip() is None, "archive is corrupt")
        xml = archive.read("word/document.xml").decode("utf-8")
        header = archive.read("word/header1.xml").decode("utf-8")
        check(archive.read("word/media/image1.png") == b"\x89PNG\r\n\x1a\nnot-really",
              "binary part was touched")
        check(archive.read("word/settings.xml") == b"<settings/>", "settings changed")

    ElementTree.fromstring(xml)          # blows up if the rewrite broke the XML
    ElementTree.fromstring(header)

    # field codes and tracked deletions are off limits
    check("REF _Ref123 \\h utilize in order to " in xml, "a field code was rewritten")
    check("<w:delText>We must utilize in order to win.</w:delText>" in xml,
          "tracked deletion was rewritten")

    # structure survives
    for marker in ('<w:pStyle w:val="Heading1"/>', "<w:tbl>", "<w:tc>",
                   '<w:hyperlink r:id="rId5">', "<w:tab/>", "<w:br/>", "<w:sectPr>"):
        check(marker in xml, "%s was lost" % marker)
    check(xml.count("<w:p>") == RICH_DOCUMENT.count("<w:p>"), "paragraph count changed")
    check("&amp;" in xml and "&amp;amp;" not in xml, "ampersand escaping broke")
    check("&lt;per&gt;" in xml, "angle brackets broke")
    check("5-10" in xml, "a numeric range was mangled")

    # headers get rewritten too
    check("it is important to note that" not in header.lower(), "header was skipped")
    check(report.rewritten >= 4, "too little was rewritten: %d" % report.rewritten)


def test_tab_and_break_block_edits_across_them(tmp_path):
    """A phrase interrupted by a tab or a line break is not stitched together."""
    source = _rich_docx(tmp_path)
    for seed in range(8):
        destination = os.path.join(tmp_path, "out%d.docx" % seed)
        humanize_docx(source, destination, Humanizer(Settings(level=10, seed=seed)))
        with zipfile.ZipFile(destination) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        check("<w:tab/>" in xml and "<w:br/>" in xml,
              "a tab or break was eaten at seed %d" % seed)


def test_xml_escaping_roundtrip():
    for raw in ("a & b", "<tag>", 'quote " and \' apostrophe', "plain"):
        check(xml_unescape(xml_escape(raw)) == raw, raw)
    check(xml_unescape("&#65;&#x42;") == "AB")


def test_docx_ampersand_is_not_double_escaped(tmp_path):
    source = _docx(tmp_path, [[("Utilize R&D in order to win — always.", "")]])
    destination = os.path.join(tmp_path, "out.docx")
    humanize_docx(source, destination, Humanizer(Settings(level=9, seed=6, typos=False)))
    text = extract_docx_text(destination)
    check("R&D" in text, "ampersand was mangled: %r" % text)
    with zipfile.ZipFile(destination) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    check("&amp;amp;" not in xml, "ampersand was double escaped")


# -- runner for people without pytest ---------------------------------------

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
