import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../lib/annotationApi';
import type { Polygon } from '../lib/annotationTypes';

/**
 * Reads the text in a newly drawn box and hands it to `onText`, which fills the transcription.
 * The annotator corrects it like any typed text. A failed or empty read just leaves the field empty.
 */
export function useOcrSuggestions(drawingId: string, enabled: boolean, onText: (regionId: string, text: string) => void) {
  const [reading, setReading] = useState<ReadonlySet<string>>(new Set());
  const controllers = useRef(new Map<string, AbortController>());
  const latest = useRef(onText);
  latest.current = onText;
  useEffect(() => {
    const active = controllers.current;
    return () => active.forEach(controller => controller.abort());
  }, []);

  const request = useCallback((regionId: string, pageId: string, polygon: Polygon) => {
    if (!enabled) return;
    const controller = new AbortController();
    controllers.current.set(regionId, controller);
    setReading(state => new Set(state).add(regionId));
    api.suggestOcr(drawingId, pageId, polygon, controller.signal)
      .then(result => { if (!controller.signal.aborted && result.text.trim()) latest.current(regionId, result.text.trim()); })
      .catch(() => undefined)
      .finally(() => {
        if (controllers.current.get(regionId) === controller) controllers.current.delete(regionId);
        setReading(state => { const next = new Set(state); next.delete(regionId); return next; });
      });
  }, [drawingId, enabled]);

  /** Stop reading a region that was deleted. */
  const cancel = useCallback((regionId: string) => controllers.current.get(regionId)?.abort(), []);

  return {reading, request, cancel};
}
