"""The rewrite engine.

Everything the humanizer does is modelled as an ``Edit``: a span of the source
string plus the text that should replace it. Nothing rewrites the document
wholesale. That matters for .docx, where the same edit list gets replayed onto
individual ``<w:t>`` nodes so runs, styles and everything else survive untouched.

Passes run in this order, each one over the output of the last:

    1. punctuation   em dashes out, ellipses and smart quotes normalised
    2. lexical       phrase and word swaps, contractions
    3. hyphens       "long-term" -> "long term", "e-mail" -> "email"
    4. seasoning     hedges, sentence openers (level >= 6 only)
    5. typos         see typos.py

Later passes see the text the earlier ones produced, so a word that only exists
because of a phrase swap can still pick up a typo. ``EditBuffer`` tracks the
composition and collapses the whole chain back down to edits against the
original string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import lexicon, lexicon_he
from .rng import Rng
from .typos import HEBREW_LETTERS, TypoEngine, is_hebrew

APOSTROPHES = "'’ʼ"


# --------------------------------------------------------------------------
# Edits
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Edit:
    """Replace ``source[start:end]`` with ``new``."""

    start: int
    end: int
    new: str
    kind: str = "edit"
    old: str = ""

    def __post_init__(self):
        if self.start > self.end:
            raise ValueError("edit start after end: %r" % (self,))

    @property
    def is_insertion(self) -> bool:
        return self.start == self.end


def dedupe(edits: Iterable[Edit]) -> List[Edit]:
    """Sort edits and drop any that overlap one already kept.

    Earlier edits in the input win, so callers put their preferred candidates
    first. Zero-width insertions are allowed to sit at the edge of a
    replacement but not inside one.
    """
    kept: List[Edit] = []
    taken: List[Tuple[int, int]] = []
    for edit in edits:
        clash = False
        for start, end in taken:
            if edit.start < end and start < edit.end:
                clash = True
                break
            if edit.is_insertion and start < edit.start < end:
                clash = True
                break
            if start == end and edit.start < start < edit.end:
                clash = True
                break
        if clash:
            continue
        kept.append(edit)
        taken.append((edit.start, edit.end))
    kept.sort(key=lambda e: (e.start, e.end))
    return kept


def apply_edits(text: str, edits: Sequence[Edit]) -> str:
    """Apply sorted, non-overlapping edits to ``text``."""
    out: List[str] = []
    cursor = 0
    for edit in sorted(edits, key=lambda e: (e.start, e.end)):
        if edit.start < cursor:
            raise ValueError("overlapping edits at %d" % edit.start)
        out.append(text[cursor:edit.start])
        out.append(edit.new)
        cursor = edit.end
    out.append(text[cursor:])
    return "".join(out)


def apply_edits_with_spans(text: str, edits: Sequence[Edit]):
    """Apply edits and report where each one landed in the *result*.

    Returns ``(result, spans)`` with each span as
    ``(start, end, kind, original)`` in result coordinates. A deletion gives a
    zero-width span, which a UI can still mark. Callers that want to highlight
    changes should use this rather than diffing the two strings afterwards: the
    edit list already knows exactly what moved and why.
    """
    out: List[str] = []
    spans: List[Tuple[int, int, str, str]] = []
    cursor = 0
    position = 0
    for edit in sorted(edits, key=lambda e: (e.start, e.end)):
        if edit.start < cursor:
            raise ValueError("overlapping edits at %d" % edit.start)
        carried = text[cursor:edit.start]
        out.append(carried)
        position += len(carried)
        start = position
        out.append(edit.new)
        position += len(edit.new)
        spans.append((start, position, edit.kind, edit.old))
        cursor = edit.end
    out.append(text[cursor:])
    return "".join(out), spans


class EditBuffer:
    """Accumulates several passes of edits and reduces them to one edit list.

    Internally the document is a list of chunks, each of which knows the span of
    the *source* it stands for. A pass edits the current text; the buffer maps
    those positions back onto chunks, splitting clean chunks so the resulting
    edits stay tight rather than swallowing whole paragraphs.
    """

    __slots__ = ("source", "_chunks")

    def __init__(self, source: str):
        self.source = source
        # each chunk: [orig_start, orig_end, text, kind]
        self._chunks: List[List] = [[0, len(source), source, ""]]

    def text(self) -> str:
        return "".join(chunk[2] for chunk in self._chunks)

    def _is_clean(self, chunk) -> bool:
        return chunk[2] == self.source[chunk[0]:chunk[1]]

    def _split_at(self, pos: int) -> int:
        """Return the index of the chunk starting at current-text ``pos``."""
        running = 0
        for index, chunk in enumerate(self._chunks):
            length = len(chunk[2])
            if pos == running:
                return index
            if pos < running + length:
                offset = pos - running
                start, end, text, kind = chunk
                if self._is_clean(chunk):
                    cut = start + offset
                    self._chunks[index:index + 1] = [
                        [start, cut, text[:offset], kind],
                        [cut, end, text[offset:], kind],
                    ]
                else:
                    # A rewritten chunk has no faithful mapping back into the
                    # source, so the left half becomes a pure insertion at its
                    # start and the right half keeps the full span.
                    self._chunks[index:index + 1] = [
                        [start, start, text[:offset], kind],
                        [start, end, text[offset:], kind],
                    ]
                return index + 1
            running += length
        return len(self._chunks)

    def apply(self, edits: Sequence[Edit]) -> None:
        """Apply a pass of edits expressed against ``self.text()``."""
        ordered = sorted(edits, key=lambda e: (e.start, e.end))
        for edit in reversed(ordered):
            left = self._split_at(edit.start)
            right = self._split_at(edit.end)
            covered = self._chunks[left:right]
            if covered:
                start = covered[0][0]
                end = covered[-1][1]
            else:  # pure insertion at a chunk boundary
                start = end = self._orig_pos_at(left)
            self._chunks[left:right] = [[start, end, edit.new, edit.kind]]

    def _orig_pos_at(self, index: int) -> int:
        if index < len(self._chunks):
            return self._chunks[index][0]
        if self._chunks:
            return self._chunks[-1][1]
        return 0

    def edits(self) -> List[Edit]:
        """Collapse the buffer into minimal edits against the source."""
        out: List[Edit] = []
        for start, end, text, kind in self._chunks:
            original = self.source[start:end]
            if text == original:
                continue
            out.append(Edit(start, end, text, kind or "edit", original))
        return out


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass
class Settings:
    """The slider, plus the switches for each family of change."""

    level: float = 5.0            # 0 = untouched, 10 = heavily reworded
    typo_rate: Optional[float] = None   # typos per 1000 words; None = from level
    seed: Optional[int] = None

    em_dashes: bool = True        # strip em/en dashes entirely
    hyphens: bool = True          # split or close up hyphenated compounds
    contractions: bool = True
    lexical: bool = True          # phrase and word swaps
    seasoning: bool = True        # hedges and sentence openers, level >= 6
    typos: bool = True
    quotes: bool = True           # curly quotes -> straight
    semicolons: bool = True       # some semicolons become full stops

    protect_first_sentence: bool = True
    preserve: List[str] = field(default_factory=list)

    def clamped_level(self) -> float:
        return max(0.0, min(10.0, float(self.level)))

    def effective_typo_rate(self) -> float:
        if self.typo_rate is not None:
            return max(0.0, float(self.typo_rate))
        level = self.clamped_level()
        if level <= 0:
            return 0.0
        # ~1.5 slips per 1000 words at level 1, ~11 at level 10.
        return 0.4 + 1.05 * level


def _p(base: float, slope: float, level: float, cap: float = 1.0) -> float:
    return max(0.0, min(cap, base + slope * level))


# --------------------------------------------------------------------------
# Pattern helpers
# --------------------------------------------------------------------------


def _token_pattern(token: str) -> str:
    """Escape one word, tolerating either apostrophe shape."""
    out = []
    for char in token:
        if char in APOSTROPHES:
            out.append("[" + re.escape(APOSTROPHES) + "]")
        else:
            out.append(re.escape(char))
    return "".join(out)


def _key_pattern(key: str) -> str:
    """Whitespace-flexible, boundary-anchored pattern for a lexicon key.

    Spaces and tabs only, never a newline: matching across a line break would
    let the replacement collapse it and silently reflow hard-wrapped text.
    """
    body = r"[ \t]+".join(_token_pattern(part) for part in key.split())
    prefix = r"\b" if key[:1].isalnum() else ""
    suffix = r"\b" if key[-1:].isalnum() else ""
    return prefix + body + suffix


_WS_RE = re.compile(r"\s+")

# Hebrew attaches ‏ו ה ב ל כ מ ש‎ directly to the word that follows, with no
# space. A replacement that ends in one of them has to close up against the
# next word or you get ‏אפשר לומר ש מדובר‎, which no one writes.
_HE_CLITICS = "ושבלכמה"
_HE_CLITIC_END_RE = re.compile("(?:^|\\s)([" + _HE_CLITICS + "]{1,2})$")
# Two-letter sequences drawn from those same letters that are words in their
# own right, and so stay detached.
_HE_CLITIC_WORDS = frozenset(
    "לו לה לי מה מי בו בה כה שב של כל הם הן שו מו הו וו".split())


def _ends_with_clitic(text: str) -> bool:
    match = _HE_CLITIC_END_RE.search(text)
    return bool(match) and match.group(1) not in _HE_CLITIC_WORDS


def _normkey(matched: str) -> str:
    text = _WS_RE.sub(" ", matched.strip()).lower()
    for apostrophe in APOSTROPHES[1:]:
        text = text.replace(apostrophe, "'")
    return text


def _match_case(source: str, replacement: str) -> str:
    """Carry the source's capitalisation over to the replacement."""
    if not source or not replacement:
        return replacement
    letters = [c for c in source if c.isalpha()]
    if len(letters) > 1 and all(c.isupper() for c in letters):
        return replacement.upper()
    if source[0].isupper() and replacement[0].islower():
        return replacement[0].upper() + replacement[1:]
    return replacement


# Spans the rewriter must not touch.
_PROTECT_PATTERNS = [
    r"https?://\S+",
    r"www\.[^\s,;]+",
    r"\b[\w.+-]+@[\w-]+\.[\w.]+\b",
    r"```.*?```",
    r"`[^`\n]+`",
    r"\[[\d\s,;–-]+\]",              # [1], [2, 3] citation markers
    r"\{\{?[^{}\n]{1,60}\}?\}",      # {placeholder}, {{var}}
    r"%[sdifr]\b",
    r"\b[A-Z][A-Za-z]*(?:\.[A-Za-z_]\w*)+\(?",   # dotted.identifiers
]
_PROTECT_RE = re.compile("|".join(_PROTECT_PATTERNS), re.DOTALL)


def protected_spans(text: str, extra: Sequence[str] = ()) -> List[Tuple[int, int]]:
    spans = [m.span() for m in _PROTECT_RE.finditer(text)]
    for pattern in extra:
        spans.extend(m.span() for m in re.finditer(pattern, text))
    spans.sort()
    return spans


def _blocked(spans: Sequence[Tuple[int, int]], start: int, end: int) -> bool:
    for span_start, span_end in spans:
        if span_start >= end:
            break
        if start < span_end and span_start < end:
            return True
    return False


SENTENCE_START_RE = re.compile(
    r"(?:^|(?<=[.!?][\"'”’)\]])\s+|(?<=[.!?])\s+)(?=[^\W\d_])", re.UNICODE)


def sentence_starts(text: str) -> List[int]:
    # The pattern consumes the gap between sentences, so the first letter of
    # the new sentence is at the *end* of the match, not its start.
    return [m.end() for m in SENTENCE_START_RE.finditer(text)]


# --------------------------------------------------------------------------
# The humanizer
# --------------------------------------------------------------------------


class Humanizer:
    """Rewrites text at a given intensity.

    ``plan`` returns edits against the input string; ``run`` returns the
    rewritten string. The .docx writer uses ``plan`` so it can place each edit
    inside the run it came from.
    """

    def __init__(self, settings: Optional[Settings] = None, **overrides):
        self.settings = settings or Settings(**overrides)
        if settings is not None and overrides:
            for key, value in overrides.items():
                setattr(self.settings, key, value)
        # Not random.Random: the browser build has to reproduce this stream
        # exactly from the same seed. See rng.py.
        self._rng = Rng(self.settings.seed)
        self._table, self._regex = self._build_table()
        self._typos = TypoEngine(self._rng)
        self._last_opener = ""
        self._recent: List[str] = []   # spans paragraphs; .docx plans each alone
        self.stats: Dict[str, int] = {}

    # -- table ----------------------------------------------------------

    def _build_table(self):
        """Both languages go in one table.

        Hebrew and Latin keys cannot collide — the scripts share no letters —
        so there is no language to detect and a document that mixes the two
        gets each phrase handled by the rules for its own script.
        """
        settings = self.settings
        level = settings.clamped_level()
        table: Dict[str, Tuple[List[str], str]] = {}

        def add(key, options, kind, overwrite=True):
            # An option identical to the key would be a no-op edit; drop it
            # rather than let it burn a probability roll.
            choices = [o for o in options if o != key]
            if not choices:
                return
            if overwrite or key not in table:
                table[key] = (choices, kind)

        if settings.lexical:
            for source in (lexicon, lexicon_he):
                for key, options in source.phrases_for_level(level).items():
                    add(key, options, "phrase")
                for key, options in source.words_for_level(level).items():
                    add(key, options, "word", overwrite=False)
        if settings.contractions and level >= 2:
            for key, value in lexicon.CONTRACTIONS.items():
                add(key, [value], "contraction")

        if not table:
            return table, None

        keys = sorted(table, key=lambda k: (-len(k), k))
        pattern = "|".join("(?:%s)" % _key_pattern(key) for key in keys)
        return table, re.compile(pattern, re.IGNORECASE)

    # -- passes ---------------------------------------------------------

    def _pass_punctuation(self, text: str, spans) -> List[Edit]:
        settings = self.settings
        edits: List[Edit] = []

        if settings.em_dashes:
            edits.extend(self._dash_edits(text, spans))

        if settings.quotes:
            straight = {"“": '"', "”": '"', "‘": "'",
                        "’": "'", "′": "'", "«": '"', "»": '"'}
            for index, char in enumerate(text):
                if char in straight and not _blocked(spans, index, index + 1):
                    edits.append(Edit(index, index + 1, straight[char], "quote", char))
            for match in re.finditer(r"…", text):
                if not _blocked(spans, *match.span()):
                    edits.append(Edit(match.start(), match.end(), "...", "quote", "…"))

        if settings.semicolons:
            probability = _p(0.10, 0.045, self.settings.clamped_level(), 0.55)
            for match in re.finditer(r";[ \t]+([a-z])", text):
                if _blocked(spans, *match.span()):
                    continue
                if self._rng.random() < probability:
                    edits.append(Edit(match.start(), match.end(),
                                      ". " + match.group(1).upper(),
                                      "semicolon", match.group(0)))

        return dedupe(edits)

    # Words that almost always open a clause rather than an afterthought. A
    # dash in front of one is joining two sentences, so it becomes a full stop
    # instead of a comma, which would leave a splice.
    _CLAUSE_STARTERS = frozenset(
        "the it this that they we you there these those he she i and but".split()
        + "זה זו זאת הם הן הוא היא אני אנחנו אתה יש אין כל וזה אבל וגם".split()
    )

    def _dash_edits(self, text: str, spans) -> List[Edit]:
        """Take every em dash out without breaking the sentence around it.

        A pair of dashes bracketing an aside has to be treated as a pair. Turning
        the first into a comma and the second into a full stop is how you end up
        with a stranded fragment, so pairs are resolved together.
        """
        # [ \t] rather than \s so a dash at the end of a line does not take the
        # line break with it and join two wrapped lines together
        matches = [m for m in re.finditer(r"[ \t]*(?:[—–]|--)[ \t]*", text)
                   if not _blocked(spans, *m.span())]
        if not matches:
            return []

        bounds = sentence_starts(text) + [len(text)]
        pairs = set()
        for index in range(len(bounds) - 1):
            low, high = bounds[index], bounds[index + 1]
            inside = [m for m in matches if low <= m.start() < high]
            for position in range(0, len(inside) - 1, 2):
                opening, closing = inside[position], inside[position + 1]
                # both must have text on the far side to be a real parenthetical
                if opening.start() > low and text[closing.end():high].strip():
                    pairs.add((opening.start(), closing.start()))

        bracketed = {}
        for opening, closing in pairs:
            style = ", " if self._rng.random() < 0.75 else "parens"
            bracketed[opening] = (style, True)
            bracketed[closing] = (style, False)

        edits: List[Edit] = []
        for match in matches:
            start, end = match.span()
            whole = match.group(0)
            before = text[:start].rstrip()
            after = text[end:]

            # A dash between two numbers is a range, leave it as a hyphen.
            if before[-1:].isdigit() and after[:1].isdigit():
                if whole != "-":
                    edits.append(Edit(start, end, "-", "dash", whole))
                continue
            if not after.strip():
                edits.append(Edit(start, end, "", "dash", whole))
                continue

            if start in bracketed:
                style, opening = bracketed[start]
                if style == "parens":
                    new = " (" if opening else ") "
                else:
                    new = ", "
                edits.append(Edit(start, end, new, "dash", whole))
                continue

            word = re.match(r"([A-Za-z']+|[" + HEBREW_LETTERS + r"]+)", after)
            clause = bool(word) and word.group(1).lower() in self._CLAUSE_STARTERS
            roll = self._rng.random()
            if clause and roll < 0.70 and after[:1].isalpha():
                # two independent clauses: a comma here would be a splice
                edits.append(Edit(start, end + 1, ". " + after[0].upper(),
                                  "dash", text[start:end + 1]))
            elif roll < 0.80:
                edits.append(Edit(start, end, ", ", "dash", whole))
            elif roll < 0.92:
                edits.append(Edit(start, end, " ", "dash", whole))
            else:
                edits.append(Edit(start, end, ": ", "dash", whole))
        return edits

    def _pass_lexical(self, text: str, spans) -> List[Edit]:
        if self._regex is None:
            return []
        level = self.settings.clamped_level()
        phrase_p = _p(0.34, 0.068, level)
        word_p = _p(0.20, 0.070, level)
        contraction_p = _p(0.28, 0.065, level)
        rates = {"phrase": phrase_p, "word": word_p, "contraction": contraction_p}

        starts = set(sentence_starts(text))
        edits: List[Edit] = []
        used: Dict[str, str] = {}
        for match in self._regex.finditer(text):
            start, end = match.span()
            if _blocked(spans, start, end):
                continue
            entry = self._table.get(_normkey(match.group(0)))
            if entry is None:
                continue
            options, kind = entry
            if self._rng.random() >= rates[kind]:
                continue
            key = _normkey(match.group(0))
            # Prefer a different wording than last time this key came up, so a
            # repeated word does not get the same substitute every paragraph.
            choices = [o for o in options if o != used.get(key)] or options
            # And avoid whatever was used very recently for *any* key, or two
            # different phrases both land on "in short" in adjacent sentences.
            fresh = [o for o in choices if o not in self._recent] or choices
            replacement = self._rng.choice(fresh)
            used[key] = replacement
            self._recent.append(replacement)
            del self._recent[:-6]

            if not replacement:
                edit = self._deletion(text, start, end, starts, kind)
            else:
                new = _match_case(match.group(0), replacement)
                if new == match.group(0) or self._doubles_a_word(text, end, new):
                    continue
                stop = end
                if _ends_with_clitic(new) and text[end:end + 1] == " ":
                    stop += 1      # ‏... ש‎ + ‏מדובר‎ -> ‏... שמדובר‎
                edit = Edit(start, stop, new, kind, text[start:stop])
            if edit is not None:
                edits.append(edit)
        return dedupe(edits)

    @staticmethod
    def _deletion(text, start, end, starts, kind) -> Optional[Edit]:
        """Cut a phrase out without leaving a hole where it stood.

        Deleting "it is important to note that " has to take the space after it
        as well, and if the phrase opened the sentence the next word has to pick
        up the capital it was carrying.
        """
        stop = end
        while stop < len(text) and text[stop] == " ":
            stop += 1
        new = ""
        if start in starts and stop < len(text) and text[stop].isalpha():
            new = text[stop].upper()
            stop += 1
        if stop >= len(text) and not new:
            # nothing follows; drop the space in front of the phrase instead
            while start > 0 and text[start - 1] == " ":
                start -= 1
        if start == end == stop:
            return None
        return Edit(start, stop, new, kind, text[start:stop])

    @staticmethod
    def _doubles_a_word(text: str, end: int, new: str) -> bool:
        """Guard against "many of" -> "a lot of" landing in front of "of"."""
        tail = new.rsplit(" ", 1)[-1].lower()
        if not tail:
            return False
        following = re.match(r"\s+([A-Za-z']+)", text[end:])
        return bool(following) and following.group(1).lower() == tail

    def _pass_hyphens(self, text: str, spans) -> List[Edit]:
        if not self.settings.hyphens:
            return []
        level = self.settings.clamped_level()
        split_p = _p(0.04, 0.030, level, 0.34)
        join_p = _p(0.25, 0.045, level, 0.70)
        edits: List[Edit] = []
        # Hebrew joins compounds with a maqaf as well as a plain hyphen, and
        # loosening those reads exactly the same way: ‏בית-ספר‎ -> ‏בית ספר‎.
        pattern = (r"\b([A-Za-z]{1,20})-([A-Za-z]{2,20})\b"
                   r"|([" + HEBREW_LETTERS + r"]{2,20})[-־]([" + HEBREW_LETTERS + r"]{2,20})")
        for match in re.finditer(pattern, text):
            start, end = match.span()
            if _blocked(spans, start, end):
                continue
            whole = match.group(0)
            lowered = whole.lower()

            if match.group(3):     # Hebrew: no prefix closing, always a space
                if lowered in lexicon_he.HYPHEN_KEEP:
                    continue
                if self._rng.random() < split_p:
                    edits.append(Edit(start, end,
                                      match.group(3) + " " + match.group(4),
                                      "hyphen", whole))
                continue

            # "check-ins" is the same compound as "check-in"
            singular = lowered[:-1] if lowered.endswith("s") else None
            if lowered in lexicon.HYPHEN_KEEP or singular in lexicon.HYPHEN_KEEP:
                continue
            closed = lexicon.HYPHEN_JOIN.get(lowered)
            if closed is None and singular in lexicon.HYPHEN_JOIN:
                closed = lexicon.HYPHEN_JOIN[singular] + "s"
            if closed is not None:
                if self._rng.random() < join_p:
                    edits.append(Edit(start, end, _match_case(whole, closed),
                                      "hyphen", whole))
                continue
            if self._rng.random() >= split_p:
                continue
            left = match.group(1)
            if len(left) == 1:
                # "e-mail" shape: a single-letter prefix closes up, it does not
                # become two words.
                new = left + match.group(2)
            else:
                new = left + " " + match.group(2)
            edits.append(Edit(start, end, new, "hyphen", whole))
        return dedupe(edits)

    def _pass_seasoning(self, text: str, spans) -> List[Edit]:
        level = self.settings.clamped_level()
        if not self.settings.seasoning or level < 6:
            return []
        opener_p = _p(-0.34, 0.070, level, 0.28)
        hedge_p = _p(-0.30, 0.060, level, 0.30)
        edits: List[Edit] = []

        starts = sentence_starts(text)
        last_opener = -99
        for index, position in enumerate(starts):
            if index == 0:            # never reword the opening sentence
                continue
            if index - last_opener < 3:
                continue              # keep them scattered, not every sentence
            if _blocked(spans, position, position + 1):
                continue
            stop = starts[index + 1] if index + 1 < len(starts) else len(text)
            sentence = text[position:stop]
            hebrew = is_hebrew(sentence[:60])
            source = lexicon_he if hebrew else lexicon

            head = sentence[:40].lower()
            if head.startswith(source.CONNECTIVE_STARTS):
                continue              # already has an opener, do not stack one
            if len(sentence.split()) < 6:
                continue              # this sentence is too short to carry one
            if self._rng.random() >= opener_p:
                continue
            match = re.match(r"[A-Za-z]+|[" + HEBREW_LETTERS + r"]+", sentence)
            if not match:
                continue
            first = match.group(0)
            if not hebrew and (first == "I" or (len(first) > 1 and first[1].isupper())):
                continue              # "I", acronyms and proper nouns keep their case
            # kept on the instance so the same opener does not turn up again in
            # the next paragraph of a .docx, which is planned separately
            choices = [o for o in source.OPENERS if o != self._last_opener]
            opener = self._rng.choice(choices)
            self._last_opener = opener
            # Hebrew has no capitals, so there is nothing to fold down
            new = opener + (first if hebrew else first[0].lower() + first[1:])
            edits.append(Edit(position, position + len(first), new, "opener", first))
            last_opener = index

        english = r"\b(?:is|are|was|were|seems?|looks?|feels?)\s+([a-z]{4,})\b"
        # Hebrew has no copula, so the slot is after the pronoun instead:
        # ‏זה חשוב‎ -> ‏זה די חשוב‎
        hebrew = (r"\b(?:הוא|היא|זה|זו|הם|הן|היה|הייתה)\s+"
                  r"([" + HEBREW_LETTERS + r"]{4,})\b")
        for pattern in (english, hebrew):
            for match in re.finditer(pattern, text):
                start, end = match.span(1)
                if _blocked(spans, start, end):
                    continue
                # "are honestly primarily carried out" — one qualifier is enough
                if match.group(1).endswith("ly"):
                    continue
                if self._rng.random() >= hedge_p:
                    continue
                source = lexicon_he if is_hebrew(match.group(1)) else lexicon
                hedge = self._rng.choice(source.HEDGES)
                edits.append(Edit(start, start, hedge + " ", "hedge", ""))

        return dedupe(edits)

    # -- entry points ---------------------------------------------------

    def plan(self, text: str) -> List[Edit]:
        """Edits that turn ``text`` into its humanized form."""
        self.stats = {}
        if not text.strip():
            return []
        if self.settings.clamped_level() <= 0 and not self.settings.typos:
            return []

        buffer = EditBuffer(text)
        extra = self.settings.preserve

        passes = (
            self._pass_punctuation,
            self._pass_lexical,
            self._pass_hyphens,
            self._pass_seasoning,
        )
        if self.settings.clamped_level() > 0:
            for stage in passes:
                current = buffer.text()
                spans = protected_spans(current, extra)
                edits = stage(current, spans)
                if edits:
                    self._count(edits)
                    buffer.apply(edits)

        if self.settings.typos:
            current = buffer.text()
            spans = protected_spans(current, extra)
            edits = self._typos.plan(
                current,
                rate=self.settings.effective_typo_rate(),
                blocked=spans,
                skip_first_sentence=self.settings.protect_first_sentence,
            )
            if edits:
                self._count(edits)
                buffer.apply(edits)

        return buffer.edits()

    def _count(self, edits: Sequence[Edit]) -> None:
        for edit in edits:
            self.stats[edit.kind] = self.stats.get(edit.kind, 0) + 1

    def run(self, text: str) -> str:
        return apply_edits(text, self.plan(text))
