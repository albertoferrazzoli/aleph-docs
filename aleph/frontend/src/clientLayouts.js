// Client-side 3D layouts. Backend provides UMAP xyz embedded into each
// node as `embedding_3d`; we compute `cluster` and `force` on demand.

function seeded(seed) {
  return function () {
    let t = (seed += 0x6d2b79f5);
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function layoutUMAP(nodes) {
  // Server already provides 3D coords. Fall back to origin if missing.
  return nodes.map((n) => {
    const e = n.embedding_3d;
    if (Array.isArray(e) && e.length >= 3) {
      return { x: e[0], y: e[1], z: e[2] };
    }
    return { x: 0, y: 0, z: 0 };
  });
}

export function layoutCluster(nodes) {
  const clusterIds = Array.from(new Set(nodes.map((n) => n.cluster ?? 'unclustered')));
  const centers = {};
  const phi = Math.PI * (3 - Math.sqrt(5));
  const R = 70;
  clusterIds.forEach((cid, i) => {
    const y = 1 - (i / (clusterIds.length - 1 || 1)) * 2;
    const radius = Math.sqrt(1 - y * y);
    const theta = phi * i;
    centers[cid] = [Math.cos(theta) * radius * R, y * R, Math.sin(theta) * radius * R];
  });
  const rng = seeded(11);
  return nodes.map((n) => {
    const [cx, cy, cz] = centers[n.cluster ?? 'unclustered'];
    const r = 18 * Math.pow(rng(), 0.7);
    const th = rng() * Math.PI * 2;
    const ph = Math.acos(2 * rng() - 1);
    return {
      x: cx + r * Math.sin(ph) * Math.cos(th),
      y: cy + r * Math.sin(ph) * Math.sin(th),
      z: cz + r * Math.cos(ph),
    };
  });
}

export function layoutForce(nodes, edges, iterations = 140) {
  const rng = seeded(23);
  const N = nodes.length;
  if (N === 0) return [];
  const pos = nodes.map(() => ({
    x: (rng() - 0.5) * 80,
    y: (rng() - 0.5) * 80,
    z: (rng() - 0.5) * 80,
  }));

  const REPULSE = 120;
  const SPRING = 0.02;
  const DAMP = 0.85;
  const vel = pos.map(() => ({ x: 0, y: 0, z: 0 }));

  const neighbors = Array.from({ length: N }, () => []);
  for (const e of edges) {
    if (e.a == null || e.b == null) continue;
    if (e.a >= N || e.b >= N) continue;
    neighbors[e.a].push([e.b, e.w]);
    neighbors[e.b].push([e.a, e.w]);
  }

  const CELL = 20;
  for (let iter = 0; iter < iterations; iter++) {
    const grid = new Map();
    for (let i = 0; i < N; i++) {
      const key = `${Math.floor(pos[i].x / CELL)}|${Math.floor(pos[i].y / CELL)}|${Math.floor(pos[i].z / CELL)}`;
      if (!grid.has(key)) grid.set(key, []);
      grid.get(key).push(i);
    }
    for (let i = 0; i < N; i++) {
      let fx = 0, fy = 0, fz = 0;
      const cx = Math.floor(pos[i].x / CELL);
      const cy = Math.floor(pos[i].y / CELL);
      const cz = Math.floor(pos[i].z / CELL);
      for (let dx = -1; dx <= 1; dx++)
        for (let dy = -1; dy <= 1; dy++)
          for (let dz = -1; dz <= 1; dz++) {
            const bucket = grid.get(`${cx + dx}|${cy + dy}|${cz + dz}`);
            if (!bucket) continue;
            for (const j of bucket) {
              if (i === j) continue;
              const ddx = pos[i].x - pos[j].x;
              const ddy = pos[i].y - pos[j].y;
              const ddz = pos[i].z - pos[j].z;
              const d2 = ddx * ddx + ddy * ddy + ddz * ddz + 0.01;
              const f = REPULSE / d2;
              fx += ddx * f; fy += ddy * f; fz += ddz * f;
            }
          }
      for (const [j, w] of neighbors[i]) {
        const ddx = pos[j].x - pos[i].x;
        const ddy = pos[j].y - pos[i].y;
        const ddz = pos[j].z - pos[i].z;
        const k = SPRING * w;
        fx += ddx * k; fy += ddy * k; fz += ddz * k;
      }
      fx += -pos[i].x * 0.0008;
      fy += -pos[i].y * 0.0008;
      fz += -pos[i].z * 0.0008;
      vel[i].x = (vel[i].x + fx * 0.1) * DAMP;
      vel[i].y = (vel[i].y + fy * 0.1) * DAMP;
      vel[i].z = (vel[i].z + fz * 0.1) * DAMP;
    }
    for (let i = 0; i < N; i++) {
      pos[i].x += vel[i].x;
      pos[i].y += vel[i].y;
      pos[i].z += vel[i].z;
    }
  }
  return pos;
}

export function computeLayout(kind, nodes, edges) {
  if (kind === 'cluster') return layoutCluster(nodes);
  if (kind === 'force') return layoutForce(nodes, edges);
  return layoutUMAP(nodes);
}
