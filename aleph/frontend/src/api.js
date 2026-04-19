// Thin fetch wrapper around the Aleph FastAPI backend.
// Authentication: the user logs in via ./login.html which stores a
// base64(user:pass) blob in localStorage under `aleph.basic_auth`. We
// add it to every request as `Authorization: Basic <b64>`. On a 401
// from the backend we clear the stored creds and bounce back to login.
// Write endpoints additionally require `X-Aleph-Key` (see login form).

const BASE = '/aleph/api';
const BASIC_STORAGE = 'aleph.basic_auth';
const WRITE_KEY_STORAGE = 'aleph.write_key';

export function getBasicAuth() {
  try {
    return localStorage.getItem(BASIC_STORAGE) || '';
  } catch {
    return '';
  }
}

export function clearAuth() {
  try {
    localStorage.removeItem(BASIC_STORAGE);
    localStorage.removeItem(WRITE_KEY_STORAGE);
  } catch {
    /* ignore */
  }
}

export function redirectToLogin() {
  window.location.replace('./login.html?force=1');
}

export function getWriteKey() {
  try {
    return localStorage.getItem(WRITE_KEY_STORAGE) || '';
  } catch {
    return '';
  }
}

export function setWriteKey(key) {
  try {
    if (key) localStorage.setItem(WRITE_KEY_STORAGE, key);
    else localStorage.removeItem(WRITE_KEY_STORAGE);
  } catch {
    /* ignore */
  }
}

async function req(path, { method = 'GET', body, write = false } = {}) {
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const basic = getBasicAuth();
  if (basic) headers['Authorization'] = 'Basic ' + basic;
  if (write) {
    const k = getWriteKey();
    if (k) headers['X-Aleph-Key'] = k;
  }
  const res = await fetch(BASE + path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: 'include',
  });
  if (res.status === 401) {
    clearAuth();
    redirectToLogin();
    const err = new Error('unauthorized');
    err.status = 401;
    throw err;
  }
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status} ${res.statusText}`);
    err.status = res.status;
    try {
      err.detail = await res.json();
    } catch {
      /* ignore */
    }
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

export async function fetchGraph(version) {
  const qs = version != null ? `?version=${encodeURIComponent(version)}` : '';
  return req(`/graph${qs}`);
}

export async function searchGraph(query, kind, limit = 15) {
  return req('/search', {
    method: 'POST',
    body: { query, kind, limit },
  });
}

export async function fetchNode(id) {
  return req(`/node/${encodeURIComponent(id)}`);
}

export async function remember(content, context) {
  return req('/remember', {
    method: 'POST',
    body: { content, context },
    write: true,
  });
}

export async function forget(id) {
  return req(`/forget/${encodeURIComponent(id)}`, {
    method: 'POST',
    write: true,
  });
}

export async function fetchNodeAudit(id, limit = 20) {
  return req(`/node/${encodeURIComponent(id)}/audit?limit=${limit}`);
}

// EventSource connection with auto-reconnect / backoff.
// The backend emits NAMED events (`event: memory_change`, `event: version_bump`,
// `event: ping`, `event: error`), so the default `onmessage` handler never
// fires. We must attach explicit listeners per event name.
// Returns a function that closes the stream.
export function openStream(onEvent) {
  let es = null;
  let closed = false;
  let backoff = 1000;

  function dispatch(name) {
    return (ev) => {
      if (!ev.data) return;
      try {
        const data = JSON.parse(ev.data);
        onEvent({ type: name, ...data });
      } catch {
        /* skip malformed */
      }
    };
  }

  function connect() {
    if (closed) return;
    try {
      es = new EventSource(BASE + '/graph/stream', { withCredentials: true });
    } catch (e) {
      scheduleReconnect();
      return;
    }
    es.onopen = () => {
      backoff = 1000;
    };
    // Named listeners — the backend uses them.
    es.addEventListener('memory_change', dispatch('memory_change'));
    es.addEventListener('version_bump', dispatch('version_bump'));
    es.addEventListener('error', (ev) => {
      // `error` is fired both on transport errors (no data) AND on explicit
      // server-side error events (with data). The former triggers reconnect.
      if (ev && ev.data) {
        try { onEvent({ type: 'error', ...JSON.parse(ev.data) }); } catch { /* */ }
        return;
      }
      if (es) { es.close(); es = null; }
      scheduleReconnect();
    });
    // Heartbeat — ignore but keeps the event listener registered so sse-starlette
    // doesn't time us out.
    es.addEventListener('ping', () => { /* heartbeat */ });
    // Fallback for unnamed events (shouldn't happen with our backend, but safe).
    es.onmessage = (ev) => {
      if (!ev.data) return;
      try { onEvent({ type: 'message', ...JSON.parse(ev.data) }); } catch { /* */ }
    };
  }

  function scheduleReconnect() {
    if (closed) return;
    const delay = Math.min(backoff, 30000);
    backoff = Math.min(backoff * 2, 30000);
    setTimeout(connect, delay);
  }

  connect();

  return () => {
    closed = true;
    if (es) es.close();
  };
}
