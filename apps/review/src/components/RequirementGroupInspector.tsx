import { ArrowDown, ArrowUp, Link2, Trash2, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { DocState, Group, Member, Role, StructuredValue, Vocabulary } from '../lib/annotationTypes';

interface Props {
  group: Group; doc: DocState; vocabulary: Vocabulary; readOnly: boolean; pickMode: boolean;
  pageNumber: (pageId: string) => number;
  onChange: (changes: Partial<Pick<Group, 'requirement_type' | 'description' | 'structured_value'>>, typing?: boolean) => void;
  onRoleChange: (member: Member, role: Role) => void;
  onMove: (member: Member, direction: -1 | 1) => void;
  onRemove: (member: Member) => void;
  onPickMode: (on: boolean) => void;
  onSelectRegion: (regionId: string) => void;
  onDelete: () => void;
}

const DECIMAL = /^[+-]?[0-9]+(\.[0-9]+)?$/;
const FIELDS: [keyof StructuredValue, string, boolean, number][] = [
  ['nominal', 'Nominal', true, 40], ['upper_tolerance', 'Upper tolerance', true, 40],
  ['lower_tolerance', 'Lower tolerance', true, 40], ['unit', 'Unit', false, 32], ['note', 'Note', false, 500]];

/** Keeps an in-progress value locally; only valid values (or empty = unknown) are saved. */
function StructuredField({label, value, decimal, maxLength, disabled, onCommit}: {
  label: string; value: string; decimal: boolean; maxLength: number; disabled: boolean; onCommit: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const valid = !decimal || draft === '' || DECIMAL.test(draft);
  return <label className="field inline">
    <span>{label}</span>
    <input value={draft} disabled={disabled} maxLength={maxLength} inputMode={decimal ? 'decimal' : 'text'} aria-invalid={!valid}
      onChange={e => {
        const next = e.target.value.trim();
        setDraft(next);
        if (!decimal || next === '' || DECIMAL.test(next)) onCommit(next);
      }}/>
  </label>;
}

export function RequirementGroupInspector(p: Props) {
  const {group, doc, vocabulary, readOnly} = p;
  const members = Object.values(doc.members).filter(m => m.group_id === group.id)
    .sort((a, b) => a.order_index - b.order_index || a.region_id.localeCompare(b.region_id));
  const incomplete = !members.some(m => m.role !== 'CONTEXT');
  const value = group.structured_value ?? {};
  const setValue = (key: keyof StructuredValue, raw: string) => {
    // Empty means unknown (null). A zero tolerance is never inferred from an empty field.
    const next = {...value, [key]: raw === '' ? null : raw};
    const empty = Object.values(next).every(v => v === null || v === undefined);
    p.onChange({structured_value: empty ? null : next}, true);
  };

  return <section className="inspector-section" aria-label="Selected requirement">
    <header className="inspector-header">
      <h2><span className="badge-inline">{group.display_number ?? '…'}</span>Requirement</h2>
      {incomplete && <span className="chip warn">Incomplete label</span>}
    </header>
    <label className="field">
      <span>Requirement type</span>
      <select value={group.requirement_type} disabled={readOnly} onChange={e => p.onChange({requirement_type: e.target.value as Group['requirement_type']})}>
        {vocabulary.requirement_types.map(t => <option key={t} value={t}>{t.replaceAll('_', ' ').toLowerCase()}</option>)}
      </select>
    </label>
    <label className="field">
      <span>Description (optional)</span>
      <textarea rows={2} maxLength={2000} value={group.description ?? ''} disabled={readOnly}
        onChange={e => p.onChange({description: e.target.value || null}, true)}/>
    </label>
    <div className="members">
      <h3>Linked regions</h3>
      {members.length === 0 && <p className="muted">No regions yet. Use “Link regions” and click regions on the drawing.</p>}
      {members.map((m, index) => {
        const region = doc.regions[m.region_id];
        const otherPage = region && region.page_id !== group.primary_page_id;
        return <div className="member-row" key={`${m.region_id}:${m.role}`}>
          <button className="member-text" onClick={() => p.onSelectRegion(m.region_id)} title="Show region">
            {region?.transcription || region?.symbol_code || <em>Incomplete label</em>}
            {otherPage && <small> · page {p.pageNumber(region.page_id)}</small>}
          </button>
          <select aria-label="Role" value={m.role} disabled={readOnly} onChange={e => p.onRoleChange(m, e.target.value as Role)}>
            {vocabulary.member_roles.map(r => <option key={r} value={r} disabled={otherPage && r !== 'CONTEXT'}>{r.replaceAll('_', ' ').toLowerCase()}</option>)}
          </select>
          {!readOnly && <>
            <button aria-label="Move up" disabled={index === 0} onClick={() => p.onMove(m, -1)}><ArrowUp size={13}/></button>
            <button aria-label="Move down" disabled={index === members.length - 1} onClick={() => p.onMove(m, 1)}><ArrowDown size={13}/></button>
            <button aria-label="Remove link" onClick={() => p.onRemove(m)}><X size={13}/></button>
          </>}
        </div>;
      })}
      {!readOnly && <button className={`secondary ${p.pickMode ? 'active' : ''}`} onClick={() => p.onPickMode(!p.pickMode)}>
        <Link2 size={14}/>{p.pickMode ? 'Done linking' : 'Link regions'}
      </button>}
    </div>
    <details className="structured">
      <summary>Structured values (optional)</summary>
      <p className="muted">Decimal strings with an explicit unit. Leave unknown values empty.</p>
      {FIELDS.map(([key, label, decimal, maxLength]) => <StructuredField key={key} label={label} decimal={decimal}
        maxLength={maxLength} value={value[key] ?? ''} disabled={readOnly} onCommit={next => setValue(key, next)}/>)}
      <p className="muted small">Tolerances use plain decimals such as +0.25 or 0; invalid entries are not saved.</p>
    </details>
    {!readOnly && <button className="danger-link" onClick={p.onDelete}><Trash2 size={14}/>Delete requirement (keeps its text regions)</button>}
  </section>;
}
