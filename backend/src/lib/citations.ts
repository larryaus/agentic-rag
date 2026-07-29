import type { Citation } from '@kb/shared';

import type { RawChunk } from './retrieve';

export const CITATION_RE = /\[ref:(\d+)\]/g;

export function extractRefs(text: string): number[] {
  const seen = new Set<number>();
  const refs: number[] = [];
  for (const match of text.matchAll(CITATION_RE)) {
    const value = match[1];
    if (value === undefined) {
      continue;
    }
    const ref = Number(value);
    if (!seen.has(ref)) {
      seen.add(ref);
      refs.push(ref);
    }
  }
  return refs;
}

export function resolveCitations(
  text: string,
  index: Map<number, RawChunk>,
): Citation[] {
  return extractRefs(text).flatMap((ref) => {
    const chunk = index.get(ref);
    if (chunk === undefined) {
      return [];
    }
    return [
      {
        ref,
        title: chunk.title,
        documentId: chunk.documentId,
        score: chunk.score,
        snippet: chunk.text.slice(0, 240),
      },
    ];
  });
}
