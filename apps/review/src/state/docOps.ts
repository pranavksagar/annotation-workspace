/** Pure operations on the normalized annotation document; mirrors server semantics for display. */
import type { AnnotationDocument, DocState, Group, Member, MutationResult, Operation, Region } from '../lib/annotationTypes';

export const memberKey = (m: {group_id: string; region_id: string; role: string}) => `${m.group_id}|${m.region_id}|${m.role}`;

export const emptyDoc = (): DocState => ({regions: {}, groups: {}, members: {}});

export function docFromServer(document: AnnotationDocument): DocState {
  const doc = emptyDoc();
  for (const r of document.regions) doc.regions[r.id] = r;
  for (const g of document.groups) doc.groups[g.id] = g;
  for (const m of document.members) doc.members[memberKey(m)] = m;
  return doc;
}

export class OperationConflict extends Error {}

/** Apply operations to a copy of `doc`. Throws OperationConflict if an operation no longer applies. */
export function applyOps(doc: DocState, ops: readonly Operation[]): DocState {
  if (!ops.length) return doc;
  const next: DocState = {regions: {...doc.regions}, groups: {...doc.groups}, members: {...doc.members}};
  for (const op of ops) applyInPlace(next, op);
  return next;
}

function applyInPlace(doc: DocState, op: Operation) {
  switch (op.type) {
    case 'region.create':
      if (doc.regions[op.region.id]) throw new OperationConflict('Region already exists');
      doc.regions[op.region.id] = {origin: 'MANUAL', origin_suggestion_id: null, ...op.region};
      return;
    case 'region.update': {
      const region = doc.regions[op.region_id];
      if (!region) throw new OperationConflict('Region no longer exists');
      doc.regions[op.region_id] = {...region, ...op.changes};
      return;
    }
    case 'region.delete':
      if (!doc.regions[op.region_id]) throw new OperationConflict('Region no longer exists');
      delete doc.regions[op.region_id];
      return;
    case 'group.create':
      if (doc.groups[op.group.id]) throw new OperationConflict('Requirement already exists');
      doc.groups[op.group.id] = {display_number: null, description: null, structured_value: null, ...op.group};
      return;
    case 'group.update': {
      const group = doc.groups[op.group_id];
      if (!group) throw new OperationConflict('Requirement no longer exists');
      doc.groups[op.group_id] = {...group, ...op.changes};
      return;
    }
    case 'group.delete':
      if (!doc.groups[op.group_id]) throw new OperationConflict('Requirement no longer exists');
      delete doc.groups[op.group_id];
      for (const key of Object.keys(doc.members)) if (doc.members[key].group_id === op.group_id) delete doc.members[key];
      return;
    case 'member.add': {
      const key = memberKey(op);
      if (!doc.groups[op.group_id] || !doc.regions[op.region_id]) throw new OperationConflict('Link target no longer exists');
      if (doc.members[key]) throw new OperationConflict('Link already exists');
      doc.members[key] = {group_id: op.group_id, region_id: op.region_id, role: op.role, order_index: op.order_index};
      const region = doc.regions[op.region_id];
      if (op.role !== 'CONTEXT' && region.semantic_disposition === 'NON_REQUIREMENT') {
        doc.regions[op.region_id] = {...region, semantic_disposition: 'UNCLASSIFIED'};
      }
      return;
    }
    case 'member.update': {
      const key = memberKey(op);
      if (!doc.members[key]) throw new OperationConflict('Link no longer exists');
      doc.members[key] = {...doc.members[key], ...op.changes};
      return;
    }
    case 'member.remove': {
      const key = memberKey(op);
      if (!doc.members[key]) throw new OperationConflict('Link no longer exists');
      delete doc.members[key];
      return;
    }
  }
}

/** Inverse operations that undo `ops` when applied after them (computed against the state before `ops`). */
export function invertOps(before: DocState, ops: readonly Operation[]): Operation[] {
  const inverses: Operation[][] = [];
  let state = before;
  for (const op of ops) {
    inverses.push(invertOne(state, op));
    state = applyOps(state, [op]);
  }
  return inverses.reverse().flat();
}

function regionCreate(region: Region): Operation {
  const {id, page_id, kind, polygon, transcription, symbol_code, legibility, reading_angle_degrees, semantic_disposition} = region;
  return {type: 'region.create', region: {id, page_id, kind, polygon, transcription, symbol_code, legibility,
    reading_angle_degrees, semantic_disposition}};
}

function groupCreate(group: Group): Operation {
  return {type: 'group.create', group: {id: group.id, primary_page_id: group.primary_page_id,
    requirement_type: group.requirement_type, description: group.description, structured_value: group.structured_value}};
}

function invertOne(state: DocState, op: Operation): Operation[] {
  switch (op.type) {
    case 'region.create':
      return [{type: 'region.delete', region_id: op.region.id}];
    case 'region.update': {
      const region = state.regions[op.region_id];
      const changes: Record<string, unknown> = {};
      for (const key of Object.keys(op.changes)) changes[key] = (region as unknown as Record<string, unknown>)[key];
      return [{type: 'region.update', region_id: op.region_id, changes}];
    }
    case 'region.delete':
      return [regionCreate(state.regions[op.region_id])];
    case 'group.create':
      return [{type: 'group.delete', group_id: op.group.id}];
    case 'group.update': {
      const group = state.groups[op.group_id];
      const changes: Record<string, unknown> = {};
      for (const key of Object.keys(op.changes)) changes[key] = (group as unknown as Record<string, unknown>)[key];
      return [{type: 'group.update', group_id: op.group_id, changes}];
    }
    case 'group.delete': {
      const members = Object.values(state.members).filter(m => m.group_id === op.group_id);
      return [groupCreate(state.groups[op.group_id]), ...members.map(memberAdd)];
    }
    case 'member.add': {
      const region = state.regions[op.region_id];
      const restore: Operation[] = [{type: 'member.remove', group_id: op.group_id, region_id: op.region_id, role: op.role}];
      if (op.role !== 'CONTEXT' && region?.semantic_disposition === 'NON_REQUIREMENT') {
        restore.push({type: 'region.update', region_id: op.region_id, changes: {semantic_disposition: 'NON_REQUIREMENT'}});
      }
      return restore;
    }
    case 'member.update': {
      const member = state.members[memberKey(op)];
      return [{type: 'member.update', group_id: op.group_id, region_id: op.region_id, role: op.role,
        changes: {order_index: member.order_index}}];
    }
    case 'member.remove':
      return [memberAdd(state.members[memberKey(op)])];
  }
}

const memberAdd = (m: Member): Operation => ({type: 'member.add', group_id: m.group_id, region_id: m.region_id,
  role: m.role, order_index: m.order_index});

/** Reconcile an acknowledged base with the server's canonical changes. */
export function applyServerResult(doc: DocState, result: MutationResult): DocState {
  const next: DocState = {regions: {...doc.regions}, groups: {...doc.groups}, members: {...doc.members}};
  for (const r of result.changed?.regions ?? []) next.regions[r.id] = r;
  for (const g of result.changed?.groups ?? []) next.groups[g.id] = g;
  for (const m of result.changed?.members ?? []) next.members[memberKey(m)] = m;
  for (const m of result.deleted?.members ?? []) delete next.members[memberKey(m)];
  for (const id of result.deleted?.group_ids ?? []) delete next.groups[id];
  for (const id of result.deleted?.region_ids ?? []) delete next.regions[id];
  return next;
}

/** Merge a follow-up update into the previous unsent operation on the same entity (typing coalescing). */
export function coalesce(previous: Operation | undefined, op: Operation): Operation | null {
  if (!previous) return null;
  if (op.type === 'region.update') {
    if (previous.type === 'region.update' && previous.region_id === op.region_id) {
      return {...previous, changes: {...previous.changes, ...op.changes}};
    }
    if (previous.type === 'region.create' && previous.region.id === op.region_id) {
      return {...previous, region: {...previous.region, ...op.changes}};
    }
  }
  if (op.type === 'group.update') {
    if (previous.type === 'group.update' && previous.group_id === op.group_id) {
      return {...previous, changes: {...previous.changes, ...op.changes}};
    }
    if (previous.type === 'group.create' && previous.group.id === op.group_id) {
      return {...previous, group: {...previous.group, ...op.changes}};
    }
  }
  return null;
}

export function describeOperation(op: Operation, doc: DocState): string {
  const regionLabel = (id: string) => {
    const r = doc.regions[id];
    return r ? `${r.kind === 'TEXT' ? 'text' : r.kind === 'SYMBOL' ? 'symbol' : 'unreadable'} region “${r.transcription || r.symbol_code || '…'}”` : 'a region';
  };
  const groupLabel = (id: string) => {
    const g = doc.groups[id];
    return g ? `requirement ${g.display_number ?? '(new)'}` : 'a requirement';
  };
  switch (op.type) {
    case 'region.create': return `Add ${op.region.kind.toLowerCase()} region “${op.region.transcription || op.region.symbol_code || '…'}”`;
    case 'region.update': return `Change ${Object.keys(op.changes).join(', ').replaceAll('_', ' ')} of ${regionLabel(op.region_id)}`;
    case 'region.delete': return `Delete ${regionLabel(op.region_id)}`;
    case 'group.create': return `Add ${op.group.requirement_type.toLowerCase().replaceAll('_', ' ')} requirement`;
    case 'group.update': return `Change ${groupLabel(op.group_id)}`;
    case 'group.delete': return `Delete ${groupLabel(op.group_id)}`;
    case 'member.add': return `Link ${regionLabel(op.region_id)} to ${groupLabel(op.group_id)} as ${op.role.toLowerCase()}`;
    case 'member.update': return `Reorder a link in ${groupLabel(op.group_id)}`;
    case 'member.remove': return `Unlink ${regionLabel(op.region_id)} from ${groupLabel(op.group_id)}`;
  }
}
