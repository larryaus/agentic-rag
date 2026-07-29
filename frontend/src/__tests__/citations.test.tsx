// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { CitationText } from '../components/CitationText';

const citation = {
  ref: 1,
  title: 'handbook.md',
  documentId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  score: 0.9,
  snippet: 'Annual leave policy',
};

afterEach(cleanup);

describe('CitationText', () => {
  it('substitutes known markers with clickable chips and preserves text', () => {
    const onOpen = vi.fn();
    render(
      <p>
        <CitationText
          text="Before [ref:1] after."
          citations={[citation]}
          onOpen={onOpen}
        />
      </p>,
    );

    expect(screen.getByText(/Before/)).toHaveTextContent('Before 1 after.');
    fireEvent.click(screen.getByRole('button', { name: 'Open citation 1' }));
    expect(onOpen).toHaveBeenCalledWith(citation);
  });

  it('leaves an unknown marker as literal plain text', () => {
    render(
      <p>
        <CitationText
          text="Unknown [ref:99] remains."
          citations={[citation]}
          onOpen={() => undefined}
        />
      </p>,
    );
    expect(screen.getByText(/Unknown/)).toHaveTextContent(
      'Unknown [ref:99] remains.',
    );
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
