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
      <CitationText
        text="Before [ref:1] after."
        citations={[citation]}
        onOpen={onOpen}
      />,
    );

    expect(screen.getByText(/Before/)).toHaveTextContent('Before 1 after.');
    fireEvent.click(screen.getByRole('button', { name: 'Open citation 1' }));
    expect(onOpen).toHaveBeenCalledWith(citation);
  });

  it('leaves an unknown marker as literal plain text', () => {
    render(
      <CitationText
        text="Unknown [ref:99] remains."
        citations={[citation]}
        onOpen={() => undefined}
      />,
    );
    expect(screen.getByText(/Unknown/)).toHaveTextContent(
      'Unknown [ref:99] remains.',
    );
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('renders Markdown emphasis instead of literal asterisks', () => {
    const view = render(
      <CitationText
        text="请查阅**员工手册**。"
        citations={[]}
        onOpen={() => undefined}
      />,
    );
    expect(view.container.querySelector('strong')).toHaveTextContent(
      '员工手册',
    );
    expect(view.container).not.toHaveTextContent('**');
  });

  it('renders Markdown lists as list items', () => {
    render(
      <CitationText
        text={'Options:\n\n- First\n- Second'}
        citations={[]}
        onOpen={() => undefined}
      />,
    );
    expect(
      screen.getAllByRole('listitem').map((item) => item.textContent),
    ).toEqual(['First', 'Second']);
  });

  it('keeps citation chips clickable inside formatted text', () => {
    const onOpen = vi.fn();
    render(
      <CitationText
        text={'- **Leave** is 15 days [ref:1]'}
        citations={[citation]}
        onOpen={onOpen}
      />,
    );
    expect(screen.getByRole('listitem')).toHaveTextContent(
      'Leave is 15 days 1',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Open citation 1' }));
    expect(onOpen).toHaveBeenCalledWith(citation);
  });

  it('opens ordinary links in a new tab without leaking the opener', () => {
    render(
      <CitationText
        text="See [the policy](https://example.com/policy)."
        citations={[]}
        onOpen={() => undefined}
      />,
    );
    const link = screen.getByRole('link', { name: 'the policy' });
    expect(link).toHaveAttribute('href', 'https://example.com/policy');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('never turns model output into live HTML', () => {
    const view = render(
      <CitationText
        text={'Hello <img src="x" onerror="alert(1)"> <script>alert(1)</script>'}
        citations={[]}
        onOpen={() => undefined}
      />,
    );
    expect(view.container.querySelector('img')).toBeNull();
    expect(view.container.querySelector('script')).toBeNull();
  });
});
