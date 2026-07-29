import { defineConfig } from 'vitest/config';

export default defineConfig({
  resolve: {
    dedupe: ['react', 'react-dom'],
  },
  test: {
    include: [
      'backend/src/__tests__/**/*.test.ts',
      'frontend/src/__tests__/**/*.{test,spec}.{ts,tsx}',
      'infra/test/**/*.test.ts',
    ],
    environment: 'node',
    setupFiles: ['./vitest.setup.ts'],
    restoreMocks: true,
    clearMocks: true,
  },
});
