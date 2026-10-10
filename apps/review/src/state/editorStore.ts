/**
 * Autosave engine around the annotation reducer.
 *
 * on local change: apply to visible document → persist to the IndexedDB outbox → schedule a flush
 * flush: at most one sealed batch in flight; seal only after the local write succeeded (or local storage is
 *        known to be unavailable); freeze mutation ID, expected version and operations; persist; send
 * success: replace the acknowledged base; newer local edits stay queued on top
 * unknown result: keep the exact frozen batch and resend it (the server resolves it by receipt)
 * conflict / lost lease / rejection: pause automatic saving and keep every local change
 */
import { ApiError, type Identity, type LeaseCredentials } from '../lib/annotationApi';
import type { AnnotationStatus, CompletionIssue, DocState, MutationResult, Operation, SealedEnvelope, Version } from '../lib/annotationTypes';
import { type LocalDrafts, outboxKey } from '../lib/localDrafts';
import { annotationReducer, type EditorAction, type EditorState, initialEditorState, type PendingOp, type SealedBatch,
  visibleDocument } from './annotationReducer';
import { applyOps, invertOps } from './docOps';

export type SaveStatus = 'saved' | 'saving' | 'offline' | 'retrying' | 'paused';
export interface PauseReason {
  kind: 'conflict' | 'lease' | 'completed' | 'rejected';
  code: string;
  message: string;
  details?: CompletionIssue[];
  currentVersion?: string;
}

export interface EditorSnapshot {
  state: EditorState;
  visible: DocState;
  saveStatus: SaveStatus;
  paused: PauseReason | null;
  storageAvailable: boolean;
  lastSavedAt: string | null;
  hasUnsaved: boolean;
  canEdit: boolean;
}

export interface StoreDeps {
  drawingId: string;
  identity: Identity;
  mutate: (drawingId: string, batch: SealedBatch, identity: Identity, lease: LeaseCredentials) => Promise<MutationResult>;
  drafts: LocalDrafts;
  maxBatchOperations?: number;
  maxBatchBytes?: number;
  textDebounceMs?: number;
  textMaxWaitMs?: number;
  onLeaseLost?: () => void;
}

type Waiter = {resolve: () => void; reject: (error: Error) => void};

export class SaveBlockedError extends Error {}

export class EditorStore {
  private state: EditorState = initialEditorState();
  private snapshot: EditorSnapshot;
  private listeners = new Set<() => void>();
  private lease: LeaseCredentials | null = null;
  private paused: PauseReason | null = null;
  private network: 'ok' | 'offline' | 'retrying' = 'ok';
  private storageAvailable = true;
  private lastSavedAt: string | null = null;
  private readOnly = true;
  private sending = false;
  private persistChain: Promise<void> = Promise.resolve();
  private persistedRevision = -1;
  private flushTimer: ReturnType<typeof setTimeout> | undefined;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;
  private retryDelay = 1000;
  private firstPendingAt: number | null = null;
  private lastChangeAt = 0;
  private waiters: Waiter[] = [];
  private control: SealedEnvelope | null = null;
  private disposed = false;
  readonly key: string;

  constructor(private deps: StoreDeps) {
    this.key = outboxKey(deps.drawingId, deps.identity.tab_id);
    this.snapshot = this.buildSnapshot();
  }

  // ------------------------------------------------------------ subscription
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getSnapshot = () => this.snapshot;

  private emit() {
    this.snapshot = this.buildSnapshot();
    this.listeners.forEach(listener => listener());
  }

  private buildSnapshot(): EditorSnapshot {
    const state = this.state;
    const hasUnsaved = state.pending.length > 0 || state.inflight !== null;
    let saveStatus: SaveStatus = 'saved';
    if (this.paused) saveStatus = 'paused';
    else if (hasUnsaved && this.network === 'offline') saveStatus = 'offline';
    else if (hasUnsaved && this.network === 'retrying') saveStatus = 'retrying';
    else if (hasUnsaved) saveStatus = 'saving';
    const previous = this.snapshot;
    const visible = previous && previous.state.base === state.base && previous.state.inflight === state.inflight
      && previous.state.pending === state.pending ? previous.visible : visibleDocument(state);
    return {
      state, visible, saveStatus, paused: this.paused, storageAvailable: this.storageAvailable,
      lastSavedAt: this.lastSavedAt, hasUnsaved,
      // Stop accepting work that can be neither persisted locally nor sent to the server.
      canEdit: !this.readOnly && !(this.network === 'offline' && !this.storageAvailable) && state.status !== 'COMPLETED',
    };
  }

  private dispatch(action: EditorAction) {
    const before = this.state;
    this.state = annotationReducer(this.state, action);
    if (this.state === before) return;
    if (this.state.pending.length && this.firstPendingAt === null) this.firstPendingAt = Date.now();
    if (!this.state.pending.length) this.firstPendingAt = null;
    this.emit();
    this.schedulePersist();
  }

  // ------------------------------------------------------------ setup
  load(base: DocState, version: Version, status: AnnotationStatus, pending: Operation[] = []) {
    this.paused = null;
    this.dispatch({type: 'load', base, version, status, pending: pending.map(op => ({op, debounce: false}))});
    this.scheduleFlush();
  }

  setReadOnly(readOnly: boolean) {
    this.readOnly = readOnly;
    this.emit();
    if (!readOnly) this.scheduleFlush();
  }

  setLease(lease: LeaseCredentials | null) {
    this.lease = lease;
    if (lease && this.paused?.kind === 'lease') this.resume();
    else if (lease) this.scheduleFlush();
  }

  hasLease() { return this.lease !== null; }
  currentLease() { return this.lease; }

  // ------------------------------------------------------------ editing
  /** Apply a user action. Returns false if it cannot apply to the visible document. */
  edit(forward: Operation[], options: {debounce?: boolean; coalesceKey?: string} = {}): boolean {
    if (!this.snapshot.canEdit || !forward.length) return false;
    const visible = this.snapshot.visible;
    try {
      applyOps(visible, forward);
    } catch {
      return false;
    }
    this.lastChangeAt = Date.now();
    this.dispatch({type: 'local', forward, inverse: invertOps(visible, forward), ...options});
    this.scheduleFlush();
    return true;
  }

  undo() {
    if (!this.snapshot.canEdit) return;
    this.lastChangeAt = Date.now();
    this.dispatch({type: 'undo'});
    this.scheduleFlush();
  }

  redo() {
    if (!this.snapshot.canEdit) return;
    this.lastChangeAt = Date.now();
    this.dispatch({type: 'redo'});
    this.scheduleFlush();
  }

  // ------------------------------------------------------------ local persistence
  private schedulePersist() {
    const run = async () => {
      const revision = this.state.revision;
      const {pending, inflight} = this.state;
      try {
        if (!pending.length && !inflight && !this.control) {
          await this.deps.drafts.remove(this.key);
        } else {
          const sealed: SealedEnvelope | null = inflight
            ? {kind: 'EDIT', mutation_id: inflight.mutation_id, expected_annotation_version: inflight.expected_annotation_version,
              operations: inflight.operations}
            : this.control;
          await this.deps.drafts.put({
            key: this.key, drawingId: this.deps.drawingId, editorId: this.deps.identity.tab_id,
            baseVersion: inflight ? inflight.expected_annotation_version : this.state.baseVersion, sealed,
            pending: pending.map(p => p.op), updatedAt: new Date().toISOString(),
          });
        }
        this.persistedRevision = Math.max(this.persistedRevision, revision);
        if (!this.storageAvailable) {
          this.storageAvailable = true;
          this.emit();
        }
      } catch {
        if (this.storageAvailable) {
          this.storageAvailable = false;
          this.emit();
        }
      }
    };
    this.persistChain = this.persistChain.then(run, run);
  }

  /** Persist a COMPLETE/REOPEN envelope before sending so its unknown outcome can be recovered. */
  async persistControl(envelope: SealedEnvelope | null) {
    this.control = envelope;
    this.schedulePersist();
    await this.persistChain;
  }

  // ------------------------------------------------------------ flushing
  private scheduleFlush() {
    clearTimeout(this.flushTimer);
    if (this.disposed || this.readOnly || this.paused || !this.lease || this.sending || this.state.inflight) return;
    if (!this.state.pending.length) {
      this.settleWaiters();
      return;
    }
    let delay = 0;
    if (this.state.pending.every(p => p.debounce)) {
      const debounce = this.deps.textDebounceMs ?? 600, maxWait = this.deps.textMaxWaitMs ?? 2000;
      const now = Date.now();
      delay = Math.max(0, Math.min(this.lastChangeAt + debounce - now, (this.firstPendingAt ?? now) + maxWait - now));
    }
    this.flushTimer = setTimeout(() => void this.flush(), delay);
  }

  /** Send pending edits now (Ctrl/Cmd+S, navigation, Finish). */
  flushNow() {
    clearTimeout(this.flushTimer);
    if (this.state.inflight && !this.sending && !this.paused && this.lease) void this.send(this.state.inflight);
    else void this.flush();
  }

  private async flush() {
    if (this.disposed || this.readOnly || this.paused || !this.lease || this.sending || this.state.inflight) return;
    if (!this.state.pending.length) {
      this.settleWaiters();
      return;
    }
    this.sending = true;
    try {
      await this.persistChain;  // the local copy must exist before the network send
      if (this.state.inflight || this.paused || !this.lease) return;
      const batch = this.takeBatch();
      this.dispatch({type: 'seal', batch, count: batch.operations.length});
      await this.persistChain;
    } finally {
      this.sending = false;
    }
    if (this.state.inflight) await this.send(this.state.inflight);
  }

  private takeBatch(): SealedBatch {
    const maxOps = this.deps.maxBatchOperations ?? 200, maxBytes = this.deps.maxBatchBytes ?? 1_800_000;
    const operations: Operation[] = [];
    let bytes = 0;
    for (const {op} of this.state.pending) {
      const size = JSON.stringify(op).length;
      if (operations.length && (operations.length >= maxOps || bytes + size > maxBytes)) break;
      operations.push(op);
      bytes += size;
    }
    return {mutation_id: crypto.randomUUID(), expected_annotation_version: this.state.baseVersion, operations};
  }

  private async send(batch: SealedBatch) {
    if (!this.lease || this.sending) return;
    this.sending = true;
    clearTimeout(this.retryTimer);
    try {
      const result = await this.deps.mutate(this.deps.drawingId, batch, this.deps.identity, this.lease);
      this.network = 'ok';
      this.retryDelay = 1000;
      this.lastSavedAt = result.saved_at;
      this.dispatch({type: 'ack', result});
    } catch (error) {
      this.handleSendError(error, batch);
    } finally {
      this.sending = false;
    }
    if (!this.paused && this.network === 'ok') this.scheduleFlush();
  }

  private handleSendError(error: unknown, batch: SealedBatch) {
    const e = error instanceof ApiError ? error : new ApiError(0, 'NETWORK_ERROR', String(error));
    if (e.isRetryable) {
      this.network = e.isNetwork ? 'offline' : 'retrying';
      this.emit();
      this.rejectWaiters(new SaveBlockedError(e.isNetwork ? 'You are offline.' : 'Could not save. Retrying…'));
      const delay = Math.max(this.retryDelay, (e.retryAfterSeconds ?? 0) * 1000);
      this.retryDelay = Math.min(30_000, this.retryDelay * 2);
      // Resend the exact frozen batch; a committed-but-unacknowledged batch comes back as its receipt.
      this.retryTimer = setTimeout(() => void this.send(batch), delay);
      return;
    }
    this.network = 'ok';
    if (e.code === 'LEASE_LOST') {
      this.lease = null;
      this.pause({kind: 'lease', code: e.code, message: 'Editing paused — changes need recovery. Reconnecting your editing session…'});
      this.deps.onLeaseLost?.();
    } else if (e.code === 'DRAWING_COMPLETED') {
      this.pause({kind: 'completed', code: e.code, message: e.message, currentVersion: e.currentVersion});
    } else if (e.status === 409) {
      this.pause({kind: 'conflict', code: e.code, message: e.message, currentVersion: e.currentVersion});
    } else {
      this.pause({kind: 'rejected', code: e.code, message: e.message, details: e.details});
    }
  }

  private pause(reason: PauseReason) {
    this.paused = reason;
    clearTimeout(this.flushTimer);
    this.emit();
    this.rejectWaiters(new SaveBlockedError(reason.message));
  }

  resume() {
    this.paused = null;
    this.emit();
    if (this.state.inflight) void this.send(this.state.inflight);
    else this.scheduleFlush();
  }

  /** Browser came back online: retry immediately instead of waiting for the backoff timer. */
  retryNow() {
    if (this.state.inflight && !this.paused && this.network !== 'ok') {
      clearTimeout(this.retryTimer);
      void this.send(this.state.inflight);
    }
  }

  /** Replace the base after recovery; the chosen local operations become the new queue. */
  rebase(base: DocState, version: Version, status: AnnotationStatus, keep: Operation[]) {
    clearTimeout(this.retryTimer);
    this.network = 'ok';
    this.load(base, version, status, keep);
  }

  /** Read-only refresh of the acknowledged document (no local work may exist). */
  replaceBase(base: DocState, version: Version, status: AnnotationStatus) {
    if (this.snapshot.hasUnsaved) return;
    this.dispatch({type: 'load', base, version, status});
  }

  discardLocal() {
    clearTimeout(this.retryTimer);
    this.network = 'ok';
    this.paused = null;
    this.dispatch({type: 'discardLocal'});
  }

  setStatus(status: AnnotationStatus, version: Version) {
    this.dispatch({type: 'status', status, version});
    this.emit();
  }

  unsavedOperations(): Operation[] {
    return [...(this.state.inflight?.operations ?? []), ...this.state.pending.map((p: PendingOp) => p.op)];
  }

  /** Resolve once every local change is acknowledged; reject if saving is blocked. */
  flushAndWait(): Promise<void> {
    if (!this.snapshot.hasUnsaved) return Promise.resolve();
    if (this.paused) return Promise.reject(new SaveBlockedError(this.paused.message));
    if (!this.lease) return Promise.reject(new SaveBlockedError('Editing is not available right now.'));
    const promise = new Promise<void>((resolve, reject) => this.waiters.push({resolve, reject}));
    this.flushNow();
    return promise;
  }

  private settleWaiters() {
    if (this.snapshot.hasUnsaved) return;
    const waiters = this.waiters;
    this.waiters = [];
    waiters.forEach(w => w.resolve());
  }

  private rejectWaiters(error: Error) {
    const waiters = this.waiters;
    this.waiters = [];
    waiters.forEach(w => w.reject(error));
  }

  async whenPersisted() { await this.persistChain; }

  activate() {
    this.disposed = false;
    this.scheduleFlush();
  }

  dispose() {
    this.disposed = true;
    clearTimeout(this.flushTimer);
    clearTimeout(this.retryTimer);
  }
}
