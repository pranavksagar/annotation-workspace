import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { api, type Identity } from '../lib/annotationApi';
import { localDrafts } from '../lib/localDrafts';
import type { AnnotationStatus, DocState, Version } from '../lib/annotationTypes';
import { EditorStore } from '../state/editorStore';

/** One autosave store per editor instance (drawing + tab identity). */
export function useAutosave(drawingId: string, identity: Identity, onLeaseLost: () => void,
                            initial: {base: DocState; version: Version; status: AnnotationStatus}) {
  const leaseLost = useRef(onLeaseLost);
  leaseLost.current = onLeaseLost;
  const store = useMemo(() => {
    const created = new EditorStore({
      drawingId, identity, drafts: localDrafts,
      mutate: (id, batch, who, lease) => api.mutate(id, batch, who, lease),
      onLeaseLost: () => leaseLost.current(),
    });
    created.load(initial.base, initial.version, initial.status);
    return created;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawingId, identity]);
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot);

  useEffect(() => {
    const online = () => store.retryNow();
    const beforeUnload = (event: BeforeUnloadEvent) => {
      // A warning only; the IndexedDB outbox, not this event, is what preserves work.
      if (store.getSnapshot().hasUnsaved) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('online', online);
    window.addEventListener('beforeunload', beforeUnload);
    return () => {
      window.removeEventListener('online', online);
      window.removeEventListener('beforeunload', beforeUnload);
    };
  }, [store]);
  useEffect(() => {
    store.activate();
    return () => store.dispose();
  }, [store]);

  return {store, snapshot};
}
