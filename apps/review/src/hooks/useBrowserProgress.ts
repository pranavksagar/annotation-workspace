import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../lib/annotationApi';
import type { BrowserProgress, Page } from '../lib/annotationTypes';

/**
 * Per-browser page and view. Restores browser-specific page first, then the drawing's shared
 * last-annotation page, then page one. Saves never touch annotation version or workflow status.
 */
export function useBrowserProgress(drawingId: string, browserId: string, pages: Page[], lastAnnotationPageId: string | null) {
  const [initial, setInitial] = useState<{pageId: string; progress: BrowserProgress | null} | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const latest = useRef<BrowserProgress | null>(null);

  useEffect(() => {
    if (!pages.length) return;
    let cancelled = false;
    const pageIds = new Set(pages.map(p => p.id));
    api.getProgress(drawingId, browserId).then(result => {
      if (cancelled) return;
      const saved = result.progress && pageIds.has(result.progress.page_id) ? result.progress : null;
      const pageId = saved?.page_id ?? (lastAnnotationPageId && pageIds.has(lastAnnotationPageId) ? lastAnnotationPageId : pages[0].id);
      setInitial({pageId, progress: saved});
    }).catch(() => { if (!cancelled) setInitial({pageId: pages[0].id, progress: null}); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawingId, browserId, pages.length]);

  const save = useCallback((progress: BrowserProgress) => {
    latest.current = progress;
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      if (latest.current) api.putProgress(drawingId, browserId, latest.current).catch(() => undefined);
    }, 1000);
  }, [drawingId, browserId]);

  useEffect(() => () => clearTimeout(timer.current), []);
  return {initial, save};
}
