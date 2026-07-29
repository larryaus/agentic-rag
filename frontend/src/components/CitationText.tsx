import type { Citation } from '@kb/shared';
import type { ReactNode } from 'react';

const MARKER = /\[ref:(\d+)\]/g;

export function CitationText(props: {
  text: string;
  citations: Citation[];
  onOpen: (citation: Citation) => void;
}): ReactNode {
  const byRef = new Map(
    props.citations.map((citation) => [citation.ref, citation]),
  );
  const nodes: ReactNode[] = [];
  let cursor = 0;
  for (const match of props.text.matchAll(MARKER)) {
    const full = match[0];
    const rawRef = match[1];
    const index = match.index;
    if (rawRef === undefined) continue;
    nodes.push(props.text.slice(cursor, index));
    const citation = byRef.get(Number(rawRef));
    if (citation === undefined) {
      nodes.push(full);
    } else {
      nodes.push(
        <sup key={`${index}-${rawRef}`}>
          <button
            type="button"
            className="citation-chip"
            title={`${citation.title}: ${citation.snippet}`}
            aria-label={`Open citation ${rawRef}`}
            onClick={() => props.onOpen(citation)}
          >
            {rawRef}
          </button>
        </sup>,
      );
    }
    cursor = index + full.length;
  }
  nodes.push(props.text.slice(cursor));
  return nodes;
}
