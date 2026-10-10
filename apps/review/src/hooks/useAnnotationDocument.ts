import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../lib/annotationApi';
import type { AnnotationDocument, DrawingDetail, Vocabulary } from '../lib/annotationTypes';

export type LoadPhase =
  | {kind: 'loading'}
  | {kind: 'preparing'; detail: DrawingDetail}
  | {kind: 'ready'; detail: DrawingDetail; document: AnnotationDocument; vocabulary: Vocabulary}
  | {kind: 'error'; message: string; notFound: boolean};

let vocabularyCache: Promise<Vocabulary> | null = null;
const loadVocabulary = () => {
  vocabularyCache ??= api.vocabulary().catch(error => { vocabularyCache = null; throw error; });
  return vocabularyCache;
};

/** Loads drawing metadata and the full label document; polls preparation progress every two seconds. */
export function useAnnotationDocument(drawingId: string) {
  const [phase, setPhase] = useState<LoadPhase>({kind: 'loading'});
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const alive = useRef(true);

  const load = useCallback(async () => {
    clearTimeout(timer.current);
    try {
      const detail = await api.detail(drawingId);
      if (!alive.current) return;
      if (detail.drawing.preparation_state !== 'READY') {
        setPhase({kind: 'preparing', detail});
        if (detail.drawing.preparation_state !== 'FAILED') timer.current = setTimeout(() => void load(), 2000);
        return;
      }
      const [document, vocabulary] = await Promise.all([api.document(drawingId), loadVocabulary()]);
      if (alive.current) setPhase({kind: 'ready', detail, document, vocabulary});
    } catch (error) {
      if (!alive.current) return;
      const notFound = error instanceof ApiError && error.status === 404;
      setPhase({kind: 'error', notFound, message: notFound ? 'This drawing was not found.'
        : error instanceof Error ? error.message : 'The drawing could not be loaded.'});
      if (!notFound) timer.current = setTimeout(() => void load(), 5000);
    }
  }, [drawingId]);

  useEffect(() => {
    alive.current = true;
    void load();
    return () => { alive.current = false; clearTimeout(timer.current); };
  }, [load]);

  return {phase, reload: load};
}
