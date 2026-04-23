import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import Scene from './Scene.jsx';
import {
  TopBar, LeftRail, RightPanel, BottomBar, RememberBox, HoverTip, TweaksPanel,
} from './UI.jsx';
import { useAlephStore } from './store.js';
import {
  fetchGraph, searchGraph, remember as apiRemember, forget as apiForget,
  openStream, getWriteKey, setWriteKey, clearAuth, redirectToLogin,
  fetchNodeAudit, fetchNode, fetchWorkspaces, setActiveWorkspace,
} from './api.js';

// The session cookie is HttpOnly so JS can't inspect it — we rely on
// the first API call to 401 and trigger a redirect via api.js. Brief
// flash of empty UI is acceptable and dwarfed by the network round-trip.

const TWEAK_DEFAULTS = {
  mood: 'cosmic',
  starfield: true,
  autoRotate: false,
  live: true,
  decayCurve: 'ebbinghaus',
  initialLayout: 'umap',
};

function applyDecay(stability, dtDays, curve) {
  if (!stability || stability <= 0) return 0;
  if (curve === 'linear') return Math.max(0, 1 - dtDays / stability);
  if (curve === 'step') return dtDays < stability ? 1 : 0;
  return Math.exp(-dtDays / stability);
}

// Small localStorage-backed useState. `serialize`/`deserialize` let callers
// round-trip non-JSON values (e.g. Sets) transparently. Keys are namespaced
// under "aleph:" to avoid collisions with other apps on the same origin.
function usePersistedState(key, initial, { serialize, deserialize } = {}) {
  const fullKey = `aleph:${key}`;
  const [value, setValue] = useState(() => {
    if (typeof window === 'undefined') return initial;
    try {
      const raw = window.localStorage.getItem(fullKey);
      if (raw == null) return initial;
      const parsed = JSON.parse(raw);
      return deserialize ? deserialize(parsed) : parsed;
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    if (typeof window === 'undefined') return;
    try {
      const out = serialize ? serialize(value) : value;
      window.localStorage.setItem(fullKey, JSON.stringify(out));
    } catch {
      // Quota exceeded or serialization error — drop silently.
    }
  }, [fullKey, value, serialize]);
  return [value, setValue];
}

// filters.kinds is a Set — localStorage is plain JSON so serialize via array.
const FILTERS_DEFAULT = {
  kinds: new Set([
    'doc_chunk', 'interaction', 'insight',
    'image', 'video_scene', 'video_transcript',
    'audio_clip', 'audio_transcript',
    'pdf_page', 'pdf_text',
  ]),
  minScore: 0.05,
};
const filtersSerialize = (f) => ({ kinds: Array.from(f.kinds), minScore: f.minScore });
const filtersDeserialize = (raw) => ({
  kinds: new Set(Array.isArray(raw?.kinds) ? raw.kinds : FILTERS_DEFAULT.kinds),
  minScore: typeof raw?.minScore === 'number' ? raw.minScore : FILTERS_DEFAULT.minScore,
});

export default function App() {
  const nodes = useAlephStore((s) => s.nodes);
  const edges = useAlephStore((s) => s.edges);
  const adjacency = useAlephStore((s) => s.adjacency);
  const idToIdx = useAlephStore((s) => s.idToIdx);
  const setGraph = useAlephStore((s) => s.setGraph);
  const applyPatch = useAlephStore((s) => s.applyPatch);
  const ensureLayout = useAlephStore((s) => s.ensureLayout);
  const reinforceLocal = useAlephStore((s) => s.reinforceLocal);

  const [loadState, setLoadState] = useState('loading'); // loading | ok | auth | error | empty
  const [loadError, setLoadError] = useState(null);

  const [layout, setLayout] = useState(TWEAK_DEFAULTS.initialLayout);
  const [fitViewTrigger, setFitViewTrigger] = useState(0);
  const requestFit = useCallback(() => setFitViewTrigger((n) => n + 1), []);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState(null);
  const [selectedId, setSelectedId] = usePersistedState('selectedId', null);
  const [hoveredId, setHoveredId] = useState(null);
  const [hoverPos, setHoverPos] = useState({ x: 0, y: 0 });
  const [isolated, setIsolated] = useState(false);
  const [timeShift, setTimeShift] = useState(0);
  const [events, setEvents] = usePersistedState('events', []);
  const [tweaksVisible, setTweaksVisible] = useState(false);
  const [tweaks, setTweaks] = usePersistedState('tweaks', TWEAK_DEFAULTS);
  const [filters, setFilters] = usePersistedState(
    'filters', FILTERS_DEFAULT,
    { serialize: filtersSerialize, deserialize: filtersDeserialize },
  );
  const [colorMode, setColorMode] = usePersistedState('colorMode', 'kind');
  const [sizeMode, setSizeMode] = usePersistedState('sizeMode', 'access');
  const [edgeCutoff, setEdgeCutoff] = usePersistedState('edgeCutoff', 0.45);
  const [zoomTarget, setZoomTarget] = useState(null);

  // Workspaces — loaded once at mount, refreshed after switch.
  const [workspaces, setWorkspaces] = useState([]);
  const [activeWorkspace, setActiveWorkspaceName] = useState(null);

  useEffect(() => {
    let cancelled = false;
    fetchWorkspaces()
      .then((r) => {
        if (cancelled) return;
        setWorkspaces(r.workspaces || []);
        setActiveWorkspaceName(r.active || null);
      })
      .catch(() => { /* non-fatal */ });
    return () => { cancelled = true; };
  }, []);

  const onSwitchWorkspace = useCallback(async (name) => {
    if (!name || name === activeWorkspace) return;
    try {
      await setActiveWorkspace(name, false);
      setActiveWorkspaceName(name);
      // Hard reload: graph + stats + stream all have to be rebuilt
      // against the new DB. Simpler (and safer) than wiring every
      // piece of state through a live swap.
      window.location.reload();
    } catch (e) {
      alert('switch failed: ' + (e.message || e));
    }
  }, [activeWorkspace]);

  // Buffered patches to avoid re-rendering on every SSE event
  const pendingPatches = useRef([]);
  const flushTimer = useRef(null);

  // Live toggle is read via ref so the SSE handler (captured at mount time)
  // always sees the current value without having to re-subscribe.
  const liveRef = useRef(true);
  useEffect(() => { liveRef.current = tweaks.live !== false; }, [tweaks.live]);

  const pushEvent = useCallback((evt) => {
    setEvents((prev) => [
      { ...evt, id: Math.random().toString(36).slice(2), ago: 'just now' },
      ...prev,
    ].slice(0, 30));
  }, []);

  // ---- Initial load with retry/backoff ----
  useEffect(() => {
    let cancelled = false;
    let closeStream = null;

    async function load() {
      setLoadState('loading');
      setLoadError(null);
      let attempt = 0;
      while (attempt < 3 && !cancelled) {
        try {
          const data = await fetchGraph();
          if (cancelled) return;
          setGraph(data);
          setLoadState((data.nodes && data.nodes.length > 0) ? 'ok' : 'empty');

          closeStream = openStream((evt) => {
            // Respect the "live updates" tweak: drop incoming events when paused.
            if (!liveRef.current && evt.type !== 'error') return;
            pendingPatches.current.push(evt);
            if (!flushTimer.current) {
              flushTimer.current = setTimeout(() => {
                flushTimer.current = null;
                const batch = pendingPatches.current;
                pendingPatches.current = [];
                for (const p of batch) {
                  if (p.type === 'version_bump') {
                    // Re-fetch the graph when the UMAP snapshot has been rebuilt.
                    fetchGraph().then(setGraph).catch(() => {});
                    pushEvent({ type: 'reinforce', msg: `snapshot v${p.version ?? '?'}` });
                    continue;
                  }
                  if (p.type === 'error') {
                    pushEvent({ type: 'forget', msg: `stream error: ${p.error || 'unknown'}` });
                    continue;
                  }
                  const shortId = (p.id || '').slice(0, 8);
                  if (p.op === 'insert') {
                    // SSE only carries {op, id, kind, source_path}. Fetch the
                    // full node from the backend, then apply. Missing
                    // embedding_3d is resolved by anchoring near the node's
                    // top-k neighbors (which we already have in the store).
                    pushEvent({ type: 'remember', msg: `insert ${p.kind || ''} · ${shortId}` });
                    fetchNode(p.id)
                      .then((detail) => {
                        if (!detail || !detail.node) return;
                        applyPatch({
                          op: 'insert',
                          node: detail.node,
                          neighbors: detail.neighbors || [],
                        });
                      })
                      .catch(() => { /* non-fatal */ });
                  } else if (p.op === 'delete') {
                    applyPatch({ op: 'delete', id: p.id });
                    pushEvent({ type: 'forget', msg: `delete ${p.kind || ''} · ${shortId}` });
                  } else if (p.op === 'update') {
                    fetchNode(p.id).then((detail) => {
                      if (detail && detail.node) {
                        applyPatch({ op: 'update', id: p.id, node: detail.node });
                      }
                    }).catch(() => {});
                    pushEvent({ type: 'reinforce', msg: `update ${p.kind || ''} · ${shortId}` });
                  } else if (p.op === 'reinforce') {
                    applyPatch({ op: 'reinforce', id: p.id });
                    pushEvent({ type: 'reinforce', msg: `reinforce ${p.kind || ''} · ${shortId}` });
                  }
                }
              }, 100); // ~10fps
            }
          });
          return;
        } catch (e) {
          if (cancelled) return;
          if (e.status === 401 || e.status === 403) {
            setLoadState('auth');
            setLoadError(e);
            return;
          }
          attempt++;
          if (attempt >= 3) {
            setLoadState('error');
            setLoadError(e);
            return;
          }
          await new Promise((r) => setTimeout(r, 500 * 2 ** attempt));
        }
      }
    }
    load();
    return () => {
      cancelled = true;
      if (closeStream) closeStream();
      if (flushTimer.current) clearTimeout(flushTimer.current);
    };
  }, [setGraph, applyPatch, pushEvent]);

  // Track "empty" state dynamically
  useEffect(() => {
    if (loadState === 'ok' && nodes.length === 0) setLoadState('empty');
    else if (loadState === 'empty' && nodes.length > 0) setLoadState('ok');
  }, [loadState, nodes.length]);

  // Keyboard shortcuts
  useEffect(() => {
    const onKey = (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      if (e.key === 'q' || e.key === 'Q') {
        e.preventDefault();
        document.getElementById('query-input')?.focus();
      } else if (e.key === 'Escape') {
        setSelectedId(null);
        setResults(null);
        setIsolated(false);
      } else if (e.key === 'i' && selectedId) {
        setIsolated((v) => !v);
      } else if (e.key === 'f' || e.key === 'F') {
        e.preventDefault();
        requestFit();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selectedId, requestFit]);

  // Auto-fit whenever the layout changes.
  useEffect(() => { requestFit(); }, [layout, requestFit]);

  // Auto-fit once the first graph payload is loaded.
  useEffect(() => {
    if (loadState === 'ok' && nodes.length > 0) requestFit();
  }, [loadState, nodes.length === 0, requestFit]);

  useEffect(() => {
    const onMove = (e) => setHoverPos({ x: e.clientX, y: e.clientY });
    window.addEventListener('pointermove', onMove);
    return () => window.removeEventListener('pointermove', onMove);
  }, []);

  // Adjusted nodes with client-side decay
  const adjustedNodes = useMemo(() => {
    const nowShifted = Date.now() - timeShift * 86400 * 1000;
    return nodes.map((n) => {
      const dt = Math.max(0, (nowShifted - (n.lastAccessAt ?? nowShifted)) / 86400 / 1000);
      const decay = n.decay === 0 ? 0 : applyDecay(n.stability ?? 1, dt, tweaks.decayCurve);
      return { ...n, decay };
    });
  }, [nodes, timeShift, tweaks.decayCurve]);

  const positions = useMemo(() => {
    if (nodes.length === 0) return [];
    return ensureLayout(layout);
    // depend on nodes identity so layout recomputes on graph reset
  }, [layout, nodes, ensureLayout]);

  const hiddenIds = useMemo(() => {
    const s = new Set();
    for (const n of adjustedNodes) {
      if (!filters.kinds.has(n.kind)) s.add(n.id);
      else if ((n.decay ?? 0) < filters.minScore) s.add(n.id);
    }
    return s;
  }, [adjustedNodes, filters]);

  const selectedIdx = useMemo(
    () => (selectedId ? idToIdx.get(selectedId) ?? -1 : -1),
    [selectedId, idToIdx],
  );
  const neighbors = useMemo(() => {
    if (selectedIdx < 0) return [];
    const adj = adjacency[selectedIdx] || [];
    return adj.map(({ j, w }) => ({ node: nodes[j], w })).filter((x) => x.node);
  }, [selectedIdx, adjacency, nodes]);

  const { highlightIds, dimmedIds, highlightEdges } = useMemo(() => {
    const hl = new Set();
    const hlE = new Set();
    const dim = new Set();
    if (selectedId && selectedIdx >= 0) {
      hl.add(selectedId);
      const adj = adjacency[selectedIdx] || [];
      for (const { j } of adj) {
        const nb = nodes[j];
        if (nb) hl.add(nb.id);
        hlE.add(`${Math.min(selectedIdx, j)}_${Math.max(selectedIdx, j)}`);
      }
      if (isolated) {
        for (const n of nodes) if (!hl.has(n.id)) dim.add(n.id);
      }
    }
    if (results) for (const id of results.ids) hl.add(id);
    return { highlightIds: hl, dimmedIds: dim, highlightEdges: hlE };
  }, [selectedId, selectedIdx, isolated, results, adjacency, nodes]);

  const onQuery = useCallback(async (q) => {
    if (!q.trim()) { setResults(null); return; }
    try {
      const res = await searchGraph(q);
      const hits = Array.isArray(res) ? res : (res?.results ?? []);
      const ids = new Set(hits.map((h) => h.id));
      const pulses = new Map();
      hits.forEach((h, k) => pulses.set(h.id, k * 0.4));
      setResults({ ids, pulses });
      pushEvent({ type: 'search', msg: `"${q}" → ${hits.length} hits${hits[0] ? ` · top ${(hits[0].score ?? 0).toFixed(2)}` : ''}` });
      if (hits[0]) {
        reinforceLocal(hits[0].id);
        const idx = idToIdx.get(hits[0].id);
        if (idx != null && positions[idx]) {
          const p = positions[idx];
          setZoomTarget({ x: p.x, y: p.y, z: p.z, radius: 60 });
        }
      }
    } catch (e) {
      pushEvent({ type: 'search', msg: `"${q}" → error ${e.status || ''}` });
      setResults({ ids: new Set(), pulses: new Map() });
    }
  }, [idToIdx, positions, pushEvent, reinforceLocal]);

  const onNodeClick = useCallback((id) => {
    setSelectedId(id);
    const idx = idToIdx.get(id);
    if (idx != null && positions[idx]) {
      const p = positions[idx];
      setZoomTarget({ x: p.x, y: p.y, z: p.z, radius: 45 });
    }
  }, [idToIdx, positions]);

  const onNodeHover = useCallback((id) => setHoveredId(id), []);

  const onForget = useCallback(async (id) => {
    // optimistic fade
    applyPatch({ op: 'delete', id });
    pushEvent({ type: 'forget', msg: `${id} · pending…` });
    setSelectedId(null);
    setIsolated(false);
    try {
      await apiForget(id);
    } catch (e) {
      pushEvent({ type: 'forget', msg: `${id} · ERROR ${e.status || ''}` });
    }
  }, [applyPatch, pushEvent]);

  const onRemember = useCallback(async (text, ctx) => {
    const placeholderId = `pending_${Math.random().toString(36).slice(2, 8)}`;
    // Optimistic placeholder node near origin — the SSE insert event
    // will replace it with real data once the server commits.
    applyPatch({
      op: 'insert',
      node: {
        id: placeholderId,
        kind: 'insight',
        content: text,
        source_path: null,
        cluster: 'unclustered',
        accessCount: 1,
        stability: 14,
        last_access_at: new Date().toISOString(),
        created_at: new Date().toISOString(),
        embedding_3d: [0, 0, 0],
        metadata: { tool: null, tags: ['pending'], context: ctx || null },
      },
    });
    pushEvent({ type: 'remember', msg: `pending · ${text.slice(0, 40)}` });
    try {
      const res = await apiRemember(text, ctx || null);
      pushEvent({ type: 'remember', msg: `committed · ${res?.id || '(no id)'}` });
      // remove placeholder; SSE will surface the real node
      applyPatch({ op: 'delete', id: placeholderId });
    } catch (e) {
      applyPatch({ op: 'delete', id: placeholderId });
      pushEvent({ type: 'remember', msg: `ERROR ${e.status || ''}` });
    }
  }, [applyPatch, pushEvent]);

  const stats = useMemo(() => {
    const s = {
      total: 0, doc: 0, interaction: 0, insight: 0,
      image: 0, video_scene: 0, audio_clip: 0, pdf_page: 0,
      video_transcript: 0, audio_transcript: 0, pdf_text: 0,
    };
    for (const n of nodes) {
      if ((n.decay ?? 1) === 0) continue;
      s.total++;
      if (n.kind === 'doc_chunk') s.doc++;
      else if (n.kind === 'interaction') s.interaction++;
      else if (n.kind === 'insight') s.insight++;
      else if (n.kind === 'image') s.image++;
      else if (n.kind === 'video_scene') s.video_scene++;
      else if (n.kind === 'video_transcript') s.video_transcript++;
      else if (n.kind === 'audio_clip') s.audio_clip++;
      else if (n.kind === 'audio_transcript') s.audio_transcript++;
      else if (n.kind === 'pdf_page') s.pdf_page++;
      else if (n.kind === 'pdf_text') s.pdf_text++;
    }
    return s;
  }, [nodes]);

  const hoveredNode = hoveredId ? nodes[idToIdx.get(hoveredId)] : null;
  const selectedNode = selectedId ? nodes[idToIdx.get(selectedId)] : null;

  const sceneHidden = useMemo(() => {
    const s = new Set(hiddenIds);
    for (const n of nodes) if (n.decay === 0) s.add(n.id);
    return s;
  }, [hiddenIds, nodes]);

  const onOpenSettings = useCallback(() => {
    const cur = getWriteKey();
    const next = window.prompt(
      'Aleph write key (X-Aleph-Key). Leave blank to clear.',
      cur,
    );
    if (next === null) return;
    setWriteKey(next.trim());
  }, []);

  const overlay = (() => {
    if (loadState === 'loading') {
      return <div className="overlay-msg"><div className="mono">loading graph…</div></div>;
    }
    if (loadState === 'auth') {
      return (
        <div className="overlay-msg">
          <div className="mono">not authenticated</div>
          <div className="hint">reload this page and enter your basic-auth credentials when the browser prompts</div>
        </div>
      );
    }
    if (loadState === 'error') {
      return (
        <div className="overlay-msg">
          <div className="mono">could not reach /aleph/api/graph</div>
          <div className="hint">{loadError?.message || 'unknown error'}</div>
        </div>
      );
    }
    if (loadState === 'empty') {
      return (
        <div className="overlay-msg">
          <div className="mono">memory is empty</div>
          <div className="hint">bootstrap the indexer, or use remember() below</div>
        </div>
      );
    }
    return null;
  })();

  return (
    <div className={'app mood-' + tweaks.mood}>
      <TopBar
        onQuery={onQuery}
        query={query}
        setQuery={setQuery}
        stats={stats}
        liveEvents={tweaks.live}
        onOpenSettings={onOpenSettings}
        workspaces={workspaces}
        activeWorkspace={activeWorkspace}
        onSwitchWorkspace={onSwitchWorkspace}
      />

      <LeftRail
        layout={layout}
        setLayout={setLayout}
        filters={filters}
        setFilters={setFilters}
        colorMode={colorMode}
        setColorMode={setColorMode}
        sizeMode={sizeMode}
        setSizeMode={setSizeMode}
        edgeCutoff={edgeCutoff}
        setEdgeCutoff={setEdgeCutoff}
      />

      <div className="canvas-wrap">
        {nodes.length > 0 ? (
          <Scene
            nodes={adjustedNodes}
            positions={positions}
            edges={edges}
            colorMode={colorMode}
            sizeMode={sizeMode}
            starfield={tweaks.starfield}
            autoRotate={tweaks.autoRotate}
            hoveredId={hoveredId}
            selectedId={selectedId}
            highlightIds={highlightIds}
            hiddenIds={sceneHidden}
            dimmedIds={dimmedIds}
            highlightEdges={highlightEdges}
            densityCutoff={edgeCutoff}
            pulsePhases={results?.pulses}
            onHover={onNodeHover}
            onClick={onNodeClick}
            zoomTarget={zoomTarget}
            fitViewTrigger={fitViewTrigger}
            onTargetReached={() => setZoomTarget(null)}
            onBgClick={() => { setSelectedId(null); setIsolated(false); }}
          />
        ) : null}
        {overlay}
      </div>

      <RightPanel
        node={selectedNode}
        neighbors={neighbors}
        onClose={() => { setSelectedId(null); setIsolated(false); }}
        onForget={onForget}
        onJump={(id) => onNodeClick(id)}
        onIsolate={() => setIsolated((v) => !v)}
        isolated={isolated}
        fetchAudit={fetchNodeAudit}
      />

      <BottomBar time={timeShift} setTime={setTimeShift} events={events} />

      <RememberBox onRemember={onRemember} />

      <HoverTip node={hoveredNode} x={hoverPos.x} y={hoverPos.y} />

      <TweaksPanel
        tweaks={tweaks}
        setTweaks={setTweaks}
        visible={tweaksVisible}
      />

      <div className="legend mono">
        <div className="legend-row"><span className="lg-solid" /><span>sim ≥ 0.60</span></div>
        <div className="legend-row"><span className="lg-dashed" /><span>sim &lt; 0.60</span></div>
        <div className="legend-keys">
          <kbd>Q</kbd>query <kbd>F</kbd>fit <kbd>Esc</kbd>clear <kbd>I</kbd>isolate · drag rotate · right-drag pan · wheel zoom
        </div>
        <div style={{ marginTop: 6, display: 'flex', gap: 6 }}>
          <button
            className="btn ghost"
            style={{ fontSize: 10, flex: 1 }}
            onClick={() => setTweaksVisible((v) => !v)}
          >tweaks</button>
          <button
            className="btn ghost"
            style={{ fontSize: 10, flex: 1 }}
            onClick={() => { clearAuth(); redirectToLogin(); }}
            title="sign out and return to login"
          >sign out</button>
        </div>
      </div>
    </div>
  );
}
