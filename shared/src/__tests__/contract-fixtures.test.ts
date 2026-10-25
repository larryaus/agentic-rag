import { readFileSync } from 'node:fs';
import { expect, it } from 'vitest';

import { fixtures } from '../fixtures';

it('keeps checked-in contract JSON equal to the typed literals', () => {
  expect(readFileSync(new URL('../../fixtures/contract.json', import.meta.url), 'utf8')).toBe(JSON.stringify(fixtures, null, 2) + '\n');
});
