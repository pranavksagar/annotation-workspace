import { describe, expect, it } from 'vitest';
import type { Operation } from '../lib/annotationTypes';
import { applyOps, emptyDoc, invertOps } from '../state/docOps';
import { parseRoute } from '../App';

const region = (id: string, extra = {}) => ({type: 'region.create', region: {id, page_id: 'p1', kind: 'TEXT',
  polygon: [[0, 0], [10, 0], [10, 5], [0, 5]], transcription: 'Ø20', symbol_code: null, legibility: 'READABLE',
  reading_angle_degrees: 0, semantic_disposition: 'UNCLASSIFIED', ...extra}}) as Operation;

describe('document operations', () => {
  const base = applyOps(emptyDoc(), [region('r1', {semantic_disposition: 'NON_REQUIREMENT'}), region('r2'),
    {type: 'group.create', group: {id: 'g1', primary_page_id: 'p1', requirement_type: 'DIMENSION'}},
    {type: 'member.add', group_id: 'g1', region_id: 'r2', role: 'NOMINAL', order_index: 0}]);

  it('adding a non-context member clears NON_REQUIREMENT, and undo restores it', () => {
    const op: Operation = {type: 'member.add', group_id: 'g1', region_id: 'r1', role: 'PRIMARY', order_index: 1};
    const after = applyOps(base, [op]);
    expect(after.regions.r1.semantic_disposition).toBe('UNCLASSIFIED');
    const undone = applyOps(after, invertOps(base, [op]));
    expect(undone.regions.r1.semantic_disposition).toBe('NON_REQUIREMENT');
    expect(Object.keys(undone.members)).toEqual(Object.keys(base.members));
  });

  it('deleting a group keeps its regions; undo recreates the group and its links', () => {
    const op: Operation = {type: 'group.delete', group_id: 'g1'};
    const after = applyOps(base, [op]);
    expect(after.groups.g1).toBeUndefined();
    expect(after.regions.r2).toBeDefined();
    expect(Object.keys(after.members)).toHaveLength(0);
    const undone = applyOps(after, invertOps(base, [op]));
    expect(undone.groups.g1.requirement_type).toBe('DIMENSION');
    expect(Object.values(undone.members)).toHaveLength(1);
  });

  it('region deletion with its links removed inverts back to the original', () => {
    const ops: Operation[] = [{type: 'member.remove', group_id: 'g1', region_id: 'r2', role: 'NOMINAL'},
      {type: 'region.delete', region_id: 'r2'}];
    const after = applyOps(base, ops);
    const undone = applyOps(after, invertOps(base, ops));
    expect(undone.regions.r2).toEqual(base.regions.r2);
    expect(undone.members).toEqual(base.members);
  });

  it('operations that no longer apply throw instead of silently changing other data', () => {
    expect(() => applyOps(emptyDoc(), [{type: 'region.update', region_id: 'missing', changes: {transcription: 'x'}}])).toThrow();
  });
});

describe('routing', () => {
  it('library is the default route and drawings use hash routes', () => {
    expect(parseRoute('')).toEqual({name: 'drawings'});
    expect(parseRoute('#/drawings')).toEqual({name: 'drawings'});
    expect(parseRoute('#/drawings/86A3E5F2-1111-4111-8111-123456789ABC')).toEqual({name: 'drawing', id: '86a3e5f2-1111-4111-8111-123456789abc'});
  });
});
