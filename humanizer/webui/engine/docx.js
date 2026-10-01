/* Read and rewrite .docx in the browser, mirroring humanizer/docxio.py.
 *
 * No library. A .docx is a zip of XML parts; this reads the central directory,
 * inflates only the parts that hold body text, rewrites the character data
 * inside <w:t> elements, and repacks. Entries that are not touched keep their
 * original compressed bytes verbatim, which is both faster and more faithful
 * than decompressing and recompressing them.
 *
 * Zip in and out uses DecompressionStream and CompressionStream with
 * 'deflate-raw', both of which browsers provide natively, so the project stays
 * dependency-free. CRC32 is the one thing they do not provide, so it is below.
 *
 * Because edits are applied per run rather than per paragraph, a bold word stays
 * bold, a hyperlink stays a hyperlink, and styles, numbering, images, tracked
 * changes and right-to-left settings all come through unchanged. Runs are read
 * in document order and joined into paragraph text first, so a phrase Word
 * happened to split across three runs is still matched as one phrase.
 *
 * Left alone deliberately: <w:instrText> (field codes), <w:delText> (tracked
 * deletions) and anything outside a paragraph.
 */

import { applyEdits } from './edit.js';

const BODY_PART_RE = /^word\/(document\d*\.xml|header\d*\.xml|footer\d*\.xml|footnotes\.xml|endnotes\.xml)$/;
const COMMENT_PART_RE = /^word\/comments\d*\.xml$/;

/* Ordered alternation: every self-closing form must come before its paired
 * form, otherwise `<w:t xml:space="preserve"/>` parses as an opening tag and
 * the match runs on to the next `</w:t>`, eating a run of real text. */
const SCAN_RE = new RegExp(
  '(?<pSelf><w:p(?:\\s[^>]*?)?/>)'
  + '|(?<pOpen><w:p(?:\\s[^>]*?)?>)'
  + '|(?<pClose></w:p>)'
  + '|(?<tSelf><w:t(?:\\s[^>]*?)?/>)'
  + '|<w:t(?<attrs>(?:\\s[^>]*?)?)>(?<text>.*?)</w:t>'
  + '|(?<tab><w:tab(?:\\s[^>]*?)?/>)'
  + '|(?<brk><w:br(?:\\s[^>]*?)?/>)'
  + '|(?<nbhyphen><w:noBreakHyphen\\s*/>)'
  + '|(?<softhyphen><w:softHyphen\\s*/>)',
  'gs');

const ENTITY_RE = /&(?:#(\d+)|#x([0-9A-Fa-f]+)|(amp|lt|gt|quot|apos));/g;
const NAMED = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };
const SPACE_ATTR_RE = /\s+xml:space\s*=\s*"[^"]*"/g;
const WORD_COUNT_RE = /[A-Za-z']+|[֐-׿]+/gu;

export function xmlUnescape(text) {
  return text.replace(ENTITY_RE, (whole, decimal, hexadecimal, named) => {
    if (named) return NAMED[named];
    return String.fromCodePoint(parseInt(decimal || hexadecimal, decimal ? 10 : 16));
  });
}

export function xmlEscape(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* ------------------------------------------------------------------ zip */

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[i] = c >>> 0;
  }
  return table;
})();

export function crc32(bytes) {
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) {
    crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

async function throughStream(bytes, stream) {
  const written = new Blob([bytes]).stream().pipeThrough(stream);
  return new Uint8Array(await new Response(written).arrayBuffer());
}

const inflateRaw = (bytes) => throughStream(bytes, new DecompressionStream('deflate-raw'));
const deflateRaw = (bytes) => throughStream(bytes, new CompressionStream('deflate-raw'));

/** Parse the central directory. Returns one record per entry. */
function readZip(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);

  let eocd = -1;
  const earliest = Math.max(0, bytes.length - 66000);
  for (let i = bytes.length - 22; i >= earliest; i--) {
    if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('that is not a .docx file');

  const count = view.getUint16(eocd + 10, true);
  const directoryOffset = view.getUint32(eocd + 16, true);
  if (directoryOffset === 0xffffffff) {
    throw new Error('zip64 archives are not supported');
  }

  const decoder = new TextDecoder('utf-8');
  const entries = [];
  let cursor = directoryOffset;

  for (let i = 0; i < count; i++) {
    if (view.getUint32(cursor, true) !== 0x02014b50) {
      throw new Error('the archive directory is damaged');
    }
    const flags = view.getUint16(cursor + 8, true);
    if (flags & 0x1) throw new Error('encrypted archives are not supported');
    const nameLength = view.getUint16(cursor + 28, true);
    const extraLength = view.getUint16(cursor + 30, true);
    const commentLength = view.getUint16(cursor + 32, true);
    const entry = {
      name: decoder.decode(bytes.subarray(cursor + 46, cursor + 46 + nameLength)),
      flags,
      method: view.getUint16(cursor + 10, true),
      time: view.getUint16(cursor + 12, true),
      date: view.getUint16(cursor + 14, true),
      crc: view.getUint32(cursor + 16, true),
      compressedSize: view.getUint32(cursor + 20, true),
      size: view.getUint32(cursor + 24, true),
      internalAttrs: view.getUint16(cursor + 36, true),
      externalAttrs: view.getUint32(cursor + 38, true),
      localOffset: view.getUint32(cursor + 42, true),
      extra: bytes.subarray(cursor + 46 + nameLength,
        cursor + 46 + nameLength + extraLength),
    };

    // The local header tells us where the data starts; its name and extra
    // lengths can differ from the directory's.
    if (view.getUint32(entry.localOffset, true) !== 0x04034b50) {
      throw new Error(`bad local header for ${entry.name}`);
    }
    const localName = view.getUint16(entry.localOffset + 26, true);
    const localExtra = view.getUint16(entry.localOffset + 28, true);
    const dataStart = entry.localOffset + 30 + localName + localExtra;
    // Sizes come from the directory, never the local header: with a data
    // descriptor (flag bit 3) the local values are zero.
    entry.raw = bytes.subarray(dataStart, dataStart + entry.compressedSize);

    entries.push(entry);
    cursor += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

async function entryText(entry) {
  if (entry.method === 0) return new TextDecoder('utf-8').decode(entry.raw);
  if (entry.method !== 8) throw new Error(`unsupported compression in ${entry.name}`);
  return new TextDecoder('utf-8').decode(await inflateRaw(entry.raw));
}

/** Repack. Entries carrying `replacement` are recompressed; the rest are copied. */
async function writeZip(entries) {
  const encoder = new TextEncoder();
  const pieces = [];
  const directory = [];
  let offset = 0;

  for (const entry of entries) {
    const name = encoder.encode(entry.name);
    let { method, crc, compressedSize, size, raw } = entry;

    if (entry.replacement) {
      size = entry.replacement.length;
      crc = crc32(entry.replacement);
      raw = await deflateRaw(entry.replacement);
      compressedSize = raw.length;
      method = 8;
    }

    // Clear bit 3: we write real sizes, so there is no data descriptor. Keep
    // the rest, including bit 11 for UTF-8 names.
    const flags = entry.flags & ~0x8;

    const local = new Uint8Array(30 + name.length + entry.extra.length);
    const localView = new DataView(local.buffer);
    localView.setUint32(0, 0x04034b50, true);
    localView.setUint16(4, 20, true);
    localView.setUint16(6, flags, true);
    localView.setUint16(8, method, true);
    localView.setUint16(10, entry.time, true);
    localView.setUint16(12, entry.date, true);
    localView.setUint32(14, crc, true);
    localView.setUint32(18, compressedSize, true);
    localView.setUint32(22, size, true);
    localView.setUint16(26, name.length, true);
    localView.setUint16(28, entry.extra.length, true);
    local.set(name, 30);
    local.set(entry.extra, 30 + name.length);

    directory.push({ entry, name, flags, method, crc, compressedSize, size, offset });
    pieces.push(local, raw);
    offset += local.length + raw.length;
  }

  const directoryOffset = offset;
  for (const item of directory) {
    const header = new Uint8Array(46 + item.name.length + item.entry.extra.length);
    const view = new DataView(header.buffer);
    view.setUint32(0, 0x02014b50, true);
    view.setUint16(4, 20, true);
    view.setUint16(6, 20, true);
    view.setUint16(8, item.flags, true);
    view.setUint16(10, item.method, true);
    view.setUint16(12, item.entry.time, true);
    view.setUint16(14, item.entry.date, true);
    view.setUint32(16, item.crc, true);
    view.setUint32(20, item.compressedSize, true);
    view.setUint32(24, item.size, true);
    view.setUint16(28, item.name.length, true);
    view.setUint16(30, item.entry.extra.length, true);
    view.setUint16(32, 0, true);
    view.setUint16(34, 0, true);
    view.setUint16(36, item.entry.internalAttrs, true);
    view.setUint32(38, item.entry.externalAttrs, true);
    view.setUint32(42, item.offset, true);
    header.set(item.name, 46);
    header.set(item.entry.extra, 46 + item.name.length);
    pieces.push(header);
    offset += header.length;
  }

  const end = new Uint8Array(22);
  const endView = new DataView(end.buffer);
  endView.setUint32(0, 0x06054b50, true);
  endView.setUint16(8, directory.length, true);
  endView.setUint16(10, directory.length, true);
  endView.setUint32(12, offset - directoryOffset, true);
  endView.setUint32(16, directoryOffset, true);
  pieces.push(end);

  const total = pieces.reduce((sum, piece) => sum + piece.length, 0);
  const out = new Uint8Array(total);
  let at = 0;
  for (const piece of pieces) { out.set(piece, at); at += piece.length; }
  return out;
}

/* ------------------------------------------------------------- the XML */

/**
 * Group every text-bearing node in a part by the paragraph it sits in.
 *
 * Tracks <w:p> depth rather than regex-matching paragraph bodies, so a
 * paragraph nested inside a text box does not truncate the one containing it.
 */
function scanParagraphs(xml) {
  const groups = [];
  const stack = [];
  SCAN_RE.lastIndex = 0;

  for (const match of xml.matchAll(SCAN_RE)) {
    const g = match.groups;
    if (g.pOpen !== undefined) {
      const group = [];
      groups.push(group);
      stack.push(group);
      continue;
    }
    if (g.pClose !== undefined) { stack.pop(); continue; }
    if (g.pSelf !== undefined) continue;
    if (!stack.length) continue;        // text outside any paragraph, leave it

    const current = stack[stack.length - 1];
    const offset = current.length ? current[current.length - 1].end : 0;

    if (g.tSelf !== undefined) continue;
    if (g.text !== undefined) {
      const plain = xmlUnescape(g.text);
      current.push({
        start: offset,
        end: offset + plain.length,
        text: plain,
        editable: true,
        tagStart: match.index,
        tagEnd: match.index + match[0].length,
        attrs: g.attrs || '',
      });
      continue;
    }

    let filler = null;
    if (g.tab !== undefined) filler = '\t';
    else if (g.brk !== undefined) filler = '\n';
    else if (g.nbhyphen !== undefined) filler = '-';
    else if (g.softhyphen !== undefined) filler = '';
    if (!filler) continue;
    current.push({
      start: offset, end: offset + filler.length, text: filler, editable: false,
    });
  }

  return groups.filter((group) => group.length);
}

const paragraphText = (segments) => segments.map((s) => s.text).join('');

/** Which editable segment should host an edit starting at `position`. */
function ownerIndex(segments, position) {
  for (let i = 0; i < segments.length; i++) {
    const segment = segments[i];
    if (segment.editable && segment.start <= position && position < segment.end) return i;
  }
  // An insertion at the very end of the paragraph belongs to the last run.
  for (let i = segments.length - 1; i >= 0; i--) {
    if (segments[i].editable && segments[i].end === position) return i;
  }
  for (let i = segments.length - 1; i >= 0; i--) if (segments[i].editable) return i;
  return null;
}

/** Drop edits that would run over a tab, a line break or a field code. */
function usable(segments, edits) {
  const fixed = segments.filter((s) => !s.editable && s.end > s.start);
  return edits.filter((item) => {
    if (item.end <= item.start) return true;
    return !fixed.some((s) => item.start < s.end && s.start < item.end);
  });
}

/**
 * Distribute paragraph-level edits across the runs they fall in.
 *
 * An edit that straddles several runs puts its whole replacement in the first
 * one and deletes the covered text from the rest. That keeps the replacement
 * under the formatting of the run the text started in, which is what a person
 * retyping the sentence would end up with.
 */
function rewriteSegments(segments, edits) {
  const ordered = [...edits].sort((a, b) => a.start - b.start || a.end - b.end);
  const owners = new Map();
  ordered.forEach((item, position) => {
    const index = ownerIndex(segments, item.start);
    if (index !== null) owners.set(position, index);
  });

  const changed = new Map();
  segments.forEach((segment, index) => {
    if (!segment.editable) return;
    const pieces = [];
    let cursor = 0;
    const length = segment.text.length;

    ordered.forEach((item, position) => {
      const owns = owners.get(position) === index;
      let start = Math.max(item.start, segment.start) - segment.start;
      let end = Math.min(item.end, segment.end) - segment.start;
      if (!owns && end <= start) return;
      if (end < 0 || start > length) return;
      start = Math.max(start, cursor);
      end = Math.max(end, start);
      pieces.push(segment.text.slice(cursor, start));
      if (owns) pieces.push(item.new);
      cursor = Math.min(end, length);
    });
    pieces.push(segment.text.slice(cursor));

    const next = pieces.join('');
    if (next !== segment.text) changed.set(index, next);
  });
  return changed;
}

function splice(xml, replacements) {
  const out = [];
  let cursor = 0;
  for (const [start, end, text] of replacements.sort((a, b) => a[0] - b[0])) {
    out.push(xml.slice(cursor, start));
    out.push(text);
    cursor = end;
  }
  out.push(xml.slice(cursor));
  return out.join('');
}

/** Rebuild a <w:t>, keeping its attributes but forcing space preservation so
 *  leading, trailing and doubled spaces survive. */
function buildTag(attrs, text) {
  return `<w:t${attrs.replace(SPACE_ATTR_RE, '')} xml:space="preserve">${xmlEscape(text)}</w:t>`;
}

function humanizePart(xml, humanizer, report) {
  const replacements = [];

  for (const segments of scanParagraphs(xml)) {
    const text = paragraphText(segments);
    if (!text.trim()) continue;
    report.paragraphs += 1;
    report.words += (text.match(WORD_COUNT_RE) || []).length;

    const edits = usable(segments, humanizer.plan(text));
    if (!edits.length) continue;
    for (const [kind, count] of Object.entries(humanizer.stats)) {
      report.stats[kind] = (report.stats[kind] || 0) + count;
    }

    const changed = rewriteSegments(segments, edits);
    if (!changed.size) continue;
    report.rewritten += 1;
    report.edits += edits.length;
    report.changes.push({ before: text, after: applyEdits(text, edits) });
    for (const [index, next] of changed) {
      const segment = segments[index];
      replacements.push([segment.tagStart, segment.tagEnd,
        buildTag(segment.attrs, next)]);
    }
  }

  return replacements.length ? splice(xml, replacements) : xml;
}

/* --------------------------------------------------------------- public */

/**
 * Rewrite the prose in a .docx. Takes and returns bytes; every entry that is
 * not a body part is copied across untouched.
 */
export async function humanizeDocx(bytes, humanizer, { includeComments = false } = {}) {
  const entries = readZip(bytes);
  if (!entries.some((e) => /^word\/document\d*\.xml$/.test(e.name))) {
    throw new Error('that does not look like a Word document');
  }

  const report = {
    paragraphs: 0, rewritten: 0, words: 0, edits: 0,
    stats: {}, parts: [], changes: [],
  };
  const encoder = new TextEncoder();

  for (const entry of entries) {
    const wanted = BODY_PART_RE.test(entry.name)
      || (includeComments && COMMENT_PART_RE.test(entry.name));
    if (!wanted) continue;
    const xml = await entryText(entry);
    const next = humanizePart(xml, humanizer, report);
    if (next !== xml) {
      entry.replacement = encoder.encode(next);
      report.parts.push(entry.name);
    }
  }

  return { file: await writeZip(entries), report };
}

/** Plain text of a document, one paragraph per line. Read-only. */
export async function extractDocxText(bytes, { includeComments = false } = {}) {
  const lines = [];
  for (const entry of readZip(bytes)) {
    if (!BODY_PART_RE.test(entry.name)
        && !(includeComments && COMMENT_PART_RE.test(entry.name))) continue;
    const xml = await entryText(entry);
    for (const segments of scanParagraphs(xml)) lines.push(paragraphText(segments));
  }
  return lines.join('\n');
}
