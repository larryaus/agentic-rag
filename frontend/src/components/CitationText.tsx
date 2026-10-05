import type { Citation } from '@kb/shared';
import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

const MARKER = /\[ref:(\d+)\]/g;
const CITATION_HREF = '#citation-';

export function CitationText(props: {
  text: string;
  citations: Citation[];
  onOpen: (citation: Citation) => void;
}): ReactNode {
  const byRef = new Map(
    props.citations.map((citation) => [citation.ref, citation]),
  );
  // Known markers become links so the Markdown parser carries them through as inline
  // nodes wherever they appear. Unknown markers are left alone and render literally.
  const source = props.text.replace(MARKER, (full, rawRef: string) =>
    byRef.has(Number(rawRef)) ? `[${rawRef}](${CITATION_HREF}${rawRef})` : full,
  );

  return (
    <div className="markdown">
      {/* Raw HTML in model output stays inert: react-markdown renders it as text. */}
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            const citation = href?.startsWith(CITATION_HREF)
              ? byRef.get(Number(href.slice(CITATION_HREF.length)))
              : undefined;
            if (citation === undefined) {
              return (
                <a href={href} target="_blank" rel="noopener noreferrer">
                  {children}
                </a>
              );
            }
            return (
              <sup>
                <button
                  type="button"
                  className="citation-chip"
                  title={`${citation.title}: ${citation.snippet}`}
                  aria-label={`Open citation ${citation.ref}`}
                  onClick={() => props.onOpen(citation)}
                >
                  {citation.ref}
                </button>
              </sup>
            );
          },
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
