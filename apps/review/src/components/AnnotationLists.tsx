import { useState } from 'react';
import type { DocState, Group, Region } from '../lib/annotationTypes';

interface Props {
  doc: DocState; pageRegions: Region[]; groups: Group[]; pageNumber: (pageId: string) => number;
  selectedRegionIds: string[]; selectedGroupId: string | null;
  onSelectRegion: (region: Region) => void; onSelectGroup: (group: Group) => void;
}

const MAX_ROWS = 500;

export function AnnotationLists({doc, pageRegions, groups, pageNumber, selectedRegionIds, selectedGroupId, onSelectRegion, onSelectGroup}: Props) {
  const [tab, setTab] = useState<'regions' | 'requirements'>('regions');
  const incompleteRegion = (r: Region) => r.kind !== 'IGNORE' && r.legibility === 'READABLE' && !r.transcription.trim() && !r.symbol_code;
  const memberCount = (g: Group) => Object.values(doc.members).filter(m => m.group_id === g.id);
  const sortedRegions = [...pageRegions].sort((a, b) => a.polygon[0][1] - b.polygon[0][1] || a.polygon[0][0] - b.polygon[0][0]);
  return <section className="lists">
    <div className="tabs" role="tablist">
      <button role="tab" aria-selected={tab === 'regions'} className={tab === 'regions' ? 'active' : ''} onClick={() => setTab('regions')}>
        Text regions <b>{pageRegions.length}</b></button>
      <button role="tab" aria-selected={tab === 'requirements'} className={tab === 'requirements' ? 'active' : ''} onClick={() => setTab('requirements')}>
        Requirements <b>{groups.length}</b></button>
    </div>
    <div className="list-body" role="tabpanel">
      {tab === 'regions' && (sortedRegions.length ? sortedRegions.slice(0, MAX_ROWS).map(r => <button key={r.id}
        className={`list-row ${selectedRegionIds.includes(r.id) ? 'active' : ''}`} onClick={() => onSelectRegion(r)}>
        <span className={`kind-dot kind-${r.kind}`}/>
        <span className="list-text">{r.kind === 'IGNORE' || r.legibility === 'UNREADABLE' ? <em>Unreadable</em>
          : r.transcription || r.symbol_code || <em className="warn">Incomplete label</em>}</span>
        {r.semantic_disposition === 'NON_REQUIREMENT' && <small>not a requirement</small>}
      </button>) : <p className="muted empty">No regions on this page yet. Choose “Text region” and drag a box around text.</p>)}
      {tab === 'regions' && sortedRegions.length > MAX_ROWS && <p className="muted empty">Showing the first {MAX_ROWS}; select on the drawing to see others.</p>}
      {tab === 'requirements' && (groups.length ? groups.map(g => {
        const members = memberCount(g);
        const incomplete = !members.some(m => m.role !== 'CONTEXT');
        const text = members.filter(m => m.role !== 'CONTEXT').sort((a, b) => a.order_index - b.order_index)
          .map(m => doc.regions[m.region_id]?.transcription || doc.regions[m.region_id]?.symbol_code).filter(Boolean).join(' ');
        return <button key={g.id} className={`list-row ${selectedGroupId === g.id ? 'active' : ''}`} onClick={() => onSelectGroup(g)}>
          <span className="badge-inline">{g.display_number ?? '…'}</span>
          <span className="list-text">{text || <em className="warn">Incomplete label</em>}</span>
          <small>{g.requirement_type.replaceAll('_', ' ').toLowerCase()} · p{pageNumber(g.primary_page_id)}</small>
          {incomplete && <small className="warn">incomplete</small>}
        </button>;
      }) : <p className="muted empty">No requirements yet. Select related regions and click “Group as requirement”.</p>)}
    </div>
    {Object.values(doc.regions).some(incompleteRegion) && <p className="list-footer warn">Some regions have an incomplete label.</p>}
  </section>;
}
