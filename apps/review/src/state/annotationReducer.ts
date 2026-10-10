/**
 * Editor document state. Three layers are kept apart:
 *   base      – last document acknowledged by the database, at baseVersion
 *   inflight  – at most one sealed batch whose outcome is pending or unknown
 *   pending   – local unsent operations, in order
 * The visible document is base + inflight + pending. An acknowledgment replaces only the base, so it can
 * never erase edits typed while the request was in flight.
 */
import type { AnnotationStatus, DocState, Operation, Version } from '../lib/annotationTypes';
import type { MutationResult } from '../lib/annotationTypes';
import { applyOps, applyServerResult, coalesce, emptyDoc } from './docOps';

export interface PendingOp { op: Operation; debounce: boolean }
export interface SealedBatch { mutation_id: string; expected_annotation_version: Version; operations: Operation[] }
export interface HistoryEntry { forward: Operation[]; inverse: Operation[]; coalesceKey?: string }

export interface EditorState {
  base: DocState;
  baseVersion: Version;
  status: AnnotationStatus;
  inflight: SealedBatch | null;
  pending: PendingOp[];
  undo: HistoryEntry[];
  redo: HistoryEntry[];
  /** Incremented on every local change; used to know when the local outbox write has caught up. */
  revision: number;
}

export type EditorAction =
  | {type: 'load'; base: DocState; version: Version; status: AnnotationStatus; pending?: PendingOp[]; inflight?: SealedBatch | null}
  | {type: 'local'; forward: Operation[]; inverse: Operation[]; debounce?: boolean; coalesceKey?: string}
  | {type: 'undo'}
  | {type: 'redo'}
  | {type: 'seal'; batch: SealedBatch; count: number}
  | {type: 'ack'; result: MutationResult}
  | {type: 'status'; status: AnnotationStatus; version: Version}
  | {type: 'discardLocal'};

export const initialEditorState = (): EditorState => ({
  base: emptyDoc(), baseVersion: '0', status: 'TO_DO', inflight: null, pending: [], undo: [], redo: [], revision: 0,
});

const MAX_HISTORY = 200;

function enqueue(pending: PendingOp[], ops: Operation[], debounce: boolean): PendingOp[] {
  const next = [...pending];
  for (const op of ops) {
    const merged = coalesce(next[next.length - 1]?.op, op);
    if (merged) next[next.length - 1] = {op: merged, debounce: next[next.length - 1].debounce && debounce};
    else next.push({op, debounce});
  }
  return next;
}

export function annotationReducer(state: EditorState, action: EditorAction): EditorState {
  switch (action.type) {
    case 'load':
      return {...state, base: action.base, baseVersion: action.version, status: action.status,
        pending: action.pending ?? [], inflight: action.inflight ?? null, undo: [], redo: [], revision: state.revision + 1};
    case 'local': {
      if (!action.forward.length) return state;
      const last = state.undo[state.undo.length - 1];
      const undo = action.coalesceKey && last?.coalesceKey === action.coalesceKey
        ? [...state.undo.slice(0, -1), {forward: [...last.forward, ...action.forward], inverse: [...action.inverse, ...last.inverse], coalesceKey: action.coalesceKey}]
        : [...state.undo, {forward: action.forward, inverse: action.inverse, coalesceKey: action.coalesceKey}].slice(-MAX_HISTORY);
      return {...state, pending: enqueue(state.pending, action.forward, !!action.debounce), undo, redo: [], revision: state.revision + 1};
    }
    case 'undo': {
      const entry = state.undo[state.undo.length - 1];
      if (!entry) return state;
      // Undo is a new inverse operation; the server version is never rolled back.
      return {...state, pending: enqueue(state.pending, entry.inverse, false), undo: state.undo.slice(0, -1),
        redo: [...state.redo, entry], revision: state.revision + 1};
    }
    case 'redo': {
      const entry = state.redo[state.redo.length - 1];
      if (!entry) return state;
      return {...state, pending: enqueue(state.pending, entry.forward, false), redo: state.redo.slice(0, -1),
        undo: [...state.undo, {...entry, coalesceKey: undefined}], revision: state.revision + 1};
    }
    case 'seal':
      if (state.inflight) return state;
      return {...state, inflight: action.batch, pending: state.pending.slice(action.count), revision: state.revision + 1};
    case 'ack':
      if (!state.inflight || state.inflight.mutation_id !== action.result.mutation_id) return state;
      return {...state, base: applyServerResult(state.base, action.result), baseVersion: action.result.annotation_version,
        status: action.result.annotation_status, inflight: null, revision: state.revision + 1};
    case 'status':
      return {...state, status: action.status, baseVersion: action.version};
    case 'discardLocal':
      return {...state, pending: [], inflight: null, undo: [], redo: [], revision: state.revision + 1};
  }
}

/** Visible document: acknowledged base plus in-flight and pending local operations. */
export function visibleDocument(state: EditorState): DocState {
  const withInflight = state.inflight ? safeApply(state.base, state.inflight.operations) : state.base;
  return safeApply(withInflight, state.pending.map(p => p.op));
}

function safeApply(doc: DocState, ops: Operation[]): DocState {
  try {
    return applyOps(doc, ops);
  } catch {
    // A queued edit no longer applies (for example after a conflicting reload); apply what does.
    let current = doc;
    for (const op of ops) {
      try { current = applyOps(current, [op]); } catch { /* skipped, surfaced through recovery */ }
    }
    return current;
  }
}
