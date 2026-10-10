/** Viewport transforms (from the ingestor prototype). Screen = canonical * scale + offset. */
export type Camera = {x: number; y: number; scale: number};
export type Point = {x: number; y: number};

export function zoomAt(camera: Camera, anchor: Point, scale: number): Camera {
  const ratio = scale / camera.scale;
  return {scale, x: anchor.x - (anchor.x - camera.x) * ratio, y: anchor.y - (anchor.y - camera.y) * ratio};
}

/** Inverse viewport transform: screen point to canonical page coordinates. */
export function drawingPoint(camera: Camera, point: Point): Point {
  return {x: (point.x - camera.x) / camera.scale, y: (point.y - camera.y) / camera.scale};
}

export function fitScale(viewport: {width: number; height: number}, page: {width: number; height: number}, margin = 32) {
  return Math.max(0.0001, Math.min(Math.max(1, viewport.width - margin) / page.width, Math.max(1, viewport.height - margin) / page.height));
}

/** Navigation state is stored as a canonical center and zoom relative to fit, so it survives window resizes. */
export function cameraFromView(viewport: {width: number; height: number}, page: {width: number; height: number},
                               view: {center_x: number; center_y: number; zoom_relative_to_fit: number}): Camera {
  const zoom = Math.min(16, Math.max(0.25, view.zoom_relative_to_fit));
  const scale = fitScale(viewport, page) * zoom;
  const cx = Math.min(page.width, Math.max(0, view.center_x)), cy = Math.min(page.height, Math.max(0, view.center_y));
  return {scale, x: viewport.width / 2 - cx * scale, y: viewport.height / 2 - cy * scale};
}

export function viewFromCamera(viewport: {width: number; height: number}, page: {width: number; height: number}, camera: Camera) {
  const center = drawingPoint(camera, {x: viewport.width / 2, y: viewport.height / 2});
  return {center_x: center.x, center_y: center.y, zoom_relative_to_fit: camera.scale / fitScale(viewport, page)};
}
