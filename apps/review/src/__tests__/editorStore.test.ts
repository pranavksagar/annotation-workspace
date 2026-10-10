import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../lib/annotationApi';
import type { MutationResult, Operation, Region } from '../lib/annotationTypes';
import { localDrafts } from '../lib/localDrafts';
import type { SealedBatch } from '../state/annotationReducer';
import { applyOps, emptyDoc } from '../state/docOps';
import { EditorStore } from '../state/editorStore';

const page = 'page-1';
const region = (id: string, transcription = ''): Region => ({id, page_id: page, kind: 'TEXT', polygon: [[0, 0], [10, 0], [10, 5], [0, 5]],
  transcription, symbol_code: null, legibility: 'READABLE', reading_angle_degrees: 0, semantic_disposition: 'UNCLASSIFIED',
  origin: 'MANUAL', origin_suggestion_id: null});

function serverResult(batch: SealedBatch, version: number): MutationResult {
  // Minimal fake server: echo created/updated regions as canonical changes.
  const after = applyOps(emptyDoc(), batch.operations.filter(op => op.type === 'region.create'));
  const changed = Object.values(after.regions);
  for (const op of batch.operations) if (op.type === 'region.update') changed.push({...region(op.region_id), ...op.changes} as Region);
  return {mutation_id: batch.mutation_id, operation_kind: 'EDIT', previous_annotation_version: String(version - 1),
    annotation_version: String(version), annotation_status: 'IN_PROGRESS', saved_at: new Date().toISOString(),
    changed: {regions: changed, groups: [], members: []}, deleted: {region_ids: [], group_ids: [], members: []}};
}

function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return {promise, resolve, reject};
}

const create = (id: string, transcription = ''): Operation => ({type: 'region.create', region: {...region(id, transcription)}});
const type = (id: string, transcription: string): Operation => ({type: 'region.update', region_id: id, changes: {transcription}});

describe('EditorStore autosave', () => {
  let calls: SealedBatch[];
  beforeEach(() => { calls = []; localDrafts.reset(); });

  function makeStore(mutate: (batch: SealedBatch) => Promise<MutationResult>) {
    const store = new EditorStore({drawingId: crypto.randomUUID(), identity: {browser_id: 'b', tab_id: crypto.randomUUID()},
      drafts: localDrafts, textDebounceMs: 5, textMaxWaitMs: 20,
      mutate: (_d, batch) => { calls.push(structuredClone(batch)); return mutate(batch); }});
    store.load(emptyDoc(), '0', 'TO_DO');
    store.setReadOnly(false);
    store.setLease({lease_token: 'token-token-token-1', lease_generation: '1'});
    return store;
  }

  it('keeps only one batch in flight and an acknowledgment never erases newer typing', async () => {
    const first = deferred<MutationResult>();
    const store = makeStore(batch => calls.length === 1 ? first.promise : Promise.resolve(serverResult(batch, 2)));
    store.edit([create('r1')]);
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    store.edit([type('r1', 'Ø2')], {debounce: true, coalesceKey: 'text:r1'});
    store.edit([type('r1', 'Ø20')], {debounce: true, coalesceKey: 'text:r1'});
    await new Promise(r => setTimeout(r, 40));
    expect(calls).toHaveLength(1);  // the second batch waits for the first acknowledgment
    first.resolve(serverResult(calls[0], 1));
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(store.getSnapshot().visible.regions.r1.transcription).toBe('Ø20');
    expect(calls[1].expected_annotation_version).toBe('1');
    expect(calls[1].operations).toEqual([type('r1', 'Ø20')]);  // typing coalesced into one update
    await vi.waitFor(() => expect(store.getSnapshot().saveStatus).toBe('saved'));
    expect(store.getSnapshot().state.baseVersion).toBe('2');
  });

  it('resends the exact frozen batch after an unknown network outcome', async () => {
    let attempt = 0;
    const store = makeStore(batch => ++attempt === 1 ? Promise.reject(new ApiError(0, 'NETWORK_ERROR', 'offline'))
      : Promise.resolve(serverResult(batch, 1)));
    store.edit([create('r1', 'A')]);
    await vi.waitFor(() => expect(store.getSnapshot().saveStatus).toBe('offline'));
    store.edit([type('r1', 'AB')]);  // newer edit stays queued behind the frozen batch
    store.retryNow();
    await vi.waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(3));
    expect(calls[1].mutation_id).toBe(calls[0].mutation_id);
    expect(calls[1].operations).toEqual(calls[0].operations);
    expect(calls[2].operations).toEqual([type('r1', 'AB')]);
  });

  it('pauses on a lost lease and keeps changes; resumes with a new lease', async () => {
    const onLost = vi.fn();
    let attempt = 0;
    const store = makeStore(batch => ++attempt === 1 ? Promise.reject(new ApiError(423, 'LEASE_LOST', 'lost'))
      : Promise.resolve(serverResult(batch, 1)));
    (store as unknown as {deps: {onLeaseLost: () => void}}).deps.onLeaseLost = onLost;
    store.edit([create('r1', 'X')]);
    await vi.waitFor(() => expect(store.getSnapshot().saveStatus).toBe('paused'));
    expect(onLost).toHaveBeenCalled();
    expect(store.getSnapshot().visible.regions.r1).toBeDefined();
    store.setLease({lease_token: 'token-token-token-2', lease_generation: '2'});
    await vi.waitFor(() => expect(store.getSnapshot().saveStatus).toBe('saved'));
    expect(calls[1].mutation_id).toBe(calls[0].mutation_id);
  });

  it('persists the outbox before sending and clears it after acknowledgment', async () => {
    const gate = deferred<MutationResult>();
    const store = makeStore(() => gate.promise);
    store.edit([create('r1', 'T')]);
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    const records = await localDrafts.listForDrawing((store as unknown as {deps: {drawingId: string}}).deps.drawingId);
    expect(records).toHaveLength(1);
    expect(records[0].sealed?.mutation_id).toBe(calls[0].mutation_id);
    expect(JSON.stringify(records[0])).not.toContain('token-token');
    gate.resolve(serverResult(calls[0], 1));
    await vi.waitFor(async () => expect(await localDrafts.listForDrawing(records[0].drawingId)).toHaveLength(0));
  });

  it('undo creates an inverse operation instead of rolling back the version', async () => {
    const store = makeStore(batch => Promise.resolve(serverResult(batch, calls.length)));
    store.edit([create('r1', 'A')]);
    await vi.waitFor(() => expect(store.getSnapshot().saveStatus).toBe('saved'));
    store.edit([type('r1', 'B')]);
    await vi.waitFor(() => expect(store.getSnapshot().visible.regions.r1.transcription).toBe('B'));
    store.undo();
    expect(store.getSnapshot().visible.regions.r1.transcription).toBe('A');
    await vi.waitFor(() => expect(calls.at(-1)?.operations).toEqual([type('r1', 'A')]));
  });

  it('flushAndWait rejects when saving is blocked', async () => {
    const store = makeStore(() => Promise.reject(new ApiError(409, 'VERSION_CONFLICT', 'changed', [], '5')));
    store.edit([create('r1', 'A')]);
    await expect(store.flushAndWait()).rejects.toThrow();
    expect(store.getSnapshot().paused?.kind).toBe('conflict');
  });
});
