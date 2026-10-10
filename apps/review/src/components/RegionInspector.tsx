import { Link2, Trash2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { DocState, Group, Region, RegionFields, Role, Vocabulary } from '../lib/annotationTypes';

interface Props {
  region: Region; doc: DocState; vocabulary: Vocabulary; readOnly: boolean; pageNumber: (pageId: string) => number;
  focusNonce?: number;
  reading?: boolean;
  onChange: (changes: Partial<RegionFields>, typing?: boolean) => void;
  onOpenGroup: (groupId: string) => void;
  onLink: (groupId: string, role: Role) => void;
  onDelete: () => void;
}

export function RegionInspector({region, doc, vocabulary, readOnly, pageNumber, focusNonce, reading, onChange, onOpenGroup, onLink, onDelete}: Props) {
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { if (focusNonce) input.current?.focus(); }, [focusNonce]);
  const [linkGroup, setLinkGroup] = useState('');
  const [linkRole, setLinkRole] = useState<Role>('PRIMARY');
  const memberships = Object.values(doc.members).filter(m => m.region_id === region.id);
  const positive = memberships.some(m => m.role !== 'CONTEXT');
  const groups = Object.values(doc.groups).sort((a, b) => (a.display_number ?? 1e9) - (b.display_number ?? 1e9));
  const unreadable = region.kind === 'IGNORE' || region.legibility === 'UNREADABLE';
  const incomplete = !unreadable && !region.transcription.trim() && !(region.kind === 'SYMBOL' && region.symbol_code);

  const insert = (character: string) => {
    const el = input.current;
    const start = el?.selectionStart ?? region.transcription.length, end = el?.selectionEnd ?? start;
    onChange({transcription: region.transcription.slice(0, start) + character + region.transcription.slice(end)}, true);
    requestAnimationFrame(() => { el?.focus(); el?.setSelectionRange(start + character.length, start + character.length); });
  };
  const setKind = (kind: Region['kind']) => {
    if (kind === 'IGNORE') onChange({kind, legibility: 'UNREADABLE', transcription: '', symbol_code: null});
    else if (kind === 'TEXT') onChange({kind, symbol_code: null, ...(region.kind === 'IGNORE' ? {legibility: 'READABLE'} : {})});
    else onChange({kind, ...(region.kind === 'IGNORE' ? {legibility: 'READABLE'} : {})});
  };
  const groupLabel = (g: Group) => `Requirement ${g.display_number ?? '(saving…)'} · ${g.requirement_type.replaceAll('_', ' ').toLowerCase()}`;

  return <section className="inspector-section" aria-label="Selected region">
    <header className="inspector-header">
      <h2>{region.kind === 'TEXT' ? 'Text region' : region.kind === 'SYMBOL' ? 'Symbol region' : 'Unreadable region'}</h2>
      {incomplete && <span className="chip warn">Incomplete label</span>}
    </header>
    <fieldset className="segmented" disabled={readOnly}>
      <legend>Region type</legend>
      {(['TEXT', 'SYMBOL', 'IGNORE'] as const).map(kind => <label key={kind} className={region.kind === kind ? 'active' : ''}>
        <input type="radio" name="kind" checked={region.kind === kind} onChange={() => setKind(kind)}/>
        {kind === 'TEXT' ? 'Text' : kind === 'SYMBOL' ? 'Symbol' : 'Unreadable'}
      </label>)}
    </fieldset>
    {region.kind !== 'IGNORE' && <>
      <label className="field">
        <span>Transcription — type exactly what the drawing shows</span>
        <input ref={input} data-autofocus="transcription" className="transcription" value={region.transcription} disabled={readOnly || unreadable}
          spellCheck={false} autoComplete="off" maxLength={4000} placeholder={unreadable ? 'Unreadable — no transcription' : 'e.g. Ø20 +0.25'}
          onChange={e => onChange({transcription: e.target.value}, true)}/>
      </label>
      {reading && !region.transcription && <p className="suggestion" role="status">Reading text…</p>}
      {!readOnly && !unreadable && <div className="palette" aria-label="Insert character">
        {vocabulary.text_characters.map(c => <button key={c.character} type="button" title={c.label} onClick={() => insert(c.character)}>{c.character}</button>)}
      </div>}
      {region.kind === 'SYMBOL' && <label className="field">
        <span>Symbol code</span>
        <select value={region.symbol_code ?? ''} disabled={readOnly || unreadable}
          onChange={e => onChange({symbol_code: e.target.value || null})}>
          <option value="">— none —</option>
          {vocabulary.symbol_codes.map(s => <option key={s.code} value={s.code}>{s.unicode ? `${s.unicode}  ` : ''}{s.label}</option>)}
        </select>
      </label>}
      <label className="check">
        <input type="checkbox" checked={region.legibility === 'UNREADABLE'} disabled={readOnly}
          onChange={e => onChange(e.target.checked ? {legibility: 'UNREADABLE', transcription: '', symbol_code: null} : {legibility: 'READABLE'})}/>
        Cannot be read reliably (no characters are guessed)
      </label>
    </>}
    <label className="field inline">
      <span>Reading angle</span>
      <input type="number" step={1} value={Math.round(region.reading_angle_degrees * 100) / 100} disabled={readOnly}
        onChange={e => Number.isFinite(e.target.valueAsNumber) && onChange({reading_angle_degrees: e.target.valueAsNumber})}/>
      <span>°</span>
    </label>
    {!positive && region.kind !== 'IGNORE' && <fieldset className="segmented small" disabled={readOnly}>
      <legend>Outside requirements</legend>
      {([['UNCLASSIFIED', 'Unclassified'], ['NON_REQUIREMENT', 'Not a requirement'], ['SUPPORTING_CONTEXT', 'Supporting context']] as const)
        .map(([value, label]) => <label key={value} className={region.semantic_disposition === value ? 'active' : ''}>
          <input type="radio" name="disposition" checked={region.semantic_disposition === value}
            onChange={() => onChange({semantic_disposition: value})}/>{label}
        </label>)}
    </fieldset>}
    <div className="linked">
      <h3>Linked requirements</h3>
      {memberships.length === 0 && <p className="muted">Not part of a requirement.</p>}
      {memberships.map(m => {
        const g = doc.groups[m.group_id];
        return g && <button key={`${m.group_id}:${m.role}`} className="link-row" onClick={() => onOpenGroup(g.id)}>
          <b>{g.display_number ?? '…'}</b>{g.requirement_type.replaceAll('_', ' ').toLowerCase()}<small>{m.role.replaceAll('_', ' ').toLowerCase()}</small>
        </button>;
      })}
      {!readOnly && groups.length > 0 && <div className="link-form">
        <select aria-label="Requirement" value={linkGroup} onChange={e => setLinkGroup(e.target.value)}>
          <option value="">Add to requirement…</option>
          {groups.map(g => <option key={g.id} value={g.id}>{groupLabel(g)}{g.primary_page_id !== region.page_id ? ` (page ${pageNumber(g.primary_page_id)})` : ''}</option>)}
        </select>
        <select aria-label="Role" value={linkRole} onChange={e => setLinkRole(e.target.value as Role)}>
          {vocabulary.member_roles.map(r => <option key={r} value={r}>{r.replaceAll('_', ' ').toLowerCase()}</option>)}
        </select>
        <button disabled={!linkGroup} onClick={() => { onLink(linkGroup, linkRole); setLinkGroup(''); }}><Link2 size={14}/>Link</button>
      </div>}
    </div>
    {!readOnly && <button className="danger-link" onClick={onDelete}><Trash2 size={14}/>Delete region</button>}
  </section>;
}
