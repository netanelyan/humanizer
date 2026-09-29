"""Read and rewrite .docx without disturbing the formatting.

No third-party library and, more to the point, no re-serialisation. A .docx is
a zip of XML parts; this module rewrites only the character data inside
``<w:t>`` elements of the parts that hold body text, splices those strings back
into the original XML byte-for-byte, and repacks the archive with every other
entry copied across untouched.

Because edits are applied per run rather than per paragraph, a bold word stays
bold, a hyperlink stays a hyperlink, and styles, numbering, images, tracked
changes and comments all come through unchanged. Runs are read in document
order and joined into paragraph text first, so a phrase that Word happened to
split across three runs is still matched as one phrase.

Parts that are deliberately left alone: ``<w:instrText>`` (field codes),
``<w:delText>`` (tracked deletions) and anything outside a paragraph.
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .core import Edit, Humanizer, apply_edits

# Parts that carry prose. Order is irrelevant, each is handled on its own.
BODY_PART_RE = re.compile(
    r"^word/("
    r"document\d*\.xml"
    r"|header\d*\.xml"
    r"|footer\d*\.xml"
    r"|footnotes\.xml"
    r"|endnotes\.xml"
    r")$"
)
COMMENT_PART_RE = re.compile(r"^word/comments\d*\.xml$")

# Ordered alternation: every self-closing form must come before its paired
# form, otherwise `<w:t xml:space="preserve"/>` parses as an opening tag and
# the match runs on to the next `</w:t>`, eating a run of real text.
_SCAN_RE = re.compile(
    r"(?P<p_self><w:p(?:\s[^>]*?)?/>)"
    r"|(?P<p_open><w:p(?:\s[^>]*?)?>)"
    r"|(?P<p_close></w:p>)"
    r"|(?P<t_self><w:t(?:\s[^>]*?)?/>)"
    r"|<w:t(?P<attrs>(?:\s[^>]*?)?)>(?P<text>.*?)</w:t>"
    r"|(?P<tab><w:tab(?:\s[^>]*?)?/>)"
    r"|(?P<brk><w:br(?:\s[^>]*?)?/>)"
    r"|(?P<nbhyphen><w:noBreakHyphen\s*/>)"
    r"|(?P<softhyphen><w:softHyphen\s*/>)",
    re.DOTALL,
)

_ENTITY_RE = re.compile(r"&(?:#(\d+)|#x([0-9A-Fa-f]+)|(amp|lt|gt|quot|apos));")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
_SPACE_ATTR_RE = re.compile(r'\s+xml:space\s*=\s*"[^"]*"')


def xml_unescape(text: str) -> str:
    def swap(match):
        decimal, hexadecimal, named = match.groups()
        if named:
            return _NAMED[named]
        return chr(int(decimal) if decimal else int(hexadecimal, 16))

    return _ENTITY_RE.sub(swap, text)


def xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class _Segment:
    """One stretch of a paragraph's text, and where it lives in the XML."""

    start: int          # offset in the paragraph's plain text
    end: int
    text: str
    editable: bool
    tag_start: int = 0  # offset of the whole <w:t ...>...</w:t> in the part
    tag_end: int = 0
    attrs: str = ""


@dataclass
class DocxReport:
    paragraphs: int = 0
    rewritten: int = 0
    words: int = 0
    edits: int = 0
    stats: Dict[str, int] = field(default_factory=dict)
    parts: List[str] = field(default_factory=list)
    changes: List[Tuple[str, str]] = field(default_factory=list)   # (before, after)

    def merge_stats(self, stats: Dict[str, int]) -> None:
        for key, value in stats.items():
            self.stats[key] = self.stats.get(key, 0) + value


def _scan_paragraphs(xml: str) -> List[List[_Segment]]:
    """Group every text-bearing node in a part by the paragraph it sits in.

    Tracks `<w:p>` depth rather than regex-matching paragraph bodies, so a
    paragraph nested inside a text box does not truncate the one containing it.
    """
    groups: List[List[_Segment]] = []
    stack: List[List[_Segment]] = []

    for match in _SCAN_RE.finditer(xml):
        kind = match.lastgroup
        if match.group("p_open") is not None:
            group: List[_Segment] = []
            groups.append(group)
            stack.append(group)
            continue
        if match.group("p_close") is not None:
            if stack:
                stack.pop()
            continue
        if match.group("p_self") is not None:
            continue
        if not stack:
            continue   # text outside any paragraph, leave it alone

        current = stack[-1]
        offset = current[-1].end if current else 0

        if match.group("t_self") is not None:
            continue
        if match.group("text") is not None:
            plain = xml_unescape(match.group("text"))
            current.append(_Segment(
                start=offset, end=offset + len(plain), text=plain, editable=True,
                tag_start=match.start(), tag_end=match.end(),
                attrs=match.group("attrs") or "",
            ))
            continue

        filler = {"tab": "\t", "brk": "\n", "nbhyphen": "-", "softhyphen": ""}.get(kind)
        if filler is None:
            continue
        if filler:
            current.append(_Segment(offset, offset + len(filler), filler, False))

    return [g for g in groups if g]


def _paragraph_text(segments: Sequence[_Segment]) -> str:
    return "".join(segment.text for segment in segments)


def _owner_index(segments: Sequence[_Segment], position: int) -> Optional[int]:
    """Which editable segment should host an edit starting at ``position``."""
    for index, segment in enumerate(segments):
        if segment.editable and segment.start <= position < segment.end:
            return index
    # An insertion at the very end of the paragraph belongs to the last run.
    for index in range(len(segments) - 1, -1, -1):
        if segments[index].editable and segments[index].end == position:
            return index
    for index in range(len(segments) - 1, -1, -1):
        if segments[index].editable:
            return index
    return None


def _usable(segments: Sequence[_Segment], edits: Sequence[Edit]) -> List[Edit]:
    """Drop edits that would run over a tab, a line break or a field code."""
    fixed = [s for s in segments if not s.editable and s.end > s.start]
    out = []
    for edit in edits:
        if edit.end > edit.start and any(
            edit.start < s.end and s.start < edit.end for s in fixed
        ):
            continue
        out.append(edit)
    return out


def _rewrite_segments(segments: List[_Segment], edits: Sequence[Edit]) -> Dict[int, str]:
    """Distribute paragraph-level edits across the runs they fall in.

    An edit that straddles several runs puts its whole replacement in the first
    one and deletes the covered text from the rest. That keeps the replacement
    under the formatting of the run the text started in, which is what a person
    retyping the sentence would end up with.
    """
    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    owners: Dict[int, int] = {}
    for position, edit in enumerate(ordered):
        index = _owner_index(segments, edit.start)
        if index is not None:
            owners[position] = index

    changed: Dict[int, str] = {}
    for index, segment in enumerate(segments):
        if not segment.editable:
            continue
        pieces: List[str] = []
        cursor = 0
        length = len(segment.text)
        for position, edit in enumerate(ordered):
            owns = owners.get(position) == index
            start = max(edit.start, segment.start) - segment.start
            end = min(edit.end, segment.end) - segment.start
            if not owns and end <= start:
                continue
            if end < 0 or start > length:
                continue
            start = max(start, cursor)
            end = max(end, start)
            pieces.append(segment.text[cursor:start])
            if owns:
                pieces.append(edit.new)
            cursor = min(end, length)
        pieces.append(segment.text[cursor:])
        new_text = "".join(pieces)
        if new_text != segment.text:
            changed[index] = new_text
    return changed


def _splice(xml: str, replacements: List[Tuple[int, int, str]]) -> str:
    out: List[str] = []
    cursor = 0
    for start, end, text in sorted(replacements):
        out.append(xml[cursor:start])
        out.append(text)
        cursor = end
    out.append(xml[cursor:])
    return "".join(out)


def _build_tag(attrs: str, text: str) -> str:
    """Rebuild a `<w:t>` element, keeping its attributes but forcing space
    preservation so leading, trailing and doubled spaces survive."""
    attrs = _SPACE_ATTR_RE.sub("", attrs)
    return '<w:t%s xml:space="preserve">%s</w:t>' % (attrs, xml_escape(text))


def humanize_part(xml: str, humanizer: Humanizer, report: DocxReport) -> str:
    replacements: List[Tuple[int, int, str]] = []

    for segments in _scan_paragraphs(xml):
        text = _paragraph_text(segments)
        if not text.strip():
            continue
        report.paragraphs += 1
        report.words += len(re.findall(r"[A-Za-z']+", text))

        edits = _usable(segments, humanizer.plan(text))
        if not edits:
            continue
        report.merge_stats(humanizer.stats)

        changed = _rewrite_segments(segments, edits)
        if not changed:
            continue
        report.rewritten += 1
        report.edits += len(edits)
        report.changes.append((text, apply_edits(text, edits)))
        for index, new_text in changed.items():
            segment = segments[index]
            replacements.append(
                (segment.tag_start, segment.tag_end,
                 _build_tag(segment.attrs, new_text))
            )

    if not replacements:
        return xml
    return _splice(xml, replacements)


def humanize_docx(source: str, destination: str, humanizer: Humanizer,
                  include_comments: bool = False) -> DocxReport:
    """Rewrite the prose in ``source`` and write the result to ``destination``.

    Every zip entry that is not a body part is copied verbatim, with its
    original timestamp and compression settings, so the output opens as the
    same document with the same look.
    """
    report = DocxReport()
    in_place = os.path.abspath(source) == os.path.abspath(destination)
    target = destination + ".tmp" if in_place else destination

    with zipfile.ZipFile(source, "r") as archive:
        names = archive.namelist()
        if "word/document.xml" not in names and "word/document2.xml" not in names:
            raise ValueError("%s does not look like a Word document" % source)

        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
            for info in archive.infolist():
                data = archive.read(info.filename)
                wanted = BODY_PART_RE.match(info.filename) or (
                    include_comments and COMMENT_PART_RE.match(info.filename)
                )
                if wanted:
                    xml = data.decode("utf-8")
                    new_xml = humanize_part(xml, humanizer, report)
                    if new_xml != xml:
                        report.parts.append(info.filename)
                        data = new_xml.encode("utf-8")
                copy = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                copy.compress_type = info.compress_type
                copy.external_attr = info.external_attr
                copy.internal_attr = info.internal_attr
                copy.create_system = info.create_system
                copy.comment = info.comment
                out.writestr(copy, data)

    if in_place:
        shutil.move(target, destination)
    return report


def extract_docx_text(source: str, include_comments: bool = False) -> str:
    """Plain text of a document, one paragraph per line. Read-only."""
    lines: List[str] = []
    with zipfile.ZipFile(source, "r") as archive:
        for info in archive.infolist():
            if not (BODY_PART_RE.match(info.filename) or
                    (include_comments and COMMENT_PART_RE.match(info.filename))):
                continue
            xml = archive.read(info.filename).decode("utf-8")
            for segments in _scan_paragraphs(xml):
                lines.append(_paragraph_text(segments))
    return "\n".join(lines)
