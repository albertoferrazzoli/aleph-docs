import { create } from 'zustand';
import { computeLayout } from './clientLayouts.js';

// Server node shape → internal:
//  - last_access_at (ISO) → lastAccessAt (epoch ms)
//  - created_at    (ISO) → createdAt (epoch ms)
//  - access_count        → accessCount
//  - stability           → stability (days)
//  - cluster / content / kind / metadata / source_path passthrough
//  - embedding_3d preserved
function normalizeNode(raw) {
  if (!raw) return raw;
  const lastMs = raw.lastAccessAt ?? parseMaybe(raw.last_access_at);
  const createdMs = raw.createdAt ?? parseMaybe(raw.created_at);
  const n = {
    id: raw.id,
    kind: raw.kind,
    content: raw.content ?? '',
    source_path: raw.source_path ?? null,
    source_section: raw.source_section ?? null,
    cluster: raw.cluster ?? 'unclustered',
    metadata: raw.metadata ?? {},
    embedding_3d: raw.embedding_3d ?? null,
    accessCount: raw.accessCount ?? raw.access_count ?? 0,
    stability: raw.stability ?? 1,
    lastAccessAt: lastMs ?? Date.now(),
    createdAt: createdMs ?? Date.now(),
    // decay is computed client-side at render time via applyDecay.
    decay: 1,
  };
  return n;
}

function parseMaybe(v) {
  if (v == null) return null;
  if (typeof v === 'number') return v;
  const t = Date.parse(v);
  return Number.isNaN(t) ? null : t;
}

function buildIndex(nodes) {
  const m = new Map();
  for (let i = 0; i < nodes.length; i++) m.set(nodes[i].id, i);
  return m;
}

function buildAdj(nodes, edges) {
  const a = Array.from({ length: nodes.length }, () => []);
  for (const e of edges) {
    if (e.a == null || e.b == null) continue;
    if (e.a >= nodes.length || e.b >= nodes.length) continue;
    a[e.a].push({ j: e.b, w: e.w });
    a[e.b].push({ j: e.a, w: e.w });
  }
  for (const lst of a) lst.sort((x, y) => y.w - x.w);
  return a;
}

// Edges in the server payload might use node IDs or indices. Normalize
// to integer indices referencing the `nodes` array.
function normalizeEdges(rawEdges, idToIdx) {
  const out = [];
  for (const e of rawEdges || []) {
    let a = e.a;
    let b = e.b;
    if (typeof a === 'string') a = idToIdx.get(a);
    if (typeof b === 'string') b = idToIdx.get(b);
    if (a == null || b == null) continue;
    out.push({ a, b, w: typeof e.w === 'number' ? e.w : 0.5 });
  }
  return out;
}

export const useAlephStore = create((set, get) => ({
  version: null,
  nodes: [],
  edges: [],
  idToIdx: new Map(),
  adjacency: [],
  // Layout cache by name. Rebuilt when setGraph is called.
  layouts: { umap: [], cluster: [], force: [] },
  layoutDirty: { umap: true, cluster: true, force: true },

  setGraph: (data) => {
    const rawNodes = data?.nodes ?? [];
    let nodes = rawNodes.map(normalizeNode);
    const idToIdx = buildIndex(nodes);
    let edges = normalizeEdges(data?.edges ?? [], idToIdx);

    // Merge `pending` memories (created after the snapshot was built).
    // Each has `anchor_ids`: the top-3 snapshot nodes nearest by cosine.
    // Position them at the centroid of those anchors' embedding_3d +
    // a small jitter so coincident inserts don't overlap.
    const pending = Array.isArray(data?.pending) ? data.pending : [];
    if (pending.length > 0) {
      for (const raw of pending) {
        const n = normalizeNode(raw);
        const anchors = [];
        for (const aid of raw.anchor_ids || []) {
          const j = idToIdx.get(aid);
          if (j == null) continue;
          const e = nodes[j]?.embedding_3d;
          if (Array.isArray(e) && e.length >= 3) anchors.push(e);
        }
        if (anchors.length > 0) {
          const s = anchors.reduce((a, b) => [a[0]+b[0], a[1]+b[1], a[2]+b[2]], [0,0,0]);
          n.embedding_3d = [
            s[0]/anchors.length + (Math.random()-0.5)*6,
            s[1]/anchors.length + (Math.random()-0.5)*6,
            s[2]/anchors.length + (Math.random()-0.5)*6,
          ];
          if (n.cluster == null && raw.anchor_ids?.[0]) {
            n.cluster = nodes[idToIdx.get(raw.anchor_ids[0])]?.cluster ?? null;
          }
        }
        idToIdx.set(n.id, nodes.length);
        nodes = nodes.concat([n]);
      }
    }

    const adjacency = buildAdj(nodes, edges);
    set({
      version: data?.version ?? null,
      nodes,
      edges,
      idToIdx,
      adjacency,
      layouts: { umap: [], cluster: [], force: [] },
      layoutDirty: { umap: true, cluster: true, force: true },
    });
  },

  ensureLayout: (name) => {
    const { layoutDirty, layouts, nodes, edges } = get();
    if (!layoutDirty[name] && layouts[name].length === nodes.length) {
      return layouts[name];
    }
    const positions = computeLayout(name, nodes, edges);
    set({
      layouts: { ...layouts, [name]: positions },
      layoutDirty: { ...layoutDirty, [name]: false },
    });
    return positions;
  },

  applyPatch: (evt) => {
    if (!evt || !evt.op) return;
    const state = get();
    const { nodes, edges, idToIdx } = state;

    if (evt.op === 'insert' && evt.node) {
      const idx = idToIdx.get(evt.node.id);
      const n = normalizeNode(evt.node);
      // If no embedding_3d (new node not yet in the UMAP snapshot), anchor
      // near the centroid of the top-3 neighbors we already have positioned,
      // plus a small random jitter so multiple fresh inserts don't overlap.
      if (!n.embedding_3d && Array.isArray(evt.neighbors) && evt.neighbors.length > 0) {
        const anchors = [];
        for (const nb of evt.neighbors.slice(0, 3)) {
          const j = idToIdx.get(nb.id);
          if (j == null) continue;
          const e = nodes[j]?.embedding_3d;
          if (Array.isArray(e) && e.length >= 3) anchors.push(e);
        }
        if (anchors.length > 0) {
          const sum = anchors.reduce((a, b) => [a[0]+b[0], a[1]+b[1], a[2]+b[2]], [0,0,0]);
          const cx = sum[0] / anchors.length;
          const cy = sum[1] / anchors.length;
          const cz = sum[2] / anchors.length;
          n.embedding_3d = [
            cx + (Math.random() - 0.5) * 6,
            cy + (Math.random() - 0.5) * 6,
            cz + (Math.random() - 0.5) * 6,
          ];
          if (n.cluster == null) n.cluster = nodes[idToIdx.get(evt.neighbors[0].id) ?? 0]?.cluster ?? null;
        }
      }
      const newNodes = nodes.slice();
      const newIdToIdx = new Map(idToIdx);
      if (idx != null) {
        newNodes[idx] = n;
      } else {
        newIdToIdx.set(n.id, newNodes.length);
        newNodes.push(n);
      }
      set({
        nodes: newNodes,
        idToIdx: newIdToIdx,
        adjacency: buildAdj(newNodes, edges),
        layoutDirty: { umap: true, cluster: true, force: true },
      });
      return;
    }

    if (evt.op === 'update' && evt.id) {
      const i = idToIdx.get(evt.id);
      if (i == null) return;
      const patch = evt.node ? normalizeNode(evt.node) : (evt.patch ?? {});
      const newNodes = nodes.slice();
      newNodes[i] = { ...newNodes[i], ...patch, id: nodes[i].id };
      set({ nodes: newNodes });
      return;
    }

    if (evt.op === 'reinforce' && evt.id) {
      const i = idToIdx.get(evt.id);
      if (i == null) return;
      const cur = nodes[i];
      const newNodes = nodes.slice();
      newNodes[i] = {
        ...cur,
        accessCount: (cur.accessCount ?? 0) + 1,
        lastAccessAt: Date.now(),
        stability: Math.min(365, (cur.stability ?? 1) * 1.3),
      };
      set({ nodes: newNodes });
      return;
    }

    if (evt.op === 'delete' && evt.id) {
      const i = idToIdx.get(evt.id);
      if (i == null) return;
      // Soft delete: mark decay=0, stability≈0, keep index stable so
      // edges don't need reindexing.
      const newNodes = nodes.slice();
      newNodes[i] = { ...newNodes[i], decay: 0, stability: 0.01 };
      set({ nodes: newNodes });
      return;
    }
  },

  reinforceLocal: (id) => {
    const { nodes, idToIdx } = get();
    const i = idToIdx.get(id);
    if (i == null) return;
    const cur = nodes[i];
    const newNodes = nodes.slice();
    newNodes[i] = {
      ...cur,
      accessCount: (cur.accessCount ?? 0) + 1,
      lastAccessAt: Date.now(),
      stability: Math.min(365, (cur.stability ?? 1) * 1.7),
    };
    set({ nodes: newNodes });
  },
}));
