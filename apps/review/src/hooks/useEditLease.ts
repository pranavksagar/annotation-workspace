import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, type Identity, type LeaseCredentials } from '../lib/annotationApi';
import type { EditorStore } from '../state/editorStore';

export type LeaseState =
  | {kind: 'idle'}
  | {kind: 'acquiring'}
  | {kind: 'held'}
  | {kind: 'unavailable'; retryAt: number}
  | {kind: 'not-editable'; code: string}
  | {kind: 'error'; message: string};

/**
 * Editing lease for one editor instance. The raw token lives only in memory. There is no forced takeover:
 * after a reload or crash the previous lease must be released or expire (at most the lease duration).
 */
export function useEditLease(drawingId: string, identity: Identity, store: EditorStore, enabled: boolean,
                             onAcquired?: (annotationVersion: string) => void) {
  const [state, setState] = useState<LeaseState>({kind: 'idle'});
  const lease = useRef<LeaseCredentials | null>(null);
  const active = useRef(false);
  const acquiring = useRef(false);
  const heartbeatTimer = useRef<ReturnType<typeof setInterval> | undefined>(undefined);
  const onAcquiredRef = useRef(onAcquired);
  onAcquiredRef.current = onAcquired;

  const drop = useCallback(() => {
    lease.current = null;
    store.setLease(null);
    clearInterval(heartbeatTimer.current);
  }, [store]);

  const sendRelease = useCallback((credentials: LeaseCredentials, keepalive: boolean) =>
    api.releaseLease(drawingId, credentials, keepalive).catch(() => undefined), [drawingId]);

  const acquire = useCallback(async (): Promise<void> => {
    if (acquiring.current || lease.current || !active.current) return;
    acquiring.current = true;
    setState({kind: 'acquiring'});
    try {
      const result = await api.acquireLease(drawingId, identity);
      const credentials = {lease_token: result.lease_token, lease_generation: result.lease_generation};
      if (!active.current) {
        void sendRelease(credentials, false);  // unmounted while acquiring
        return;
      }
      lease.current = credentials;
      setState({kind: 'held'});
      onAcquiredRef.current?.(result.annotation_version);
      store.setLease(credentials);
      clearInterval(heartbeatTimer.current);
      heartbeatTimer.current = setInterval(() => void heartbeat(), result.heartbeat_seconds * 1000);
    } catch (error) {
      if (error instanceof ApiError && error.code === 'LEASE_HELD') {
        setState({kind: 'unavailable', retryAt: Date.now() + (error.retryAfterSeconds ?? 90) * 1000});
      } else if (error instanceof ApiError && (error.code === 'DRAWING_COMPLETED' || error.code === 'PREPARATION_NOT_READY')) {
        setState({kind: 'not-editable', code: error.code});
      } else {
        setState({kind: 'error', message: error instanceof Error ? error.message : 'Could not start editing.'});
      }
    } finally {
      acquiring.current = false;
    }
    // heartbeat is a hoisted function declaration below
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawingId, identity, store, sendRelease]);

  async function heartbeat() {
    const current = lease.current;
    if (!current) return;
    try {
      await api.heartbeat(drawingId, current);
    } catch (error) {
      if (error instanceof ApiError && (error.code === 'LEASE_LOST' || error.status === 404)) {
        // Never treat a local timer as proof of a valid server lease: reacquire or fall back to viewing.
        drop();
        await acquire();
      }
    }
  }
  const heartbeatRef = useRef(heartbeat);
  heartbeatRef.current = heartbeat;

  const release = useCallback(async (keepalive = false) => {
    const current = lease.current;
    if (!current) return;
    drop();
    setState({kind: 'idle'});
    await sendRelease(current, keepalive);
  }, [drop, sendRelease]);

  useEffect(() => {
    if (!enabled) return;
    active.current = true;
    void acquire();
    const visible = () => { if (document.visibilityState === 'visible') void heartbeatRef.current(); };
    const pagehide = () => {
      const current = lease.current;
      if (current) void sendRelease(current, true);  // best effort; expiry covers crashes
    };
    document.addEventListener('visibilitychange', visible);
    window.addEventListener('pagehide', pagehide);
    return () => {
      active.current = false;
      document.removeEventListener('visibilitychange', visible);
      window.removeEventListener('pagehide', pagehide);
      void release();
    };
  }, [enabled, acquire, release, sendRelease]);

  /**
   * A save was rejected with LEASE_LOST. If a heartbeat already obtained a newer lease, confirm it with the
   * server and resume with it; otherwise acquire a new one (or fall back to viewing if someone else holds it).
   */
  const reacquire = useCallback(async () => {
    const current = lease.current;
    if (current) {
      try {
        await api.heartbeat(drawingId, current);
        store.setLease(current);
        return;
      } catch { /* the newer lease is not valid either */ }
    }
    drop();
    await acquire();
  }, [drawingId, store, drop, acquire]);

  return {state, acquire, reacquire, release, hasLease: () => lease.current !== null};
}
