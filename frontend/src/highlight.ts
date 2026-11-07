// Finds evidence excerpts in a source text and splits the text into plain and marked segments.

export interface Mark {
  key: string; // which proposal value this excerpt supports, e.g. "1" for line 1, "S" for ship-to
  excerpt: string;
}

export interface Segment {
  text: string;
  key?: string;
}

function find(text: string, excerpt: string): [number, number] | null {
  const needle = excerpt.trim();
  if (needle.length < 2) return null;
  let i = text.indexOf(needle);
  if (i >= 0) return [i, i + needle.length];
  i = text.toLowerCase().indexOf(needle.toLowerCase());
  if (i >= 0) return [i, i + needle.length];
  // Whitespace may differ between the stored excerpt and the text (line breaks in PDFs).
  const parts = needle.split(/\s+/).map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const m = new RegExp(parts.join("\\s+"), "i").exec(text);
  return m ? [m.index, m.index + m[0].length] : null;
}

export function segments(text: string, marks: Mark[]): Segment[] {
  const found: { start: number; end: number; key: string }[] = [];
  for (const m of marks) {
    const r = find(text, m.excerpt);
    if (r && !found.some((f) => r[0] < f.end && r[1] > f.start)) found.push({ start: r[0], end: r[1], key: m.key });
  }
  found.sort((a, b) => a.start - b.start);
  const out: Segment[] = [];
  let pos = 0;
  for (const f of found) {
    if (f.start > pos) out.push({ text: text.slice(pos, f.start) });
    out.push({ text: text.slice(f.start, f.end), key: f.key });
    pos = f.end;
  }
  if (pos < text.length) out.push({ text: text.slice(pos) });
  return out;
}
