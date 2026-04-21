// 3D scene — plain Three.js (no r3f). Mounts into a div via React
// effect. Handles nodes, halos, edges (solid + dashed), starfield,
// orbit camera, hover/pick, pulse animations.

import * as THREE from 'three';
import { useRef, useEffect } from 'react';

const KIND_COLOR = {
  doc_chunk:        new THREE.Color('#7dd3fc'),  // sky blue
  interaction:      new THREE.Color('#fbbf24'),  // amber
  insight:          new THREE.Color('#f472b6'),  // pink
  image:            new THREE.Color('#4ade80'),  // green
  video_scene:      new THREE.Color('#fb7185'),  // coral
  audio_clip:       new THREE.Color('#a78bfa'),  // violet
  pdf_page:         new THREE.Color('#f97316'),  // orange
  video_transcript: new THREE.Color('#fca5a5'),  // lighter coral — paired with video_scene
  audio_transcript: new THREE.Color('#c4b5fd'),  // lighter violet — paired with audio_clip
};
const DEFAULT_KIND = new THREE.Color('#94a3b8');

// Stable hue per cluster label (hash-based so any string works).
const CLUSTER_HUES = new Map();
function hueFor(cluster) {
  if (!cluster) return 200;
  if (CLUSTER_HUES.has(cluster)) return CLUSTER_HUES.get(cluster);
  let h = 0;
  for (let i = 0; i < cluster.length; i++) {
    h = (h * 31 + cluster.charCodeAt(i)) >>> 0;
  }
  const hue = h % 360;
  CLUSTER_HUES.set(cluster, hue);
  return hue;
}

function colorFor(node, mode) {
  if (mode === 'stability') {
    const t = Math.min(1, (node.stability ?? 1) / 120);
    return new THREE.Color().setHSL(0.58 - t * 0.52, 0.75, 0.62);
  }
  if (mode === 'source') {
    const h = hueFor(node.cluster) / 360;
    return new THREE.Color().setHSL(h, 0.6, 0.65);
  }
  return KIND_COLOR[node.kind] || DEFAULT_KIND;
}

function sizeFor(node, mode) {
  if (mode === 'access') return 0.4 + Math.log((node.accessCount ?? 0) + 1) * 0.45;
  if (mode === 'stability') return 0.35 + Math.min((node.stability ?? 0) / 120, 1) * 1.4;
  if (mode === 'decay') return 0.3 + (node.decay ?? 0) * 1.5;
  return 0.6;
}

export default function Scene(props) {
  const containerRef = useRef(null);
  const stateRef = useRef({});

  // One-time scene setup
  useEffect(() => {
    const container = containerRef.current;
    const W = container.clientWidth || 1;
    const H = container.clientHeight || 1;

    const scene = new THREE.Scene();
    // Transparent scene → CSS on .canvas-wrap paints the background, so the
    // `mood-*` class on .app actually controls the scene look.
    scene.background = null;
    scene.fog = new THREE.Fog(0x06070c, 200, 600);

    const camera = new THREE.PerspectiveCamera(50, W / H, 0.1, 2000);
    camera.position.set(140, 80, 140);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setClearColor(0x000000, 0);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(W, H);
    container.appendChild(renderer.domElement);

    // Starfield
    const starPos = new Float32Array(2000 * 3);
    for (let i = 0; i < 2000; i++) {
      const r = 400 + Math.random() * 600;
      const th = Math.random() * Math.PI * 2;
      const ph = Math.acos(2 * Math.random() - 1);
      starPos[i * 3] = r * Math.sin(ph) * Math.cos(th);
      starPos[i * 3 + 1] = r * Math.sin(ph) * Math.sin(th);
      starPos[i * 3 + 2] = r * Math.cos(ph);
    }
    const starGeo = new THREE.BufferGeometry();
    starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
    const starMat = new THREE.PointsMaterial({
      size: 0.9, color: '#cbd5e1', transparent: true, opacity: 0.55, sizeAttenuation: true,
    });
    const stars = new THREE.Points(starGeo, starMat);
    scene.add(stars);

    const MAX = 4000;
    const nodeGeo = new THREE.SphereGeometry(1, 14, 14);
    const nodeMat = new THREE.MeshBasicMaterial({ toneMapped: false });
    const nodesMesh = new THREE.InstancedMesh(nodeGeo, nodeMat, MAX);
    nodesMesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(MAX * 3), 3);
    nodesMesh.count = 0;
    scene.add(nodesMesh);

    const haloGeo = new THREE.SphereGeometry(1, 10, 10);
    const haloMat = new THREE.MeshBasicMaterial({
      transparent: true, opacity: 0.11, blending: THREE.AdditiveBlending,
      depthWrite: false, toneMapped: false,
    });
    const halosMesh = new THREE.InstancedMesh(haloGeo, haloMat, MAX);
    halosMesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(MAX * 3), 3);
    halosMesh.count = 0;
    scene.add(halosMesh);

    const solidGeo = new THREE.BufferGeometry();
    const solidMat = new THREE.LineBasicMaterial({
      vertexColors: true, transparent: true, opacity: 0.6,
      blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false,
    });
    const solidLines = new THREE.LineSegments(solidGeo, solidMat);
    scene.add(solidLines);

    const dashedGeo = new THREE.BufferGeometry();
    const dashedMat = new THREE.LineDashedMaterial({
      vertexColors: true, dashSize: 0.9, gapSize: 1.2,
      transparent: true, opacity: 0.45, blending: THREE.AdditiveBlending,
      depthWrite: false, toneMapped: false,
    });
    const dashedLines = new THREE.LineSegments(dashedGeo, dashedMat);
    scene.add(dashedLines);

    const cam = {
      theta: 0.6, phi: 1.1, radius: 200,
      target: new THREE.Vector3(0, 0, 0),
      isDrag: false, isPan: false,
      lastX: 0, lastY: 0,
      zoomTarget: null,
    };
    function updateCamera() {
      const x = cam.target.x + cam.radius * Math.sin(cam.phi) * Math.cos(cam.theta);
      const y = cam.target.y + cam.radius * Math.cos(cam.phi);
      const z = cam.target.z + cam.radius * Math.sin(cam.phi) * Math.sin(cam.theta);
      camera.position.set(x, y, z);
      camera.lookAt(cam.target);
    }

    const dom = renderer.domElement;
    const raycaster = new THREE.Raycaster();
    const mouseNDC = new THREE.Vector2();

    function onDown(e) {
      if (e.button === 0) cam.isDrag = true;
      else if (e.button === 2) cam.isPan = true;
      cam.lastX = e.clientX; cam.lastY = e.clientY;
      cam.moved = 0;
    }
    function onUp(e) {
      if (cam.isDrag && cam.moved < 4) {
        const rect = dom.getBoundingClientRect();
        mouseNDC.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
        mouseNDC.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
        raycaster.setFromCamera(mouseNDC, camera);
        raycaster.params.Points = { threshold: 1 };
        const hits = raycaster.intersectObject(nodesMesh);
        const cur = stateRef.current.propsRef.current;
        if (hits.length > 0) {
          const idx = hits[0].instanceId;
          const node = cur.nodes[idx];
          if (node) cur.onClick(node.id);
        } else {
          cur.onBgClick && cur.onBgClick();
        }
      }
      cam.isDrag = false; cam.isPan = false;
    }
    function onMove(e) {
      const dx = e.clientX - cam.lastX;
      const dy = e.clientY - cam.lastY;
      cam.moved += Math.abs(dx) + Math.abs(dy);
      if (cam.isDrag) {
        cam.theta -= dx * 0.005;
        cam.phi = Math.max(0.1, Math.min(Math.PI - 0.1, cam.phi - dy * 0.005));
      } else if (cam.isPan) {
        const panScale = cam.radius * 0.002;
        const forward = new THREE.Vector3().subVectors(cam.target, camera.position).normalize();
        const right = new THREE.Vector3().crossVectors(forward, camera.up).normalize();
        const up = new THREE.Vector3().crossVectors(right, forward).normalize();
        cam.target.addScaledVector(right, -dx * panScale);
        cam.target.addScaledVector(up, dy * panScale);
      } else {
        const rect = dom.getBoundingClientRect();
        mouseNDC.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
        mouseNDC.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
        raycaster.setFromCamera(mouseNDC, camera);
        const hits = raycaster.intersectObject(nodesMesh);
        const cur = stateRef.current.propsRef.current;
        const id = hits.length > 0 ? cur.nodes[hits[0].instanceId]?.id : null;
        if (id !== stateRef.current.hoveredId) {
          stateRef.current.hoveredId = id;
          cur.onHover(id);
        }
      }
      cam.lastX = e.clientX; cam.lastY = e.clientY;
    }
    function onWheel(e) {
      e.preventDefault();
      cam.radius = Math.max(10, Math.min(500, cam.radius * (1 + e.deltaY * 0.0012)));
    }
    function onContext(e) { e.preventDefault(); }

    dom.addEventListener('pointerdown', onDown);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointermove', onMove);
    dom.addEventListener('wheel', onWheel, { passive: false });
    dom.addEventListener('contextmenu', onContext);

    function onResize() {
      const w = container.clientWidth, h = container.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    }
    window.addEventListener('resize', onResize);

    const dummy = new THREE.Object3D();
    const tmpColor = new THREE.Color();
    let raf = 0;
    const t0 = performance.now();

    function animate() {
      raf = requestAnimationFrame(animate);
      const st = stateRef.current;
      if (!st || !st.propsRef || !st.propsRef.current) return;

      const t = (performance.now() - t0) / 1000;
      const props = st.propsRef.current;
      const {
        nodes, positions, colorMode, sizeMode,
        hoveredId, selectedId, highlightIds, hiddenIds, dimmedIds, pulsePhases,
      } = props;

      stars.visible = props.starfield !== false;
      if (stars.visible) stars.rotation.y = t * 0.008;

      // Auto-rotate when enabled AND user is not dragging / panning / zooming.
      if (props.autoRotate && !cam.isDrag && !cam.isPan && !cam.zoomTarget) {
        cam.theta += 0.0015;
      }

      if (cam.zoomTarget) {
        cam.target.lerp(
          new THREE.Vector3(cam.zoomTarget.x, cam.zoomTarget.y, cam.zoomTarget.z), 0.08,
        );
        cam.radius += (Math.max(20, cam.zoomTarget.radius || 40) - cam.radius) * 0.08;
        if (cam.target.distanceTo(
          new THREE.Vector3(cam.zoomTarget.x, cam.zoomTarget.y, cam.zoomTarget.z)) < 0.5) {
          cam.zoomTarget = null;
          if (props.onTargetReached) props.onTargetReached();
        }
      }
      updateCamera();

      const count = Math.min(nodes.length, MAX);
      nodesMesh.count = count;
      halosMesh.count = count;

      for (let i = 0; i < count; i++) {
        const n = nodes[i];
        const p = positions[i] || { x: 0, y: 0, z: 0 };
        const isHidden = hiddenIds && hiddenIds.has(n.id);
        let s = sizeFor(n, sizeMode);
        const isHover = hoveredId === n.id;
        const isSel = selectedId === n.id;
        const isHL = highlightIds && highlightIds.has(n.id);
        const isDim = dimmedIds && dimmedIds.has(n.id);
        if (isHover || isSel) s *= 1.9;
        else if (isHL) s *= 1.35;
        const pulse = pulsePhases && pulsePhases.get(n.id);
        if (pulse !== undefined) s *= 1 + 0.35 * Math.sin(t * 5 + pulse);
        if (isHidden) s = 0;

        dummy.position.set(p.x, p.y, p.z);
        dummy.scale.setScalar(s);
        dummy.updateMatrix();
        nodesMesh.setMatrixAt(i, dummy.matrix);

        dummy.scale.setScalar(isHidden ? 0 : s * 3.2 * (isHover || isSel ? 1.6 : 1));
        dummy.updateMatrix();
        halosMesh.setMatrixAt(i, dummy.matrix);

        const base = colorFor(n, colorMode);
        tmpColor.copy(base);
        if (isHidden) tmpColor.multiplyScalar(0);
        else if (isDim) tmpColor.multiplyScalar(0.18);
        else if (isSel) tmpColor.multiplyScalar(1.4);
        else if (isHover) tmpColor.multiplyScalar(1.3);
        nodesMesh.setColorAt(i, tmpColor);

        tmpColor.copy(base);
        if (isHidden) tmpColor.multiplyScalar(0);
        else if (isDim) tmpColor.multiplyScalar(0.05);
        halosMesh.setColorAt(i, tmpColor);
      }
      nodesMesh.instanceMatrix.needsUpdate = true;
      halosMesh.instanceMatrix.needsUpdate = true;
      if (nodesMesh.instanceColor) nodesMesh.instanceColor.needsUpdate = true;
      if (halosMesh.instanceColor) halosMesh.instanceColor.needsUpdate = true;

      if (st.edgesDirty) {
        st.edgesDirty = false;
        const { edges, densityCutoff, highlightEdges } = props;
        const sp = [], sc = [], dp = [], dc = [];
        for (const e of edges) {
          if (e.w < densityCutoff) continue;
          const a = nodes[e.a], b = nodes[e.b];
          const pa = positions[e.a], pb = positions[e.b];
          if (!a || !b || !pa || !pb) continue;
          if (hiddenIds && (hiddenIds.has(a.id) || hiddenIds.has(b.id))) continue;
          const isDim = dimmedIds && (dimmedIds.has(a.id) || dimmedIds.has(b.id));
          const isHLE = highlightEdges && highlightEdges.has(
            `${Math.min(e.a, e.b)}_${Math.max(e.a, e.b)}`,
          );
          const ca = KIND_COLOR[a.kind] || DEFAULT_KIND;
          const cb = KIND_COLOR[b.kind] || DEFAULT_KIND;
          const r = (ca.r + cb.r) / 2, g = (ca.g + cb.g) / 2, bb = (ca.b + cb.b) / 2;
          let intensity = 0.35 + (e.w - 0.35) * 1.2;
          if (isDim) intensity *= 0.08;
          if (isHLE) intensity = 1.6;
          const col = [r * intensity, g * intensity, bb * intensity];
          const dst = e.w >= 0.6 ? [sp, sc] : [dp, dc];
          dst[0].push(pa.x, pa.y, pa.z, pb.x, pb.y, pb.z);
          dst[1].push(...col, ...col);
        }
        solidGeo.setAttribute('position', new THREE.Float32BufferAttribute(sp, 3));
        solidGeo.setAttribute('color', new THREE.Float32BufferAttribute(sc, 3));
        dashedGeo.setAttribute('position', new THREE.Float32BufferAttribute(dp, 3));
        dashedGeo.setAttribute('color', new THREE.Float32BufferAttribute(dc, 3));
        dashedLines.computeLineDistances();
      }

      renderer.render(scene, camera);
    }
    animate();

    stateRef.current = {
      scene, camera, renderer, cam, nodesMesh, halosMesh, solidGeo, dashedGeo,
      dashedLines, propsRef: { current: null }, hoveredId: null, edgesDirty: true,
      cleanup: () => {
        cancelAnimationFrame(raf);
        window.removeEventListener('resize', onResize);
        dom.removeEventListener('pointerdown', onDown);
        window.removeEventListener('pointerup', onUp);
        window.removeEventListener('pointermove', onMove);
        dom.removeEventListener('wheel', onWheel);
        dom.removeEventListener('contextmenu', onContext);
        renderer.dispose();
        if (renderer.domElement.parentNode === container) {
          container.removeChild(renderer.domElement);
        }
      },
    };

    return () => stateRef.current.cleanup();
  }, []);

  stateRef.current.propsRef = stateRef.current.propsRef || { current: null };
  stateRef.current.propsRef.current = props;

  useEffect(() => {
    if (stateRef.current) stateRef.current.edgesDirty = true;
  }, [props.positions, props.edges, props.densityCutoff, props.dimmedIds, props.hiddenIds, props.highlightEdges]);

  useEffect(() => {
    if (stateRef.current.cam && props.zoomTarget) {
      stateRef.current.cam.zoomTarget = props.zoomTarget;
    }
  }, [props.zoomTarget]);

  // Auto-fit: compute bounding sphere of visible nodes and animate camera.
  useEffect(() => {
    if (!stateRef.current.cam) return;
    const positions = props.positions;
    const nodes = props.nodes;
    const dimmed = props.dimmedIds;
    const hidden = props.hiddenIds;
    if (!positions || !positions.length) return;

    // Pick visible (non-hidden, non-dimmed) positions; fall back to all.
    const visible = [];
    for (let i = 0; i < positions.length; i++) {
      const n = nodes[i];
      if (!n || (hidden && hidden.has(n.id)) || (dimmed && dimmed.has(n.id))) continue;
      visible.push(positions[i]);
    }
    const pts = visible.length > 0 ? visible : positions;

    // Centroid + max distance → bounding sphere.
    let cx = 0, cy = 0, cz = 0;
    for (const p of pts) { cx += p.x; cy += p.y; cz += p.z; }
    cx /= pts.length; cy /= pts.length; cz /= pts.length;
    let maxD2 = 0;
    for (const p of pts) {
      const dx = p.x - cx, dy = p.y - cy, dz = p.z - cz;
      const d2 = dx * dx + dy * dy + dz * dz;
      if (d2 > maxD2) maxD2 = d2;
    }
    const r = Math.sqrt(maxD2);
    // Radius that fits the sphere in view: r / tan(fov/2) with padding.
    // Camera fov is 50°, so tan(25°) ≈ 0.466. Pad by 1.6x for breathing room.
    const radius = Math.max(30, (r / 0.466) * 1.6);

    stateRef.current.cam.zoomTarget = { x: cx, y: cy, z: cz, radius };
  }, [props.fitViewTrigger]);

  return <div ref={containerRef} style={{ width: '100%', height: '100%' }} />;
}
