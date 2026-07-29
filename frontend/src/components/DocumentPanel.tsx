import type {
  DocumentSummary,
  Page,
  UploadResponse,
} from '@kb/shared';
import { useCallback, useEffect, useState } from 'react';

import type { AppConfig } from '../config';
import { getAccessToken } from '../auth/auth';
import { apiFetch } from '../api/http';

function putWithProgress(opts: {
  file: File;
  url: string;
  contentType: string;
  onProgress: (percent: number) => void;
}): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('PUT', opts.url);
    request.setRequestHeader('Content-Type', opts.contentType);
    request.setRequestHeader('If-None-Match', '*');
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) {
        opts.onProgress(Math.round((event.loaded / event.total) * 100));
      }
    });
    request.addEventListener('load', () => {
      if (request.status >= 200 && request.status < 300) resolve();
      else reject(new Error(`Upload failed (${request.status})`));
    });
    request.addEventListener('error', () =>
      reject(new Error('Upload failed')),
    );
    request.send(opts.file);
  });
}

function uploadContentType(file: File): string {
  const extension = file.name.toLowerCase().split('.').at(-1);
  const byExtension: Record<string, string> = {
    pdf: 'application/pdf',
    txt: 'text/plain',
    md: 'text/markdown',
    html: 'text/html',
    htm: 'text/html',
  };
  return extension === undefined ? file.type : (byExtension[extension] ?? file.type);
}

export function DocumentPanel(props: {
  config: AppConfig;
}): React.JSX.Element {
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [progress, setProgress] = useState<number>();
  const [error, setError] = useState<string>();

  const refresh = useCallback(async () => {
    const response = await apiFetch(props.config, '/v1/documents');
    const page = (await response.json()) as Page<DocumentSummary>;
    setDocuments(page.items);
  }, [props.config]);

  useEffect(() => {
    void refresh().catch((cause: unknown) => {
      setError(cause instanceof Error ? cause.message : 'Could not list documents');
    });
  }, [refresh]);

  useEffect(() => {
    if (
      !documents.some((document) =>
        ['UPLOADING', 'PENDING', 'INGESTING'].includes(document.status),
      )
    ) {
      return undefined;
    }
    const timer = window.setInterval(() => void refresh(), 10_000);
    return () => window.clearInterval(timer);
  }, [documents, refresh]);

  const upload = async (file: File): Promise<void> => {
    setError(undefined);
    setProgress(0);
    try {
      await getAccessToken(props.config);
      const contentType = uploadContentType(file);
      const response = await apiFetch(props.config, '/v1/uploads', {
        method: 'POST',
        body: JSON.stringify({
          filename: file.name,
          contentType,
          sizeBytes: file.size,
        }),
      });
      const signed = (await response.json()) as UploadResponse;
      await putWithProgress({
        file,
        url: signed.uploadUrl,
        contentType,
        onProgress: setProgress,
      });
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Upload failed');
    } finally {
      setProgress(undefined);
    }
  };

  return (
    <section className="panel documents-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Shared library</p>
          <h2>Documents</h2>
        </div>
        <label className="upload-button">
          Upload
          <input
            type="file"
            accept=".pdf,.txt,.md,.html,.htm"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file !== undefined) void upload(file);
              event.target.value = '';
            }}
          />
        </label>
      </div>
      {progress === undefined ? null : (
        <div className="progress" aria-label={`Upload ${progress}%`}>
          <span style={{ width: `${progress}%` }} />
        </div>
      )}
      {error === undefined ? null : <p className="error-banner">{error}</p>}
      <ul className="document-list">
        {documents.map((document) => (
          <li key={document.documentId}>
            <span className="document-name">{document.title}</span>
            <span className={`status status-${document.status.toLowerCase()}`}>
              {document.status}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
