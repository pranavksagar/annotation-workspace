import { FileImage, Search } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { UploadDrawing, type UploadOutcome } from '../components/UploadDrawing';
import { api, assetUrl } from '../lib/annotationApi';
import type { AnnotationStatus, LibraryItem, LibraryPage } from '../lib/annotationTypes';
import { relativeTime, STATUS_LABEL } from '../lib/format';

type Filter = 'ALL' | AnnotationStatus;
const FILTERS: Filter[] = ['ALL', 'TO_DO', 'IN_PROGRESS', 'COMPLETED'];

export function DrawingsPage({onOpen}: {onOpen: (drawingId: string) => void}) {
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [filter, setFilter] = useState<Filter>('ALL');
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [data, setData] = useState<LibraryPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<UploadOutcome | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const cursor = cursors[cursors.length - 1];

  useEffect(() => {
    const t = setTimeout(() => { setDebounced(query.trim()); setCursors([null]); }, 300);
    return () => clearTimeout(t);
  }, [query]);

  const load = useCallback(async () => {
    clearTimeout(timer.current);
    let preparing = false;
    try {
      const page = await api.listDrawings({q: debounced, status: filter === 'ALL' ? undefined : filter, cursor});
      setData(page);
      setError(null);
      preparing = page.items.some(i => i.preparation_state === 'QUEUED' || i.preparation_state === 'RUNNING');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The library could not be loaded.');
    }
    // Refresh every 15 s while visible; every 2 s while something is being prepared.
    if (document.visibilityState === 'visible') timer.current = setTimeout(() => void load(), preparing ? 2000 : error ? 30_000 : 15_000);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced, filter, cursor]);

  useEffect(() => {
    void load();
    const visible = () => { if (document.visibilityState === 'visible') void load(); else clearTimeout(timer.current); };
    document.addEventListener('visibilitychange', visible);
    return () => { clearTimeout(timer.current); document.removeEventListener('visibilitychange', visible); };
  }, [load]);

  const uploaded = (outcome: UploadOutcome) => {
    setNotice(outcome);
    setFilter('ALL');
    setQuery('');
    setCursors([null]);
    void load();
  };

  return <main className="library">
    <header className="library-header">
      <div>
        <h1>Drawings</h1>
        <p className="muted">Shared library. Anyone with access can upload and annotate drawings.</p>
      </div>
      <UploadDrawing onUploaded={uploaded}/>
    </header>
    {notice && <div className={`notice ${notice.duplicate ? 'info' : 'ok'}`} role="status">
      <span>{notice.duplicate ? 'This drawing is already in the library' : `Uploaded ${notice.filename}. Preparing pages…`}</span>
      <button className="link" onClick={() => onOpen(notice.drawingId)}>Open</button>
      <button className="link muted" aria-label="Dismiss" onClick={() => setNotice(null)}>×</button>
    </div>}
    <div className="library-controls">
      <label className="search">
        <Search size={16}/>
        <input type="search" placeholder="Search by filename" value={query} onChange={e => setQuery(e.target.value)} aria-label="Search by filename"/>
      </label>
      <div className="filters" role="tablist" aria-label="Workflow status">
        {FILTERS.map(f => <button key={f} role="tab" aria-selected={filter === f} className={filter === f ? 'active' : ''}
          onClick={() => { setFilter(f); setCursors([null]); }}>
          {f === 'ALL' ? 'All' : STATUS_LABEL[f]} <b>{data?.counts[f] ?? '–'}</b>
        </button>)}
      </div>
    </div>
    {error && <p className="error" role="alert">{error} Retrying…</p>}
    <div className="drawing-table" role="table" aria-label="Drawings">
      <div className="drawing-row head" role="row">
        <span role="columnheader"/><span role="columnheader">Filename</span><span role="columnheader">Pages</span>
        <span role="columnheader">Status</span><span role="columnheader">Last annotation update</span><span role="columnheader"/>
      </div>
      {data?.items.map(item => <DrawingRow key={item.id} item={item} onOpen={onOpen}/>)}
      {data && !data.items.length && <p className="muted empty">{debounced || filter !== 'ALL' ? 'No drawings match.' : 'No drawings yet. Upload a PDF, PNG or JPEG to start.'}</p>}
    </div>
    <div className="pager">
      <button className="secondary" disabled={cursors.length < 2} onClick={() => setCursors(c => c.slice(0, -1))}>Previous</button>
      <button className="secondary" disabled={!data?.next_cursor} onClick={() => data?.next_cursor && setCursors(c => [...c, data.next_cursor])}>Next</button>
    </div>
  </main>;
}

function DrawingRow({item, onOpen}: {item: LibraryItem; onOpen: (id: string) => void}) {
  const preparing = item.preparation_state === 'QUEUED' || item.preparation_state === 'RUNNING';
  const progress = item.preparation_progress;
  const action = item.annotation_status === 'COMPLETED' ? 'View' : item.annotation_status === 'IN_PROGRESS' ? 'Continue' : 'Open';
  return <div className="drawing-row" role="row">
    <span className="thumb" role="cell">{item.thumbnail_asset_id ? <img src={assetUrl(item.thumbnail_asset_id)} alt="" loading="lazy"/> : <FileImage size={20}/>}</span>
    <span role="cell" className="filename">
      <strong>{item.filename}</strong>
      {preparing && <small className="secondary-message">Preparing{progress.page_count ? ` · page ${Math.min((progress.pages_done ?? 0) + 1, progress.page_count)} of ${progress.page_count}` : '…'}</small>}
      {item.preparation_state === 'FAILED' && <small className="secondary-message bad">Preparation failed</small>}
      {item.being_edited && item.annotation_status !== 'COMPLETED' && <small className="secondary-message">Being edited</small>}
    </span>
    <span role="cell">{item.page_count ?? '–'}</span>
    <span role="cell"><span className={`status status-${item.annotation_status}`}>{STATUS_LABEL[item.annotation_status]}</span></span>
    <span role="cell" className="muted">{item.annotation_version === '0' ? 'Not started' : relativeTime(item.annotation_updated_at)}</span>
    <span role="cell"><button className="secondary" onClick={() => onOpen(item.id)}>{action}</button></span>
  </div>;
}
