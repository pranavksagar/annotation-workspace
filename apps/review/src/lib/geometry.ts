/**
 * Client mirror of the server's quadrilateral contract: four distinct vertices, clockwise in the
 * top-left-origin canonical frame, convex, positive area, inside the page (0.01 unit epsilon).
 */
import type { Polygon } from './annotationTypes';
import type { Point } from './viewer';

export const BOUNDARY_EPSILON = 0.01;

export const round6 = (value: number) => Math.round(value * 1e6) / 1e6 || 0;

export function isValidQuad(polygon: Polygon, width: number, height: number): boolean {
  if (polygon.length !== 4) return false;
  for (const [x, y] of polygon) {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
    if (x < -BOUNDARY_EPSILON || y < -BOUNDARY_EPSILON || x > width + BOUNDARY_EPSILON || y > height + BOUNDARY_EPSILON) return false;
  }
  if (new Set(polygon.map(p => `${p[0]},${p[1]}`)).size !== 4) return false;
  for (let i = 0; i < 4; i++) {
    const a = polygon[i], b = polygon[(i + 1) % 4], c = polygon[(i + 2) % 4];
    if ((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) <= 0) return false;
  }
  return true;
}

export function clampQuad(polygon: Polygon, width: number, height: number): Polygon {
  return polygon.map(([x, y]) => [round6(Math.min(width, Math.max(0, x))), round6(Math.min(height, Math.max(0, y)))]);
}

/** Clockwise rectangle starting at the top-left corner. */
export function rectQuad(x1: number, y1: number, x2: number, y2: number): Polygon {
  const left = Math.min(x1, x2), right = Math.max(x1, x2), top = Math.min(y1, y2), bottom = Math.max(y1, y2);
  return [[left, top], [right, top], [right, bottom], [left, bottom]];
}

export function bbox(points: Polygon) {
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  return {left: Math.min(...xs), top: Math.min(...ys), right: Math.max(...xs), bottom: Math.max(...ys)};
}

export function centroid(polygon: Polygon): Point {
  return {x: polygon.reduce((s, p) => s + p[0], 0) / polygon.length, y: polygon.reduce((s, p) => s + p[1], 0) / polygon.length};
}

export function containsPoint(polygon: Polygon, point: Point, tolerance = 0): boolean {
  if (polygon.length < 3) return false;
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [ax, ay] = polygon[j], [bx, by] = polygon[i];
    const dx = bx - ax, dy = by - ay, length = dx * dx + dy * dy;
    const t = length ? Math.max(0, Math.min(1, ((point.x - ax) * dx + (point.y - ay) * dy) / length)) : 0;
    if (Math.hypot(point.x - ax - t * dx, point.y - ay - t * dy) <= Math.max(tolerance, 1e-8)) return true;
    if ((ay > point.y) !== (by > point.y) && point.x < (bx - ax) * (point.y - ay) / (by - ay) + ax) inside = !inside;
  }
  return inside;
}

export function translateQuad(polygon: Polygon, dx: number, dy: number, width: number, height: number): Polygon {
  const box = bbox(polygon);
  const cdx = Math.min(width - box.right, Math.max(-box.left, dx));
  const cdy = Math.min(height - box.bottom, Math.max(-box.top, dy));
  return polygon.map(([x, y]) => [round6(x + cdx), round6(y + cdy)]);
}

/** Rotation keeps vertex order, so the first corner stays first in reading orientation. */
export function rotateQuad(polygon: Polygon, radians: number, center = centroid(polygon)): Polygon {
  const cos = Math.cos(radians), sin = Math.sin(radians);
  return polygon.map(([x, y]) => {
    const dx = x - center.x, dy = y - center.y;
    return [round6(center.x + dx * cos - dy * sin), round6(center.y + dx * sin + dy * cos)];
  });
}

const U = [0, 1, 1, 0], V = [0, 0, 1, 1];

/**
 * Resize by dragging corner `index`; the opposite corner stays fixed and the result is a rectangle
 * aligned with the region's reading direction (edge 0→1).
 */
export function resizeQuad(polygon: Polygon, index: number, pointer: Point, minSize = 1): Polygon {
  const fixed = (index + 2) % 4;
  const [p0, p1] = polygon;
  const len = Math.hypot(p1[0] - p0[0], p1[1] - p0[1]) || 1;
  const u = {x: (p1[0] - p0[0]) / len, y: (p1[1] - p0[1]) / len};
  const v = {x: -u.y, y: u.x};
  const f = {x: polygon[fixed][0], y: polygon[fixed][1]};
  const d = {x: pointer.x - f.x, y: pointer.y - f.y};
  const su = U[index] - U[fixed], sv = V[index] - V[fixed];
  const w = Math.max(minSize, (d.x * u.x + d.y * u.y) * su);
  const h = Math.max(minSize, (d.x * v.x + d.y * v.y) * sv);
  return [0, 1, 2, 3].map(k => {
    const a = (U[k] - U[fixed]) * w, b = (V[k] - V[fixed]) * h;
    return [round6(f.x + a * u.x + b * v.x), round6(f.y + a * u.y + b * v.y)];
  });
}

export function moveVertex(polygon: Polygon, index: number, pointer: Point): Polygon {
  return polygon.map((p, i) => i === index ? [round6(pointer.x), round6(pointer.y)] : p);
}

export function normalizeAngle(degrees: number): number {
  let angle = degrees % 360;
  if (angle > 180) angle -= 360;
  if (angle <= -180) angle += 360;
  return round6(angle);
}
