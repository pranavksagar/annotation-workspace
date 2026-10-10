import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { Page, Polygon, Region, RegionKind } from '../lib/annotationTypes';
import { centroid, containsPoint, isValidQuad, moveVertex, rectQuad, resizeQuad, rotateQuad, translateQuad } from '../lib/geometry';
import { type Camera, cameraFromView, drawingPoint, fitScale, type Point, viewFromCamera, zoomAt } from '../lib/viewer';

export type Tool = 'select' | RegionKind;
export interface Layers { TEXT: boolean; SYMBOL: boolean; IGNORE: boolean; badges: boolean }
export interface View { center_x: number; center_y: number; zoom_relative_to_fit: number }
export interface Badge { groupId: string; number: number | null; x: number; y: number; selected: boolean }

type Gesture =
  | {kind: 'pan'; id: number; start: Point; camera: Camera; moved: boolean; clickRegion?: string; additive?: boolean}
  | {kind: 'draw'; id: number; start: Point; current: Point}
  | {kind: 'move'; id: number; regionId: string; start: Point; original: Polygon; moved: boolean}
  | {kind: 'resize'; id: number; regionId: string; corner: number; original: Polygon; free: boolean}
  | {kind: 'rotate'; id: number; regionId: string; original: Polygon; startAngle: number; snap: boolean};

interface Props {
  page: Page;
  imageUrl: string;
  regions: Region[];
  badges: Badge[];
  selectedRegionIds: string[];
  highlightedRegionIds: Set<string>;
  tool: Tool;
  readOnly: boolean;
  pickMode: boolean;
  layers: Layers;
  initialView?: View | null;
  fitNonce: number;
  focus?: {polygon: Polygon; nonce: number} | null;
  onViewChange: (view: View) => void;
  onCreateRegion: (kind: RegionKind, polygon: Polygon) => void;
  onGeometryCommit: (regionId: string, polygon: Polygon, angleDeltaDegrees: number) => void;
  onRegionClick: (regionId: string, additive: boolean) => void;
  onBackgroundClick: () => void;
  onBadgeClick: (groupId: string) => void;
}

const MIN_ZOOM = 0.25, MAX_ZOOM = 16;

function area(polygon: Polygon) {
  return Math.abs(polygon.reduce((s, p, i) => {
    const q = polygon[(i + 1) % polygon.length];
    return s + p[0] * q[1] - q[0] * p[1];
  }, 0)) / 2;
}

function isTyping(target: EventTarget | null) {
  const el = target as HTMLElement | null;
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
}

export function AnnotationCanvas(props: Props) {
  const {page, regions, tool, readOnly, pickMode, layers} = props;
  const viewport = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({width: 0, height: 0});
  const sizeRef = useRef(size);
  const [camera, setCamera] = useState<Camera>({x: 0, y: 0, scale: 1});
  const cameraRef = useRef(camera);
  cameraRef.current = camera;
  const gesture = useRef<Gesture | undefined>(undefined);
  const [draft, setDraft] = useState<{kind: 'draw'; polygon: Polygon} | {kind: 'edit'; regionId: string; polygon: Polygon} | null>(null);
  const [panning, setPanning] = useState(false);
  const [spaceHeld, setSpaceHeld] = useState(false);
  const initialized = useRef(false);
  const onViewChange = useRef(props.onViewChange);
  onViewChange.current = props.onViewChange;

  const fit = () => {
    const s = sizeRef.current, scale = fitScale(s, page);
    setCamera({scale, x: (s.width - page.width * scale) / 2, y: (s.height - page.height * scale) / 2});
  };

  useLayoutEffect(() => {
    const measure = () => {
      const rect = viewport.current!.getBoundingClientRect();
      const next = {width: rect.width, height: rect.height};
      if (!next.width || !next.height || (next.width === sizeRef.current.width && next.height === sizeRef.current.height)) return;
      const previous = sizeRef.current;
      sizeRef.current = next;
      setSize(next);
      if (!initialized.current) {
        initialized.current = true;
        if (props.initialView) setCamera(cameraFromView(next, page, props.initialView));
        else fit();
      } else if (previous.width) {
        // Window resize: keep the same canonical center and zoom relative to fit.
        setCamera(c => cameraFromView(next, page, viewFromCamera(previous, page, c)));
      }
    };
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(measure);
    observer?.observe(viewport.current!);
    return () => observer?.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page.id]);

  useEffect(() => {
    if (initialized.current && sizeRef.current.width) onViewChange.current(viewFromCamera(sizeRef.current, page, camera));
  }, [camera, page]);

  useEffect(() => { if (props.fitNonce) fit(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [props.fitNonce]);

  useEffect(() => {
    if (!props.focus || !sizeRef.current.width) return;
    const xs = props.focus.polygon.map(p => p[0]), ys = props.focus.polygon.map(p => p[1]);
    const left = Math.min(...xs), right = Math.max(...xs), top = Math.min(...ys), bottom = Math.max(...ys);
    const s = sizeRef.current, base = fitScale(s, page);
    const scale = Math.max(base * MIN_ZOOM, Math.min(base * 6, s.width * 0.4 / Math.max(5, right - left), s.height * 0.4 / Math.max(5, bottom - top)));
    setCamera({scale, x: s.width / 2 - (left + right) / 2 * scale, y: s.height / 2 - (top + bottom) / 2 * scale});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.focus?.nonce]);

  const zoom = (factor: number, anchor?: Point) => {
    const at = anchor ?? {x: sizeRef.current.width / 2, y: sizeRef.current.height / 2};
    const base = fitScale(sizeRef.current, page);
    setCamera(c => zoomAt(c, at, Math.min(base * MAX_ZOOM, Math.max(base * MIN_ZOOM, c.scale * factor))));
  };

  useEffect(() => {
    const node = viewport.current!;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      if (gesture.current) return;
      const rect = node.getBoundingClientRect();
      const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1);
      zoom(Math.exp(-Math.max(-200, Math.min(200, pixels)) * 0.002), {x: event.clientX - rect.left, y: event.clientY - rect.top});
    };
    node.addEventListener('wheel', wheel, {passive: false});
    return () => node.removeEventListener('wheel', wheel);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page.id]);

  const cancelGesture = () => {
    gesture.current = undefined;
    setDraft(null);
    setPanning(false);
  };

  useEffect(() => {
    const down = (event: KeyboardEvent) => {
      if (isTyping(event.target)) return;
      if (event.key === 'Escape' && gesture.current) { event.stopPropagation(); cancelGesture(); }
      if (event.code === 'Space' && !event.repeat) { event.preventDefault(); setSpaceHeld(true); }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === '+' || event.key === '=') zoom(1.25);
      else if (event.key === '-') zoom(1 / 1.25);
      else if (event.key === '0') fit();
    };
    const up = (event: KeyboardEvent) => { if (event.code === 'Space') setSpaceHeld(false); };
    window.addEventListener('keydown', down, true);
    window.addEventListener('keyup', up);
    return () => { window.removeEventListener('keydown', down, true); window.removeEventListener('keyup', up); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page.id]);

  const local = (event: React.PointerEvent): Point => {
    const rect = viewport.current!.getBoundingClientRect();
    return {x: event.clientX - rect.left, y: event.clientY - rect.top};
  };
  const canonical = (event: React.PointerEvent) => drawingPoint(cameraRef.current, local(event));
  const clampPoint = (p: Point): Point => ({x: Math.min(page.width, Math.max(0, p.x)), y: Math.min(page.height, Math.max(0, p.y))});

  const visibleRegions = useMemo(() => regions.filter(r => layers[r.kind]), [regions, layers]);
  const regionById = useMemo(() => new Map(regions.map(r => [r.id, r])), [regions]);

  const hitTest = (p: Point): Region | undefined => {
    const tolerance = 3 / cameraRef.current.scale;
    let best: Region | undefined, bestArea = Infinity;
    for (const region of visibleRegions) {
      if (!containsPoint(region.polygon, p, tolerance)) continue;
      const a = area(region.polygon);
      if (a < bestArea) { best = region; bestArea = a; }
    }
    return best;
  };

  const single = props.selectedRegionIds.length === 1 ? regionById.get(props.selectedRegionIds[0]) : undefined;
  const editable = !readOnly && tool === 'select' && !pickMode;

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (gesture.current || (event.button !== 0 && event.button !== 1)) return;
    event.preventDefault();
    viewport.current?.focus({preventScroll: true});
    const id = event.pointerId, screen = local(event), p = drawingPoint(cameraRef.current, screen);
    const startPan = (extra: Partial<Extract<Gesture, {kind: 'pan'}>> = {}) => {
      gesture.current = {kind: 'pan', id, start: screen, camera: cameraRef.current, moved: false, ...extra};
    };
    const handle = (event.target as Element).closest('[data-handle]')?.getAttribute('data-handle');
    if (event.button === 1 || spaceHeld) startPan();
    else if (handle && single && editable) {
      if (handle === 'rotate') {
        const c = centroid(single.polygon);
        gesture.current = {kind: 'rotate', id, regionId: single.id, original: single.polygon, startAngle: Math.atan2(p.y - c.y, p.x - c.x), snap: event.shiftKey};
      } else {
        gesture.current = {kind: 'resize', id, regionId: single.id, corner: Number(handle.split('-')[1]), original: single.polygon, free: event.altKey};
      }
    } else if (tool !== 'select' && !readOnly && !pickMode) {
      if (p.x < 0 || p.y < 0 || p.x > page.width || p.y > page.height) startPan();
      else gesture.current = {kind: 'draw', id, start: p, current: p};
    } else {
      const hit = hitTest(p);
      const additive = event.shiftKey || event.metaKey || event.ctrlKey;
      if (hit && editable && !additive) {
        if (!props.selectedRegionIds.includes(hit.id) || props.selectedRegionIds.length > 1) props.onRegionClick(hit.id, false);
        gesture.current = {kind: 'move', id, regionId: hit.id, start: p, original: hit.polygon, moved: false};
      } else {
        startPan({clickRegion: hit?.id, additive});
      }
    }
    event.currentTarget.setPointerCapture(id);
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const g = gesture.current;
    if (!g || g.id !== event.pointerId) return;
    const p = canonical(event);
    if (g.kind === 'pan') {
      const screen = local(event);
      if (Math.hypot(screen.x - g.start.x, screen.y - g.start.y) > 4) g.moved = true;
      if (g.moved) {
        setPanning(true);
        setCamera({...g.camera, x: g.camera.x + screen.x - g.start.x, y: g.camera.y + screen.y - g.start.y});
      }
    } else if (g.kind === 'draw') {
      g.current = clampPoint(p);
      setDraft({kind: 'draw', polygon: rectQuad(g.start.x, g.start.y, g.current.x, g.current.y)});
    } else if (g.kind === 'move') {
      const dx = p.x - g.start.x, dy = p.y - g.start.y;
      if (Math.hypot(dx, dy) * cameraRef.current.scale > 3) g.moved = true;
      if (g.moved) setDraft({kind: 'edit', regionId: g.regionId, polygon: translateQuad(g.original, dx, dy, page.width, page.height)});
    } else if (g.kind === 'resize') {
      const next = g.free ? moveVertex(g.original, g.corner, clampPoint(p))
        : resizeQuad(g.original, g.corner, p, 1 / cameraRef.current.scale);
      if (isValidQuad(next, page.width, page.height)) setDraft({kind: 'edit', regionId: g.regionId, polygon: next});
    } else if (g.kind === 'rotate') {
      const c = centroid(g.original);
      let delta = Math.atan2(p.y - c.y, p.x - c.x) - g.startAngle;
      if (event.shiftKey) delta = Math.round(delta / (Math.PI / 12)) * (Math.PI / 12);
      const next = rotateQuad(g.original, delta, c);
      if (isValidQuad(next, page.width, page.height)) setDraft({kind: 'edit', regionId: g.regionId, polygon: next});
    }
  };

  const onPointerUp = (event: React.PointerEvent<HTMLDivElement>) => {
    const g = gesture.current;
    if (!g || g.id !== event.pointerId) return;
    event.currentTarget.releasePointerCapture(event.pointerId);
    if (g.kind === 'pan' && !g.moved) {
      if (g.clickRegion) props.onRegionClick(g.clickRegion, !!g.additive);
      else props.onBackgroundClick();
    } else if (g.kind === 'draw' && draft?.kind === 'draw') {
      const [a, , c] = draft.polygon;
      const minimum = 3 / cameraRef.current.scale;
      if (c[0] - a[0] >= minimum && c[1] - a[1] >= minimum && isValidQuad(draft.polygon, page.width, page.height)) {
        props.onCreateRegion(tool as RegionKind, draft.polygon);
      }
    } else if ((g.kind === 'move' || g.kind === 'resize' || g.kind === 'rotate') && draft?.kind === 'edit') {
      // One geometry operation per gesture, committed on release.
      let angle = 0;
      if (g.kind === 'rotate') {
        const c = centroid(g.original), first = g.original[0], moved = draft.polygon[0];
        angle = (Math.atan2(moved[1] - c.y, moved[0] - c.x) - Math.atan2(first[1] - c.y, first[0] - c.x)) * 180 / Math.PI;
      }
      if (isValidQuad(draft.polygon, page.width, page.height)) props.onGeometryCommit(g.regionId, draft.polygon, angle);
    }
    cancelGesture();
  };

  const percentage = size.width ? Math.round(camera.scale / fitScale(size, page) * 100) : 100;
  const scale = camera.scale;
  const selected = new Set(props.selectedRegionIds);
  const shown = draft?.kind === 'edit' ? visibleRegions.map(r => r.id === draft.regionId ? {...r, polygon: draft.polygon} : r) : visibleRegions;
  const handleRegion = single && editable ? (draft?.kind === 'edit' && draft.regionId === single.id ? {...single, polygon: draft.polygon} : single) : undefined;

  return <div className="canvas-shell">
    <div className="zoom-controls" role="toolbar" aria-label="Zoom">
      <button onClick={() => zoom(1 / 1.25)} title="Zoom out (−)" aria-label="Zoom out">−</button>
      <output aria-label="Zoom relative to fit">{percentage}%</output>
      <button onClick={() => zoom(1.25)} title="Zoom in (+)" aria-label="Zoom in">+</button>
    </div>
    <div ref={viewport} role="application" aria-label={`Drawing page ${page.page_number}`} tabIndex={0}
      className={`canvas-viewport tool-${tool} ${panning ? 'panning' : ''} ${spaceHeld ? 'space-pan' : ''} ${pickMode ? 'pick-mode' : ''}`}
      onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
      onPointerCancel={cancelGesture} onLostPointerCapture={() => gesture.current && cancelGesture()}>
      <div className="canvas-page" style={{width: page.width, height: page.height,
        transform: `translate(${camera.x}px, ${camera.y}px) scale(${scale})`}}>
        <img src={props.imageUrl} alt={`Page ${page.page_number}`} draggable={false} width={page.width} height={page.height}/>
        <svg viewBox={`0 0 ${page.width} ${page.height}`} width={page.width} height={page.height}>
          <RegionLayer regions={shown} selected={selected} highlighted={props.highlightedRegionIds}/>
          {layers.badges && props.badges.map(b => <g key={b.groupId} className={`badge ${b.selected ? 'selected' : ''}`}
            transform={`translate(${b.x} ${b.y}) scale(${1 / scale}) translate(10 -10)`}
            onPointerDown={event => { event.stopPropagation(); props.onBadgeClick(b.groupId); }}>
            <title>{`Requirement ${b.number ?? '(saving…)'}`}</title>
            <circle r={11}/>
            <text textAnchor="middle" dominantBaseline="central">{b.number ?? '…'}</text>
          </g>)}
          {draft?.kind === 'draw' && <polygon className={`draft-box kind-${tool}`} points={draft.polygon.map(p => p.join(',')).join(' ')}/>}
          {handleRegion && <Handles polygon={handleRegion.polygon} scale={scale}/>}
        </svg>
      </div>
    </div>
  </div>;
}

const RegionLayer = memo(function RegionLayer({regions, selected, highlighted}: {regions: Region[]; selected: Set<string>; highlighted: Set<string>}) {
  return <g>
    {regions.map(r => <polygon key={r.id} data-region={r.id}
      className={`region kind-${r.kind} ${selected.has(r.id) ? 'selected' : ''} ${highlighted.has(r.id) ? 'highlighted' : ''} ${
        r.kind !== 'IGNORE' && r.legibility === 'READABLE' && !r.transcription && !r.symbol_code ? 'incomplete' : ''}`}
      points={r.polygon.map(p => p.join(',')).join(' ')}>
      <title>{r.transcription || r.symbol_code || (r.kind === 'IGNORE' ? 'Unreadable region' : 'Incomplete label')}</title>
    </polygon>)}
  </g>;
});

function Handles({polygon, scale}: {polygon: Polygon; scale: number}) {
  const [p0, p1] = polygon;
  const len = Math.hypot(p1[0] - p0[0], p1[1] - p0[1]) || 1;
  const v = {x: (p1[1] - p0[1]) / len, y: -(p1[0] - p0[0]) / len};  // outward from the top edge
  const mid = {x: (p0[0] + p1[0]) / 2, y: (p0[1] + p1[1]) / 2};
  const rotate = {x: mid.x + v.x * 26 / scale, y: mid.y + v.y * 26 / scale};
  const r = 5 / scale;
  return <g className="handles">
    <line x1={mid.x} y1={mid.y} x2={rotate.x} y2={rotate.y}/>
    <circle data-handle="rotate" className="rotate-handle" cx={rotate.x} cy={rotate.y} r={r * 1.2}><title>Rotate (Shift snaps to 15°)</title></circle>
    {polygon.map((p, i) => <rect key={i} data-handle={`corner-${i}`} className={i === 0 ? 'corner first' : 'corner'}
      x={p[0] - r} y={p[1] - r} width={r * 2} height={r * 2}>
      <title>{i === 0 ? 'Resize — first corner in reading order (Alt moves one corner)' : 'Resize (Alt moves one corner)'}</title>
    </rect>)}
  </g>;
}
