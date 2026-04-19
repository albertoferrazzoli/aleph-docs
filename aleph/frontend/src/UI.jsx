// UI overlays: top bar, rails, side panel, timeline, tweaks, remember
// composer, hover tip. All positioned absolutely above the canvas.

import { useState, useEffect } from 'react';

function fmtDaysAgo(ts) {
  if (!ts) return '—';
  const d = (Date.now() - ts) / 86400 / 1000;
  if (d < 1) return `${Math.max(0, Math.round(d * 24))}h ago`;
  if (d < 30) return `${Math.round(d)}d ago`;
  return `${Math.round(d / 30)}mo ago`;
}

// Seven memory kinds — keep in sync with mcp/memory/schema.sql and
// aleph/frontend/src/Scene.jsx KIND_COLOR.
export const KIND_SWATCH = {
  doc_chunk:   '#7dd3fc',   // sky blue
  interaction: '#fbbf24',   // amber
  insight:     '#f472b6',   // pink
  image:       '#4ade80',   // green
  video_scene: '#fb7185',   // coral
  audio_clip:  '#a78bfa',   // violet
  pdf_page:    '#f97316',   // orange
};
export const KIND_LABELS = {
  doc_chunk:   'doc chunk',
  interaction: 'interaction',
  insight:     'insight',
  image:       'image',
  video_scene: 'video scene',
  audio_clip:  'audio clip',
  pdf_page:    'pdf page',
};
export const KINDS_ORDER = [
  'doc_chunk', 'interaction', 'insight',
  'image', 'video_scene', 'audio_clip', 'pdf_page',
];
const kindLabel = (k) => KIND_LABELS[k] || k;

export function TopBar({ onQuery, query, setQuery, stats, liveEvents, onOpenSettings }) {
  return (
    <div className="topbar">
      <div className="brand">
        <div className="brand-dot" />
        <div>
          <div className="brand-title">Aleph</div>
          <div className="brand-sub">semantic memory — aleph-docs-mcp MCP</div>
        </div>
      </div>

      <form
        className="query-form"
        onSubmit={(e) => { e.preventDefault(); onQuery(query); }}
      >
        <span className="query-prefix">semantic_search</span>
        <input
          id="query-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="ask anything…"
          spellCheck={false}
        />
        <span className="query-hint">↵ to search · Q to focus</span>
      </form>

      <div className="stats">
        <Stat label="n" value={stats.total} />
        <Stat label="doc" value={stats.doc} dotColor={KIND_SWATCH.doc_chunk} />
        <Stat label="int" value={stats.interaction} dotColor={KIND_SWATCH.interaction} />
        <Stat label="ins" value={stats.insight} dotColor={KIND_SWATCH.insight} />
        <Stat label="img" value={stats.image} dotColor={KIND_SWATCH.image} />
        <Stat label="vid" value={stats.video_scene} dotColor={KIND_SWATCH.video_scene} />
        <Stat label="aud" value={stats.audio_clip} dotColor={KIND_SWATCH.audio_clip} />
        <Stat label="pdf" value={stats.pdf_page} dotColor={KIND_SWATCH.pdf_page} />
        <div className="live-badge">
          <span className={'live-dot ' + (liveEvents ? 'on' : 'off')} />
          <span>stream</span>
        </div>
        {onOpenSettings && (
          <button
            className="btn ghost"
            style={{ marginLeft: 8 }}
            onClick={onOpenSettings}
            title="Write key settings"
          >⚙</button>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, dotColor }) {
  return (
    <div className="stat">
      {dotColor && <span className="stat-dot" style={{ background: dotColor }} />}
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
    </div>
  );
}

export function LeftRail({
  layout, setLayout, filters, setFilters, colorMode, setColorMode,
  sizeMode, setSizeMode, edgeCutoff, setEdgeCutoff,
}) {
  return (
    <div className="left-rail">
      <Section title="layout">
        <div className="segmented">
          {['umap', 'force', 'cluster'].map((m) => (
            <button key={m} className={layout === m ? 'on' : ''} onClick={() => setLayout(m)}>
              {m}
            </button>
          ))}
        </div>
        <div className="hint">
          {layout === 'umap' && 'UMAP projection computed server-side'}
          {layout === 'force' && 'spring layout over top-k edges (client)'}
          {layout === 'cluster' && 'HDBSCAN groups as galaxies (client)'}
        </div>
      </Section>

      <Section title="filter kind">
        {KINDS_ORDER.map((k) => (
          <label key={k} className="checkbox">
            <input
              type="checkbox"
              checked={filters.kinds.has(k)}
              onChange={(e) => {
                const s = new Set(filters.kinds);
                if (e.target.checked) s.add(k); else s.delete(k);
                setFilters({ ...filters, kinds: s });
              }}
            />
            <span className="swatch" style={{ background: KIND_SWATCH[k] }} />
            <span>{kindLabel(k)}</span>
          </label>
        ))}
      </Section>

      <Section title="min decay score">
        <Slider
          min={0} max={1} step={0.01}
          value={filters.minScore}
          onChange={(v) => setFilters({ ...filters, minScore: v })}
          format={(v) => v.toFixed(2)}
        />
        <div className="hint">hides memories whose score × decay is below threshold</div>
      </Section>

      <Section title="color by">
        <div className="segmented">
          {[['kind', 'kind'], ['stability', 'stability'], ['source', 'source']].map(([v, label]) => (
            <button key={v} className={colorMode === v ? 'on' : ''} onClick={() => setColorMode(v)}>{label}</button>
          ))}
        </div>
      </Section>

      <Section title="size by">
        <div className="segmented">
          {[['access', 'access'], ['stability', 'stab'], ['decay', 'decay']].map(([v, label]) => (
            <button key={v} className={sizeMode === v ? 'on' : ''} onClick={() => setSizeMode(v)}>{label}</button>
          ))}
        </div>
      </Section>

      <Section title="edge weight cutoff">
        <Slider
          min={0.35} max={0.95} step={0.01}
          value={edgeCutoff}
          onChange={setEdgeCutoff}
          format={(v) => v.toFixed(2)}
        />
        <div className="hint">solid ≥ 0.60 · dashed &lt; 0.60</div>
      </Section>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="section">
      <div className="section-title">{title}</div>
      {children}
    </div>
  );
}

function Slider({ min, max, step, value, onChange, format }) {
  return (
    <div className="slider">
      <input
        type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
      />
      <span className="slider-val">{format ? format(value) : value}</span>
    </div>
  );
}

const OP_COLOR = {
  insert: '#50fa7b',
  update: '#7dd3fc',
  delete: '#f87171',
  reinforce: '#fbbf24',
  access: '#94a3b8',
};

function AuditHistory({ nodeId, fetchAudit }) {
  const [events, setEvents] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    let alive = true;
    setEvents(null); setErr(null);
    if (!nodeId || !fetchAudit) return;
    fetchAudit(nodeId, 20)
      .then((d) => { if (alive) setEvents(d?.events || []); })
      .catch((e) => { if (alive) setErr(e?.message || 'error'); });
    return () => { alive = false; };
  }, [nodeId, fetchAudit]);

  if (err) return <div className="rp-empty">audit: {err}</div>;
  if (events === null) return <div className="rp-empty">loading audit…</div>;
  if (events.length === 0) return <div className="rp-empty">no audit events</div>;
  return (
    <div className="rp-audit">
      {events.map((e) => (
        <div key={e.id} className="rp-audit-row">
          <span className="rp-audit-op mono" style={{ color: OP_COLOR[e.op] || '#94a3b8', borderColor: OP_COLOR[e.op] || '#94a3b8' }}>{e.op}</span>
          <span className="rp-audit-ts mono">{fmtDaysAgo(e.ts_ms ?? Date.parse(e.ts))}</span>
          <span className="rp-audit-actor mono">{e.actor || '—'}</span>
        </div>
      ))}
    </div>
  );
}

function MediaRenderer({ node }) {
  const mt = node.media_type || '';
  if (!node.media_ref) return <div className="rp-content">{node.content}</div>;
  const src = `/aleph/api/media/${node.id}`;
  if (mt.startsWith('image/')) {
    return <img className="rp-media" src={src} alt={node.content} loading="lazy" />;
  }
  if (mt.startsWith('video/')) {
    const t = /#t=([\d.]+)/.exec(node.media_ref)?.[1];
    return (
      <video className="rp-media" controls preload="metadata"
             src={`${src}${t ? `#t=${t}` : ''}`} />
    );
  }
  if (mt.startsWith('audio/')) {
    const m = /#t=([\d.]+),([\d.]+)/.exec(node.media_ref);
    return (
      <div className="rp-audio">
        <audio className="rp-media" controls preload="metadata" src={src} />
        {m && (
          <div className="mono" style={{ fontSize: 10, opacity: 0.6 }}>
            {m[1]}s – {m[2]}s
          </div>
        )}
      </div>
    );
  }
  if (mt === 'application/pdf') {
    const page = /#page=(\d+)/.exec(node.media_ref)?.[1];
    return (
      <div className="rp-pdf">
        {node.preview_b64 && (
          <img
            src={`data:image/jpeg;base64,${node.preview_b64}`}
            alt="page preview"
            style={{ maxWidth: '100%' }}
          />
        )}
        <a
          className="btn"
          href={`${src}${page ? `#page=${page}` : ''}`}
          target="_blank"
          rel="noreferrer"
        >open pdf{page ? ` · page ${page}` : ''}</a>
      </div>
    );
  }
  return <div className="rp-content">{node.content}</div>;
}

export function RightPanel({ node, neighbors, onClose, onForget, onJump, onIsolate, isolated, fetchAudit }) {
  if (!node) return null;
  const kindColor = KIND_SWATCH[node.kind] || '#94a3b8';
  return (
    <div className="right-panel">
      <div className="rp-header">
        <div className="rp-kind" style={{ color: kindColor, borderColor: kindColor }}>{kindLabel(node.kind)}</div>
        <button className="rp-close" onClick={onClose}>×</button>
      </div>
      <div className="rp-id mono">{node.id}</div>
      <MediaRenderer node={node} />
      {node.media_ref && node.content && (
        <div className="rp-content" style={{ opacity: 0.7 }}>{node.content}</div>
      )}

      {node.source_path && (
        <div className="rp-meta-row mono">
          <span className="rp-meta-k">source_path</span>
          <span className="rp-meta-v">{node.source_path}{node.source_section || ''}</span>
        </div>
      )}
      {node.metadata?.tool && (
        <div className="rp-meta-row mono">
          <span className="rp-meta-k">tool</span>
          <span className="rp-meta-v">{node.metadata.tool}</span>
        </div>
      )}

      <div className="rp-grid">
        <div className="rp-cell">
          <div className="rp-cell-k">stability</div>
          <div className="rp-cell-v">{(node.stability ?? 0).toFixed(1)}<span className="rp-unit">d</span></div>
          <div className="rp-bar"><div style={{ width: `${Math.min(100, (node.stability ?? 0) / 365 * 100)}%`, background: kindColor }} /></div>
        </div>
        <div className="rp-cell">
          <div className="rp-cell-k">access_count</div>
          <div className="rp-cell-v">{node.accessCount ?? 0}</div>
        </div>
        <div className="rp-cell">
          <div className="rp-cell-k">decay score</div>
          <div className="rp-cell-v">{(node.decay ?? 0).toFixed(3)}</div>
          <div className="rp-bar"><div style={{ width: `${(node.decay ?? 0) * 100}%`, background: kindColor }} /></div>
        </div>
        <div className="rp-cell">
          <div className="rp-cell-k">last_access</div>
          <div className="rp-cell-v small">{fmtDaysAgo(node.lastAccessAt)}</div>
        </div>
      </div>

      <div className="rp-section">top-k neighbors</div>
      <div className="rp-neighbors">
        {neighbors.slice(0, 8).map((nb) => {
          const c = KIND_SWATCH[nb.node.kind] || '#94a3b8';
          return (
            <div key={nb.node.id} className="rp-nb" onClick={() => onJump(nb.node.id)}>
              <div className="rp-nb-bar">
                <div className="rp-nb-bar-fill" style={{ width: `${nb.w * 100}%`, background: c, opacity: nb.w >= 0.6 ? 1 : 0.6, borderStyle: nb.w >= 0.6 ? 'solid' : 'dashed' }} />
              </div>
              <span className="rp-nb-score mono">{nb.w.toFixed(2)}</span>
              <span className="rp-nb-txt">{nb.node.content}</span>
            </div>
          );
        })}
        {neighbors.length === 0 && <div className="rp-empty">no neighbors above cutoff</div>}
      </div>

      <div className="rp-section">audit history</div>
      <AuditHistory nodeId={node.id} fetchAudit={fetchAudit} />

      <div className="rp-actions">
        <button className="btn" onClick={onIsolate}>{isolated ? 'exit isolate' : 'isolate neighborhood'}</button>
        <button className="btn danger" onClick={() => onForget(node.id)}>forget()</button>
      </div>
    </div>
  );
}

export function BottomBar({ time, setTime, events }) {
  const MAX_BACK = 120;
  return (
    <div className="bottom-bar">
      <div className="timeline">
        <div className="timeline-label mono">
          <span className="tl-k">time shift</span>
          <span className="tl-v">{time === 0 ? 'now' : `−${time.toFixed(0)}d`}</span>
        </div>
        <input
          type="range"
          min={-MAX_BACK} max={0} step={1}
          value={-time}
          onChange={(e) => setTime(-parseFloat(e.target.value))}
          className="timeline-range"
        />
        <div className="timeline-ticks">
          <span>−120d</span><span>−90d</span><span>−60d</span><span>−30d</span><span>now</span>
        </div>
      </div>
      <div className="event-log mono">
        {events.slice(0, 5).map((e, i) => (
          <div key={e.id + '-' + i} className={`evt evt-${e.type}`}>
            <span className="evt-t">{e.ago}</span>
            <span className="evt-tag">{e.type}</span>
            <span className="evt-msg">{e.msg}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function RememberBox({ onRemember, disabled }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [ctx, setCtx] = useState('');
  return (
    <div className={'remember-box' + (open ? ' open' : '')}>
      {!open ? (
        <button className="btn remember-trigger" onClick={() => setOpen(true)}>
          <span className="plus">+</span> remember()
        </button>
      ) : (
        <div className="remember-form">
          <div className="rm-title mono">remember(content, context?)</div>
          <textarea
            value={text} onChange={(e) => setText(e.target.value)}
            placeholder="e.g. Cliente X: crash risolto con --no-strong-name + re-sign manuale"
            rows={3}
          />
          <input
            value={ctx} onChange={(e) => setCtx(e.target.value)}
            placeholder="context (optional)"
          />
          <div className="rm-actions">
            <button className="btn ghost" onClick={() => { setOpen(false); setText(''); setCtx(''); }}>cancel</button>
            <button
              className="btn primary"
              disabled={disabled || !text.trim()}
              onClick={() => {
                onRemember(text.trim(), ctx.trim());
                setText(''); setCtx(''); setOpen(false);
              }}
            >commit insight</button>
          </div>
        </div>
      )}
    </div>
  );
}

export function HoverTip({ node, x, y }) {
  if (!node) return null;
  const c = KIND_SWATCH[node.kind] || '#94a3b8';
  return (
    <div className="hover-tip" style={{ left: x + 14, top: y + 14 }}>
      <div className="ht-head">
        <span className="ht-kind" style={{ color: c, borderColor: c }}>{kindLabel(node.kind)}</span>
        <span className="mono ht-id">{node.id}</span>
      </div>
      <div className="ht-content">{node.content}</div>
      <div className="ht-meta mono">
        <span>stab {(node.stability ?? 0).toFixed(0)}d</span>
        <span>·</span>
        <span>hits {node.accessCount ?? 0}</span>
        <span>·</span>
        <span>decay {(node.decay ?? 0).toFixed(2)}</span>
      </div>
    </div>
  );
}

export function TweaksPanel({ tweaks, setTweaks, visible }) {
  if (!visible) return null;
  return (
    <div className="tweaks-panel">
      <div className="tp-title">Tweaks</div>
      <label className="tp-row">
        <span>mood</span>
        <select value={tweaks.mood} onChange={(e) => setTweaks({ ...tweaks, mood: e.target.value })}>
          <option value="cosmic">dark cosmic</option>
          <option value="minimal">technical minimal</option>
        </select>
      </label>
      <label className="tp-row">
        <span>starfield</span>
        <input type="checkbox" checked={tweaks.starfield} onChange={(e) => setTweaks({ ...tweaks, starfield: e.target.checked })} />
      </label>
      <label className="tp-row">
        <span>auto-rotate</span>
        <input type="checkbox" checked={tweaks.autoRotate} onChange={(e) => setTweaks({ ...tweaks, autoRotate: e.target.checked })} />
      </label>
      <label className="tp-row">
        <span>live updates</span>
        <input type="checkbox" checked={tweaks.live} onChange={(e) => setTweaks({ ...tweaks, live: e.target.checked })} />
      </label>
      <label className="tp-row">
        <span>decay curve</span>
        <select value={tweaks.decayCurve} onChange={(e) => setTweaks({ ...tweaks, decayCurve: e.target.value })}>
          <option value="ebbinghaus">Ebbinghaus (exp)</option>
          <option value="linear">linear</option>
          <option value="step">step</option>
        </select>
      </label>
    </div>
  );
}
