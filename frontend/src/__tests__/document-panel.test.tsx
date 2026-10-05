// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { DocumentSummary } from '@kb/shared';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AppConfig } from '../config';
import { DocumentPanel } from '../components/DocumentPanel';

const mocks = vi.hoisted(() => ({
  getAccessToken: vi.fn(),
  apiFetch: vi.fn(),
}));

vi.mock('../auth/auth', () => ({ getAccessToken: mocks.getAccessToken }));
vi.mock('../api/http', () => ({ apiFetch: mocks.apiFetch }));

const config: AppConfig = {
  userPoolId: 'pool',
  userPoolClientId: 'client',
  cognitoDomain: 'https://auth.example.test',
  apiUrl: 'https://api.example.test',
  chatUrl: 'https://chat.example.test',
  awsRegion: 'us-east-1',
};

function doc(
  documentId: string,
  title: string,
  status: DocumentSummary['status'],
): DocumentSummary {
  return {
    documentId,
    title,
    contentType: 'text/markdown',
    sizeBytes: 10,
    status,
    uploadedAt: '2026-10-05T00:00:00.000Z',
  };
}

// Serves the list from `library` and applies DELETE to it, like the real API.
function serveLibrary(library: DocumentSummary[]): void {
  mocks.apiFetch.mockImplementation(
    (_config: AppConfig, path: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') {
        const documentId = decodeURIComponent(path.split('/').at(-1) ?? '');
        const index = library.findIndex(
          (item) => item.documentId === documentId,
        );
        if (index >= 0) library.splice(index, 1);
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      return Promise.resolve(
        new Response(JSON.stringify({ items: [...library] })),
      );
    },
  );
}

beforeEach(() => {
  mocks.apiFetch.mockReset();
  mocks.getAccessToken.mockReset().mockResolvedValue('token');
});
afterEach(cleanup);

describe('DocumentPanel failed document removal', () => {
  it('offers removal only for failed documents', async () => {
    serveLibrary([
      doc('ready-id', 'handbook.md', 'READY'),
      doc('failed-id', 'broken.md', 'FAILED'),
    ]);
    render(<DocumentPanel config={config} />);

    expect(
      await screen.findByRole('button', { name: 'Remove broken.md' }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Remove handbook.md' }),
    ).not.toBeInTheDocument();
  });

  it('deletes the failed document and drops it from the list', async () => {
    serveLibrary([
      doc('ready-id', 'handbook.md', 'READY'),
      doc('failed id', 'broken.md', 'FAILED'),
    ]);
    render(<DocumentPanel config={config} />);

    fireEvent.click(
      await screen.findByRole('button', { name: 'Remove broken.md' }),
    );

    await waitFor(() =>
      expect(screen.queryByText('broken.md')).not.toBeInTheDocument(),
    );
    expect(screen.getByText('handbook.md')).toBeInTheDocument();
    expect(mocks.apiFetch).toHaveBeenCalledWith(
      config,
      '/v1/documents/failed%20id',
      { method: 'DELETE' },
    );
  });

  it('shows the server message when removal is refused', async () => {
    serveLibrary([doc('failed-id', 'broken.md', 'FAILED')]);
    render(<DocumentPanel config={config} />);
    const button = await screen.findByRole('button', {
      name: 'Remove broken.md',
    });
    mocks.apiFetch.mockRejectedValueOnce(
      new Error('Only failed documents can be removed'),
    );

    fireEvent.click(button);

    expect(
      await screen.findByText('Only failed documents can be removed'),
    ).toBeInTheDocument();
    expect(screen.getByText('broken.md')).toBeInTheDocument();
  });
});
