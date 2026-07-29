/**
 * Local mock of every AWS dependency the frontend talks to: Cognito Hosted UI +
 * token endpoint + JWKS, the HTTP API, an S3 presigned PUT target, and the
 * streaming chat Function URL.
 *
 * Exists so the UI can be exercised end to end with no AWS account. It is a
 * development fixture, not a reimplementation of the backend — handler logic
 * under backend/src is the real thing and is covered by the vitest suite.
 *
 * No dependencies beyond the Node standard library.
 *   node tools/mock-server.mjs
 */
import { createServer } from 'node:http';
import { createSign, generateKeyPairSync, randomUUID } from 'node:crypto';

const PORT = Number(process.env.MOCK_PORT ?? 4000);
const ORIGIN = process.env.MOCK_ORIGIN ?? `http://localhost:${PORT}`;
const CLIENT_ID = 'localmockclientid';
const USER = { sub: 'local-user-0001', email: 'dev@example.com' };

// ---------------------------------------------------------------- signing keys
const { publicKey, privateKey } = generateKeyPairSync('rsa', {
  modulusLength: 2048,
});
const KID = 'local-mock-key-1';
const jwkPublic = { ...publicKey.export({ format: 'jwk' }), kid: KID, use: 'sig', alg: 'RS256' };

const b64url = (input) =>
  Buffer.from(input).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

function signJwt(claims) {
  const header = b64url(JSON.stringify({ alg: 'RS256', typ: 'JWT', kid: KID }));
  const now = Math.floor(Date.now() / 1000);
  const payload = b64url(
    JSON.stringify({ iss: ORIGIN, aud: CLIENT_ID, iat: now, exp: now + 3600, ...claims }),
  );
  const signer = createSign('RSA-SHA256');
  signer.update(`${header}.${payload}`);
  return `${header}.${payload}.${b64url(signer.sign(privateKey))}`;
}

// --------------------------------------------------------------- in-memory state
/** documentId -> record */
const documents = new Map();
/** sessionId -> { sessionId, title, createdAt, updatedAt, messageCount, messages[] } */
const sessions = new Map();
/** pending auth codes -> nonce */
const codes = new Map();

const CORPUS = [
  {
    title: 'company_handbook.md',
    text: '年度带薪年假为 15 天，入职满三年后增加至 20 天。年假需提前两周在系统中提交申请，由直属主管审批。',
  },
  {
    title: 'product_faq.md',
    text: '标准套餐支持每月 10,000 次 API 调用；企业套餐不限量，并提供 99.9% 的服务级别协议。',
  },
  {
    title: 'support_runbook.md',
    text: '一级事故需在 15 分钟内响应，并在事故频道中同步状态。超过 30 分钟未解决须升级至值班经理。',
  },
];

// ------------------------------------------------------------------- utilities
function cors(res) {
  res.setHeader('access-control-allow-origin', '*');
  res.setHeader('access-control-allow-headers', 'authorization,content-type,if-none-match');
  res.setHeader('access-control-allow-methods', 'GET,POST,PUT,OPTIONS');
  res.setHeader('access-control-expose-headers', 'etag');
}

function json(res, status, body) {
  cors(res);
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks)));
  });
}

/** The mock trusts any bearer token; the real backend verifies it against Cognito. */
function authed(req) {
  return (req.headers.authorization ?? '').startsWith('Bearer ');
}

function citationsFor(query) {
  const q = query.toLowerCase();
  const scored = CORPUS.map((doc, i) => {
    const hit = ['年假', '假期', 'leave'].some((k) => q.includes(k)) && i === 0 ? 3
      : ['api', '套餐', '调用'].some((k) => q.includes(k)) && i === 1 ? 3
      : ['事故', '响应', '升级'].some((k) => q.includes(k)) && i === 2 ? 3
      : 1;
    return { doc, score: hit };
  });
  return scored.sort((a, b) => b.score - a.score).slice(0, 2);
}

// ---------------------------------------------------------------------- routes
const server = createServer(async (req, res) => {
  const url = new URL(req.url, ORIGIN);
  const path = url.pathname;

  if (req.method === 'OPTIONS') {
    cors(res);
    res.writeHead(204);
    return res.end();
  }

  // --- Cognito: JWKS -------------------------------------------------------
  if (path === '/.well-known/jwks.json') {
    return json(res, 200, { keys: [jwkPublic] });
  }

  // --- Cognito: Hosted UI. Auto-approves and bounces straight back. ---------
  if (path === '/oauth2/authorize') {
    const redirectUri = url.searchParams.get('redirect_uri');
    const state = url.searchParams.get('state') ?? '';
    const nonce = url.searchParams.get('nonce') ?? '';
    const code = randomUUID();
    codes.set(code, nonce);
    const target = new URL(redirectUri);
    target.searchParams.set('code', code);
    target.searchParams.set('state', state);
    cors(res);
    res.writeHead(302, { location: target.toString() });
    return res.end();
  }

  // --- Cognito: token exchange --------------------------------------------
  if (path === '/oauth2/token' && req.method === 'POST') {
    const form = new URLSearchParams((await readBody(req)).toString());
    const nonce = codes.get(form.get('code')) ?? '';
    codes.delete(form.get('code'));
    return json(res, 200, {
      token_type: 'Bearer',
      expires_in: 3600,
      id_token: signJwt({
        sub: USER.sub,
        email: USER.email,
        token_use: 'id',
        nonce,
        'cognito:username': USER.email,
      }),
      access_token: signJwt({
        sub: USER.sub,
        token_use: 'access',
        scope: 'openid email profile kb-api/access',
        client_id: CLIENT_ID,
        username: USER.email,
      }),
      refresh_token: 'mock-refresh-token',
    });
  }

  // --- S3: presigned PUT target -------------------------------------------
  if (path.startsWith('/s3/') && req.method === 'PUT') {
    const key = decodeURIComponent(path.slice('/s3/'.length));
    const body = await readBody(req);
    // The sidecar PUT is written server-side in the real flow; ignore it here.
    if (!key.endsWith('.metadata.json')) {
      const documentId = key.split('/')[2];
      const doc = documents.get(documentId);
      if (doc) {
        doc.sizeBytes = body.length;
        doc.status = 'INGESTING';
        // Mimic Bedrock ingestion latency, then reach a terminal state.
        setTimeout(() => {
          doc.status = 'READY';
          doc.updatedAt = new Date().toISOString();
        }, 2500);
      }
    }
    cors(res);
    res.writeHead(200, { etag: '"mock"' });
    return res.end();
  }

  // --- S3: presigned GET (download) ----------------------------------------
  if (path.startsWith('/s3-get/')) {
    const documentId = decodeURIComponent(path.slice('/s3-get/'.length));
    const doc = documents.get(documentId);
    cors(res);
    res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8' });
    return res.end(
      doc ? `Mock contents of ${doc.title}\n\n${CORPUS.map((c) => c.text).join('\n\n')}` : 'not found',
    );
  }

  if (!authed(req) && path.startsWith('/v1/')) {
    return json(res, 401, { message: 'missing bearer token' });
  }

  // --- HTTP API: create upload --------------------------------------------
  if (path === '/v1/uploads' && req.method === 'POST') {
    const body = JSON.parse((await readBody(req)).toString() || '{}');
    const documentId = randomUUID();
    const safeName = String(body.filename ?? 'file.md').replace(/[^A-Za-z0-9._-]/g, '_');
    const key = `uploads/${USER.sub}/${documentId}/${safeName}`;
    documents.set(documentId, {
      documentId,
      title: safeName,
      contentType: body.contentType ?? 'text/markdown',
      sizeBytes: body.sizeBytes ?? 0,
      status: 'UPLOADING',
      uploadedAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    });
    return json(res, 200, {
      documentId,
      key,
      expiresIn: 300,
      uploadUrl: `${ORIGIN}/s3/${encodeURIComponent(key)}`,
    });
  }

  // --- HTTP API: document download -----------------------------------------
  const dl = path.match(/^\/v1\/documents\/([^/]+)\/download$/);
  if (dl) {
    if (!documents.has(dl[1])) return json(res, 404, { message: 'not found' });
    return json(res, 200, { url: `${ORIGIN}/s3-get/${dl[1]}` });
  }

  // --- HTTP API: list documents --------------------------------------------
  if (path === '/v1/documents') {
    const items = [...documents.values()].sort((a, b) => b.uploadedAt.localeCompare(a.uploadedAt));
    return json(res, 200, { items });
  }

  // --- HTTP API: sessions ---------------------------------------------------
  if (path === '/v1/sessions') {
    const items = [...sessions.values()]
      .map((s) => ({
        sessionId: s.sessionId,
        title: s.title,
        createdAt: s.createdAt,
        updatedAt: s.updatedAt,
        messageCount: s.messageCount,
      }))
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
    return json(res, 200, { items });
  }
  const detail = path.match(/^\/v1\/sessions\/([^/]+)$/);
  if (detail) {
    const s = sessions.get(detail[1]);
    if (!s) return json(res, 404, { message: 'not found' });
    return json(res, 200, s);
  }

  // --- Chat Function URL: SSE ----------------------------------------------
  if (path === '/chat' && req.method === 'POST') {
    if (!authed(req)) return json(res, 401, { message: 'missing bearer token' });
    const body = JSON.parse((await readBody(req)).toString() || '{}');
    const message = String(body.message ?? '').trim();
    if (message === '') return json(res, 400, { message: 'message required' });

    const sessionId = body.sessionId ?? randomUUID();
    const now = new Date().toISOString();
    const session = sessions.get(sessionId) ?? {
      sessionId,
      title: message.slice(0, 60),
      createdAt: now,
      updatedAt: now,
      messageCount: 0,
      messages: [],
    };
    session.messages.push({ role: 'user', content: message, citations: [], createdAt: now });
    session.messageCount += 1;

    cors(res);
    res.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
      connection: 'keep-alive',
    });
    const send = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));

    send('session', { sessionId });
    await wait(120);
    send('tool_use', { name: 'search_knowledge_base', input: { query: message } });
    await wait(260);

    const hits = citationsFor(message);
    const citations = hits.map((h, i) => ({
      ref: i + 1,
      title: h.doc.title,
      documentId: [...documents.keys()][i] ?? `mock-doc-${i + 1}`,
      score: 0.9 - i * 0.12,
      snippet: h.doc.text.slice(0, 120),
    }));
    for (const c of citations) {
      send('citation', c);
      await wait(90);
    }

    const answer =
      `根据知识库中的资料：${hits[0].doc.text} [ref:1]` +
      (citations.length > 1 ? ` 另外，${hits[1].doc.text} [ref:2]` : '') +
      `\n\n（这是本地 mock 服务返回的回答，未调用 Amazon Bedrock。）`;

    // Stream in small chunks so the UI's incremental rendering is exercised,
    // including multi-byte characters split across frames.
    for (let i = 0; i < answer.length; i += 3) {
      send('text', { delta: answer.slice(i, i + 3) });
      await wait(18);
    }

    session.messages.push({
      role: 'assistant',
      content: answer,
      citations,
      createdAt: new Date().toISOString(),
    });
    session.messageCount += 1;
    session.updatedAt = new Date().toISOString();
    sessions.set(sessionId, session);

    send('done', {
      sessionId,
      stopReason: 'end_turn',
      usage: { inputTokens: 428, outputTokens: answer.length },
    });
    return res.end();
  }

  json(res, 404, { message: `no mock route for ${req.method} ${path}` });
});

server.listen(PORT, () => {
  const jwks = `${ORIGIN}/.well-known/jwks.json`;
  console.log(`mock AWS stack listening on ${ORIGIN}`);
  console.log(`  cognito domain : ${ORIGIN}`);
  console.log(`  jwks uri       : ${jwks}`);
  console.log(`  http api       : ${ORIGIN}`);
  console.log(`  chat url       : ${ORIGIN}/chat`);
  console.log(`  signed in as   : ${USER.email} (${USER.sub})`);
});
