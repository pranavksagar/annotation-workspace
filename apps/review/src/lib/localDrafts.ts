/**
 * IndexedDB outbox: a recovery aid alongside server persistence, never a substitute for it.
 * One record per (drawing, editor instance). Lease tokens are never stored here.
 */
import type { Operation, SealedEnvelope, Version } from './annotationTypes';

export interface OutboxRecord {
  key: string;
  drawingId: string;
  editorId: string;
  /** Version that the sealed batch (or, if none, the first pending operation) expects. */
  baseVersion: Version;
  sealed: SealedEnvelope | null;
  pending: Operation[];
  updatedAt: string;
}

const DB_NAME = 'annotation-workspace';
const STORE = 'outboxes';

export class LocalStorageUnavailable extends Error {}

let dbPromise: Promise<IDBDatabase> | null = null;

function open(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined') {
      reject(new LocalStorageUnavailable('IndexedDB is not available in this browser.'));
      return;
    }
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.open(DB_NAME, 1);
    } catch (error) {
      reject(new LocalStorageUnavailable(String(error)));
      return;
    }
    request.onupgradeneeded = () => {
      const store = request.result.createObjectStore(STORE, {keyPath: 'key'});
      store.createIndex('drawingId', 'drawingId');
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new LocalStorageUnavailable(request.error?.message ?? 'IndexedDB could not be opened.'));
    request.onblocked = () => reject(new LocalStorageUnavailable('IndexedDB is blocked by another tab.'));
  });
  dbPromise.catch(() => { dbPromise = null; });
  return dbPromise;
}

function run<T>(mode: IDBTransactionMode, body: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return open().then(db => new Promise<T>((resolve, reject) => {
    let tx: IDBTransaction;
    try {
      tx = db.transaction(STORE, mode);
    } catch (error) {
      reject(new LocalStorageUnavailable(String(error)));
      return;
    }
    const request = body(tx.objectStore(STORE));
    tx.oncomplete = () => resolve(request.result);
    tx.onerror = () => reject(new LocalStorageUnavailable(tx.error?.message ?? 'Could not write to browser storage.'));
    tx.onabort = () => reject(new LocalStorageUnavailable(tx.error?.message ?? 'Browser storage quota exceeded.'));
  }));
}

export const outboxKey = (drawingId: string, editorId: string) => `${drawingId}:${editorId}`;

export const localDrafts = {
  async put(record: OutboxRecord): Promise<void> {
    await run('readwrite', store => store.put(record));
  },
  async remove(key: string): Promise<void> {
    await run('readwrite', store => store.delete(key));
  },
  async listForDrawing(drawingId: string): Promise<OutboxRecord[]> {
    const records = await run<OutboxRecord[]>('readonly', store => store.index('drawingId').getAll(drawingId) as IDBRequest<OutboxRecord[]>);
    return records.sort((a, b) => a.updatedAt.localeCompare(b.updatedAt));
  },
  /** Test hook: forget the cached connection. */
  reset() { dbPromise = null; },
};

export type LocalDrafts = typeof localDrafts;
