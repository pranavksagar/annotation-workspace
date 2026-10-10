import { useState } from 'react';
import type { CompletionIssue, Page } from '../lib/annotationTypes';
import { Dialog } from './Dialog';

interface Props {
  pages: Page[];
  emptyPageIds: string[];
  busy: boolean;
  error: string | null;
  issues: CompletionIssue[];
  onConfirm: (emptyConfirmed: string[]) => void;
  onShowIssue: (issue: CompletionIssue) => void;
  onClose: () => void;
}

export function FinishDialog({pages, emptyPageIds, busy, error, issues, onConfirm, onShowIssue, onClose}: Props) {
  const [confirmed, setConfirmed] = useState<Set<string>>(new Set());
  const number = (id?: string) => pages.find(p => p.id === id)?.page_number;
  const ready = emptyPageIds.every(id => confirmed.has(id));
  return <Dialog title="Finish drawing" onClose={busy ? () => undefined : onClose} actions={<>
    <button className="secondary" onClick={onClose} disabled={busy}>Keep editing</button>
    <button className="primary" disabled={busy || !ready} onClick={() => onConfirm([...confirmed])}>
      {busy ? 'Saving and finishing…' : 'Finish drawing'}
    </button>
  </>}>
    <p>Finish confirms that every page has been annotated. Mark unreadable text explicitly.</p>
    {emptyPageIds.length > 0 && <div className="empty-pages">
      <p><strong>{emptyPageIds.length === 1 ? 'One page has' : `${emptyPageIds.length} pages have`} no labels.</strong> Confirm only if the page really contains no labelable text or symbols.</p>
      {emptyPageIds.map(id => <label className="check" key={id}>
        <input type="checkbox" checked={confirmed.has(id)} onChange={e => setConfirmed(s => {
          const next = new Set(s);
          if (e.target.checked) next.add(id); else next.delete(id);
          return next;
        })}/>
        Page {number(id)} contains no labelable text or symbols
      </label>)}
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
    {issues.length > 0 && <div className="issues">
      <p><strong>{issues.length} label{issues.length === 1 ? ' needs' : 's need'} attention before finishing:</strong></p>
      <ul>
        {issues.slice(0, 100).map((issue, i) => <li key={i}>
          <span>{issue.page_id && pages.length > 1 ? `Page ${number(issue.page_id)}: ` : ''}{issue.message}</span>
          {(issue.region_id || issue.group_id || issue.page_id) && <button className="link" onClick={() => onShowIssue(issue)}>Show</button>}
        </li>)}
      </ul>
    </div>}
  </Dialog>;
}
