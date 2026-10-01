/* Edit primitives, mirroring the first part of humanizer/core.py.
 *
 * Everything the engine does is an edit: a span of the source plus the text
 * that replaces it. Nothing rewrites a string wholesale, which is what lets the
 * .docx writer replay the same list onto individual runs and keep formatting.
 */

/** An edit replaces source[start, end) with `text`. */
export function edit(start, end, text, kind = 'edit', old = '') {
  if (start > end) throw new Error(`edit start after end: ${start} > ${end}`);
  return { start, end, new: text, kind, old };
}

const byPosition = (a, b) => a.start - b.start || a.end - b.end;

/**
 * Sort edits and drop any that overlap one already kept. Earlier edits win, so
 * callers put their preferred candidates first. Zero-width insertions may sit
 * at the edge of a replacement but not inside one.
 */
export function dedupe(edits) {
  const kept = [];
  const taken = [];
  for (const candidate of edits) {
    let clash = false;
    for (const [start, end] of taken) {
      if (candidate.start < end && start < candidate.end) { clash = true; break; }
      if (candidate.start === candidate.end
          && start < candidate.start && candidate.start < end) { clash = true; break; }
      if (start === end
          && candidate.start < start && start < candidate.end) { clash = true; break; }
    }
    if (clash) continue;
    kept.push(candidate);
    taken.push([candidate.start, candidate.end]);
  }
  kept.sort(byPosition);   // stable, as Python's sort is
  return kept;
}

/** Apply sorted, non-overlapping edits to `text`. */
export function applyEdits(text, edits) {
  const out = [];
  let cursor = 0;
  for (const item of [...edits].sort(byPosition)) {
    if (item.start < cursor) throw new Error(`overlapping edits at ${item.start}`);
    out.push(text.slice(cursor, item.start));
    out.push(item.new);
    cursor = item.end;
  }
  out.push(text.slice(cursor));
  return out.join('');
}

/**
 * Apply edits and report where each one landed in the *result*, as
 * {start, end, kind, old}. A deletion gives a zero-width span, which the UI
 * still marks. This is what the page highlights from — the edit list already
 * knows what moved and why, so there is nothing to diff.
 */
export function applyEditsWithSpans(text, edits) {
  const out = [];
  const spans = [];
  let cursor = 0;
  let position = 0;
  for (const item of [...edits].sort(byPosition)) {
    if (item.start < cursor) throw new Error(`overlapping edits at ${item.start}`);
    const carried = text.slice(cursor, item.start);
    out.push(carried);
    position += carried.length;
    const start = position;
    out.push(item.new);
    position += item.new.length;
    spans.push({ start, end: position, kind: item.kind, old: item.old });
    cursor = item.end;
  }
  out.push(text.slice(cursor));
  return { text: out.join(''), spans };
}

/**
 * Accumulates several passes of edits and reduces them to one list against the
 * original string.
 *
 * The document is held as chunks, each knowing the span of the source it stands
 * for. A pass edits the current text; the buffer maps those positions back onto
 * chunks, splitting clean ones so the resulting edits stay tight instead of
 * swallowing whole paragraphs.
 */
export class EditBuffer {
  constructor(source) {
    this.source = source;
    // each chunk: [origStart, origEnd, text, kind]
    this.chunks = [[0, source.length, source, '']];
  }

  text() {
    let out = '';
    for (const chunk of this.chunks) out += chunk[2];
    return out;
  }

  isClean(chunk) {
    return chunk[2] === this.source.slice(chunk[0], chunk[1]);
  }

  /** Index of the chunk starting at current-text `pos`, splitting if needed. */
  splitAt(pos) {
    let running = 0;
    for (let index = 0; index < this.chunks.length; index++) {
      const chunk = this.chunks[index];
      const length = chunk[2].length;
      if (pos === running) return index;
      if (pos < running + length) {
        const offset = pos - running;
        const [start, end, text, kind] = chunk;
        if (this.isClean(chunk)) {
          const cut = start + offset;
          this.chunks.splice(index, 1,
            [start, cut, text.slice(0, offset), kind],
            [cut, end, text.slice(offset), kind]);
        } else {
          // A rewritten chunk has no faithful mapping back into the source, so
          // the left half becomes a pure insertion at its start and the right
          // half keeps the full span.
          this.chunks.splice(index, 1,
            [start, start, text.slice(0, offset), kind],
            [start, end, text.slice(offset), kind]);
        }
        return index + 1;
      }
      running += length;
    }
    return this.chunks.length;
  }

  /** Apply a pass of edits expressed against this.text(). */
  apply(edits) {
    const ordered = [...edits].sort(byPosition);
    for (let i = ordered.length - 1; i >= 0; i--) {
      const item = ordered[i];
      const left = this.splitAt(item.start);
      const right = this.splitAt(item.end);
      const covered = this.chunks.slice(left, right);
      let start;
      let end;
      if (covered.length) {
        start = covered[0][0];
        end = covered[covered.length - 1][1];
      } else {                       // pure insertion at a chunk boundary
        start = end = this.origPosAt(left);
      }
      this.chunks.splice(left, right - left, [start, end, item.new, item.kind]);
    }
  }

  origPosAt(index) {
    if (index < this.chunks.length) return this.chunks[index][0];
    if (this.chunks.length) return this.chunks[this.chunks.length - 1][1];
    return 0;
  }

  /** Collapse the buffer into minimal edits against the source. */
  edits() {
    const out = [];
    for (const [start, end, text, kind] of this.chunks) {
      const original = this.source.slice(start, end);
      if (text === original) continue;
      out.push(edit(start, end, text, kind || 'edit', original));
    }
    return out;
  }
}
