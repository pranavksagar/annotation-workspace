import { describe, expect, it } from 'vitest';
import { isValidQuad, rectQuad, resizeQuad, rotateQuad, translateQuad } from '../lib/geometry';
import { cameraFromView, drawingPoint, viewFromCamera } from '../lib/viewer';

describe('quad geometry mirrors the server contract', () => {
  it('accepts clockwise rectangles and rejects invalid shapes', () => {
    expect(isValidQuad(rectQuad(10, 10, 50, 20), 100, 100)).toBe(true);
    expect(isValidQuad([[0, 0], [0, 10], [10, 10], [10, 0]], 100, 100)).toBe(false);  // counter-clockwise
    expect(isValidQuad([[0, 0], [10, 10], [10, 0], [0, 10]], 100, 100)).toBe(false);  // self-intersecting
    expect(isValidQuad(rectQuad(90, 90, 110, 95), 100, 100)).toBe(false);             // outside the page
  });

  it('rotation preserves vertex order and validity', () => {
    const quad = rectQuad(40, 40, 60, 50);
    const rotated = rotateQuad(quad, Math.PI / 2);
    expect(isValidQuad(rotated, 100, 100)).toBe(true);
    // The first corner (reading start) moves with the rotation instead of being re-sorted.
    expect(rotated[0][0]).toBeCloseTo(55);
    expect(rotated[0][1]).toBeCloseTo(35);
  });

  it('resizing a rotated region keeps the opposite corner fixed and stays a rectangle', () => {
    const quad = rotateQuad(rectQuad(40, 40, 60, 50), Math.PI / 6);
    const resized = resizeQuad(quad, 2, {x: 80, y: 80});
    expect(resized[0][0]).toBeCloseTo(quad[0][0]);
    expect(resized[0][1]).toBeCloseTo(quad[0][1]);
    expect(isValidQuad(resized, 100, 100)).toBe(true);
    const side = (a: number[], b: number[]) => Math.hypot(a[0] - b[0], a[1] - b[1]);
    expect(side(resized[0], resized[1])).toBeCloseTo(side(resized[3], resized[2]));
  });

  it('moving clamps the region inside the page', () => {
    const moved = translateQuad(rectQuad(10, 10, 20, 20), 500, -500, 100, 100);
    expect(moved).toEqual(rectQuad(90, 0, 100, 10));
  });

  it('view state survives a window resize without changing canonical coordinates', () => {
    const page = {width: 842, height: 595};
    const view = {center_x: 300, center_y: 200, zoom_relative_to_fit: 3};
    const small = cameraFromView({width: 800, height: 600}, page, view);
    const large = cameraFromView({width: 1600, height: 1000}, page, view);
    const centerSmall = drawingPoint(small, {x: 400, y: 300});
    const centerLarge = drawingPoint(large, {x: 800, y: 500});
    expect(centerSmall.x).toBeCloseTo(300);
    expect(centerLarge.y).toBeCloseTo(200);
    expect(viewFromCamera({width: 1600, height: 1000}, page, large).zoom_relative_to_fit).toBeCloseTo(3);
  });
});
