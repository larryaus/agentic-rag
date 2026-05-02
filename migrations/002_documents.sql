CREATE TABLE IF NOT EXISTS documents (
  id          BIGSERIAL PRIMARY KEY,
  title       TEXT NOT NULL,
  source_uri  TEXT,
  mime_type   TEXT NOT NULL,
  byte_size   BIGINT NOT NULL,
  sha256      TEXT NOT NULL UNIQUE,
  metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
  id           BIGSERIAL PRIMARY KEY,
  document_id  BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ord          INT NOT NULL,
  content      TEXT NOT NULL,
  token_count  INT NOT NULL,
  embedding    vector(1024) NOT NULL,
  tsv          tsvector GENERATED ALWAYS AS
                 (to_tsvector('simple', content)) STORED,
  metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (document_id, ord)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
  ON chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS chunks_doc_id  ON chunks (document_id);
