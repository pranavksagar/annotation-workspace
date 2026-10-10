/**
 * Reload/reconnect recovery for same-browser outboxes.
 * A sealed batch with an unknown outcome is resolved by its receipt before anything is resent. A committed
 * receipt is consumed (never re-sent, never applied over a newer document); an uncommitted batch returns to
 * the local queue. Separate abandoned editor instances stay separate.
 */
import { api } from './annotationApi';
import type { Operation, Version } from './annotationTypes';
import { localDrafts, type OutboxRecord } from './localDrafts';

export interface RecoveredDraft {
  key: string;
  baseVersion: Version;
  operations: Operation[];
  updatedAt: string;
}

export interface RecoveryResult {
  drafts: RecoveredDraft[];
  storageAvailable: boolean;
  /** A COMPLETE envelope from this browser was committed (its response was lost). */
  completedByReceipt: boolean;
}

export async function resolveOutboxes(drawingId: string, ownKey: string): Promise<RecoveryResult> {
  let records: OutboxRecord[];
  try {
    records = await localDrafts.listForDrawing(drawingId);
  } catch {
    return {drafts: [], storageAvailable: false, completedByReceipt: false};
  }
  const drafts: RecoveredDraft[] = [];
  let completedByReceipt = false;
  for (const record of records) {
    if (record.key === ownKey) continue;
    let baseVersion = record.baseVersion;
    let operations = record.pending;
    if (record.sealed) {
      const receipt = await api.receipt(drawingId, record.sealed.mutation_id);  // network errors propagate: retry later
      if (receipt) {
        baseVersion = receipt.resulting_version;
        if (receipt.operation_kind === 'COMPLETE') completedByReceipt = true;
      } else if (record.sealed.kind === 'EDIT') {
        operations = [...(record.sealed.operations ?? []), ...operations];
        baseVersion = record.sealed.expected_annotation_version;
      }
      // An uncommitted COMPLETE/REOPEN is dropped; the engineer can press the button again.
    }
    if (!operations.length) {
      await localDrafts.remove(record.key).catch(() => undefined);
      continue;
    }
    if (baseVersion !== record.baseVersion || record.sealed) {
      await localDrafts.put({...record, baseVersion, sealed: null, pending: operations}).catch(() => undefined);
    }
    drafts.push({key: record.key, baseVersion, operations, updatedAt: record.updatedAt});
  }
  return {drafts, storageAvailable: true, completedByReceipt};
}
