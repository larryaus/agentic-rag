import { writeFileSync } from 'node:fs';

import { fixtures } from '../shared/src/fixtures.ts';

writeFileSync(new URL('../shared/fixtures/contract.json', import.meta.url), JSON.stringify(fixtures, null, 2) + '\n');
