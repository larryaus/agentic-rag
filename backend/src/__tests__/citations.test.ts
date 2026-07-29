import { describe, expect, it } from 'vitest';

import { extractRefs, resolveCitations } from '../lib/citations';
import { chunk } from './agent-fixtures';

describe('citations', () => {
  it('deduplicates refs in appearance order', () => {
    expect(extractRefs('A [ref:2], B [ref:1], again [ref:2].')).toEqual([
      2, 1,
    ]);
  });

  it('drops refs that are absent from the citation index', () => {
    const index = new Map([[1, chunk(0, 'x'.repeat(300))]]);
    expect(resolveCitations('Known [ref:1], fake [ref:99].', index)).toEqual([
      expect.objectContaining({
        ref: 1,
        snippet: 'x'.repeat(240),
      }),
    ]);
  });
});
