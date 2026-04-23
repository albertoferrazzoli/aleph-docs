// Thin fetch wrapper around the Aleph FastAPI backend.
// Authentication: the user logs in via ./login.html which posts to
// POST /auth/login. The backend replies with a Set-Cookie for an
// HttpOnly session cookie — fetch/EventSource carry it automatically
// when `credentials: 'include'` / `withCredentials: true` is set.
// On a 401 we redirect back to the login page.
//
// We intentionally no longer read any auth material from localStorage
// for READ paths — the cookie is the source of truth. Write endpoints
// still need an X-Aleph-Key header, which IS kept in localStorage
// because it's orthogonal to the session (a human might have a
// session but not the write key).

const BASE = '/aleph/api';
const WRITE_KEY_STORAGE = 'aleph.write_key';
// Legacy key left readable for migration; never written anymore.
const LEGACY_BASIC_STORAGE = 'aleph.basic_auth';

export function clearAuth() {
  try {
    localStorage.removeItem(LEGACY_BASIC_STORAGE);
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

export async function logout() {
  try {
    await fetch(BASE + '/auth/logout', {
      method: 'POST',
      credentials: 'include',
    });
  } catch {
    /* ignore — the cookie Max-Age will expire it anyway */
  }
  clearAuth();
  redirectToLogin();
}

async function req(path, { method = 'GET', body, write = false } = {}) {
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
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

export async function fetchWorkspaces() {
  return req('/workspaces');
}

export async function setActiveWorkspace(name, reindex = false) {
  return req('/workspaces/active', {
    method: 'POST',
    body: { name, reindex },
  });
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
