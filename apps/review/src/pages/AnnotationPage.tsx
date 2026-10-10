import { ArrowLeft, CheckCircle2, HelpCircle, RotateCcw } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AnnotationCanvas, type Badge, type Layers, type Tool, type View } from '../components/AnnotationCanvas';
import { AnnotationLists } from '../components/AnnotationLists';
import { AnnotationToolbar } from '../components/AnnotationToolbar';
import { Dialog } from '../components/Dialog';
import { DraftRecovery } from '../components/DraftRecovery';
import { FinishDialog } from '../components/FinishDialog';
import { PageNavigation } from '../components/PageNavigation';
import { RegionInspector } from '../components/RegionInspector';
import { RequirementGroupInspector } from '../components/RequirementGroupInspector';
import { SaveIndicator } from '../components/SaveIndicator';
import { useAnnotationDocument } from '../hooks/useAnnotationDocument';
import { useAutosave } from '../hooks/useAutosave';
import { useBrowserProgress } from '../hooks/useBrowserProgress';
import { useEditLease } from '../hooks/useEditLease';
import { useOcrSuggestions } from '../hooks/useOcrSuggestions';
import { api, ApiError, assetUrl } from '../lib/annotationApi';
import type { AnnotationDocument, CompletionIssue, DocState, DrawingDetail, Group, Member, MutationResult, Operation,
  Polygon, Region, RegionFields, RegionKind, Role, SealedEnvelope, Vocabulary } from '../lib/annotationTypes';
import { STATUS_LABEL } from '../lib/format';
import { bbox, normalizeAngle } from '../lib/geometry';
import { browserId, newTabId } from '../lib/identity';
import { localDrafts } from '../lib/localDrafts';
import { type RecoveredDraft, resolveOutboxes } from '../lib/recovery';
import { docFromServer } from '../state/docOps';

export function AnnotationPage({drawingId, onBack}: {drawingId: string; onBack: () => void}) {
  const {phase, reload} = useAnnotationDocument(drawingId);
  if (phase.kind === 'loading') return <div className="center-message">Loading drawing…</div>;
  if (phase.kind === 'error') return <div className="center-message">
    <p className={phase.notFound ? '' : 'error'}>{phase.message}{phase.notFound ? '' : ' Retrying…'}</p>
    <button className="secondary" onClick={onBack}><ArrowLeft size={16}/>Back to drawings</button>
  </div>;
  if (phase.kind === 'preparing') return <PreparationView detail={phase.detail} onBack={onBack} onRetry={async () => {
    await api.retryPreparation(drawingId).catch(() => undefined);
    void reload();
  }}/>;
  return <Workspace drawingId={drawingId} detail={phase.detail} document={phase.document} vocabulary={phase.vocabulary} onBack={onBack}/>;
}

function PreparationView({detail, onBack, onRetry}: {detail: DrawingDetail; onBack: () => void; onRetry: () => void}) {
  const {progress} = detail.preparation;
  const failed = detail.drawing.preparation_state === 'FAILED';
  const fraction = progress.page_count ? (progress.pages_done ?? 0) / progress.page_count : 0;
  return <div className="center-message">
    <h2>{detail.drawing.filename}</h2>
    {failed ? <>
      <p className="error">Preparation failed: {detail.preparation.error_message ?? 'the drawing could not be rendered.'}</p>
      <button className="primary" onClick={onRetry}><RotateCcw size={16}/>Retry preparation</button>
    </> : <>
      <p>Preparing pages{progress.page_count ? ` — ${progress.pages_done ?? 0} of ${progress.page_count} ready` : '…'}</p>
      <div className="progress wide"><span style={{width: `${Math.max(4, fraction * 100)}%`}}/></div>
      <p className="muted">The editor opens when every page is ready.</p>
    </>}
    <button className="secondary" onClick={onBack}><ArrowLeft size={16}/>Back to drawings</button>
  </div>;
}

type Selection = {regionIds: string[]; groupId: string | null};
type Recovery = {title: string; explanation: string; operations: Operation[]; savedDoc: DocState; version: string;
  status: AnnotationDocument['annotation_status']; draftKey?: string};

const DEFAULT_LAYERS: Layers = {TEXT: true, SYMBOL: true, IGNORE: true, badges: true};

function Workspace({drawingId, detail, document: initialDocument, vocabulary, onBack}: {
  drawingId: string; detail: DrawingDetail; document: AnnotationDocument; vocabulary: Vocabulary; onBack: () => void;
}) {
  const pages = detail.pages;
  const identity = useMemo(() => ({browser_id: browserId(), tab_id: newTabId()}), []);
  const leaseLost = useRef<() => void>(() => undefined);
  const initial = useMemo(() => ({base: docFromServer(initialDocument), version: initialDocument.annotation_version,
    status: initialDocument.annotation_status}), [initialDocument]);
  const {store, snapshot} = useAutosave(drawingId, identity, () => leaseLost.current(), initial);
  const doc = snapshot.visible;
  const status = snapshot.state.status;
  const completed = status === 'COMPLETED';

  const [drafts, setDrafts] = useState<RecoveredDraft[]>([]);
  const [recovery, setRecovery] = useState<Recovery | null>(null);
  const [finishing, setFinishing] = useState(false);
  const handlingPause = useRef<object | null>(null);

  const lease = useEditLease(drawingId, identity, store, !completed);
  leaseLost.current = () => void lease.reacquire();
  const editing = lease.state.kind === 'held' && !completed;
  const readOnly = !editing || finishing || recovery !== null || !snapshot.canEdit;
  useEffect(() => { store.setReadOnly(!editing || recovery !== null); }, [store, editing, recovery]);

  // ---------------------------------------------------------------- recovery of same-browser outboxes
  const fetchLatest = useCallback(async () => {
    const latest = await api.document(drawingId);
    return {doc: docFromServer(latest), version: latest.annotation_version, status: latest.annotation_status};
  }, [drawingId]);

  useEffect(() => {
    let cancelled = false;
    const attempt = () => resolveOutboxes(drawingId, store.key).then(async result => {
      if (cancelled) return;
      setDrafts(result.drafts);
      if (result.completedByReceipt && !completed) {
        const latest = await fetchLatest();
        store.rebase(latest.doc, latest.version, latest.status, []);
      }
    }).catch(() => { if (!cancelled) setTimeout(attempt, 5000); });
    attempt();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawingId, store]);

  // Only the editor holding the lease drains a recovered outbox, one abandoned draft at a time.
  useEffect(() => {
    if (!editing || recovery || !drafts.length || snapshot.hasUnsaved) return;
    const draft = drafts[0];
    if (draft.baseVersion === snapshot.state.baseVersion) {
      store.rebase(snapshot.state.base, snapshot.state.baseVersion, snapshot.state.status, draft.operations);
      void localDrafts.remove(draft.key).catch(() => undefined);
      setDrafts(d => d.slice(1));
    } else {
      setRecovery({title: 'Unsaved changes from this browser', draftKey: draft.key,
        explanation: 'This drawing changed after these edits were made in this browser. Choose which of your changes to apply to the latest saved drawing.',
        operations: draft.operations, savedDoc: snapshot.state.base, version: snapshot.state.baseVersion, status: snapshot.state.status});
    }
  }, [editing, recovery, drafts, snapshot.hasUnsaved, snapshot.state, store]);

  // Paused saving: conflicts and rejections open recovery against the latest saved drawing.
  useEffect(() => {
    const paused = snapshot.paused;
    if (!paused || recovery || handlingPause.current === paused) return;
    handlingPause.current = paused;
    if (paused.kind === 'conflict' || paused.kind === 'rejected') {
      void fetchLatest().then(latest => setRecovery({
        title: paused.kind === 'conflict' ? 'This drawing changed before your edits were saved' : 'Some changes could not be saved',
        explanation: paused.kind === 'conflict'
          ? 'Another editor saved changes first. Your changes are kept below; choose which to apply to the latest saved drawing.'
          : `The server rejected a save: ${paused.message} Choose which changes to apply again.`,
        operations: store.unsavedOperations(), savedDoc: latest.doc, version: latest.version, status: latest.status}));
    } else if (paused.kind === 'completed') {
      // Keep the edits aside; they are offered again only after someone explicitly reopens the drawing.
      const operations = store.unsavedOperations();
      const key = `${drawingId}:${identity.tab_id}:deferred`;
      void localDrafts.put({key, drawingId, editorId: `${identity.tab_id}:deferred`, baseVersion: snapshot.state.baseVersion,
        sealed: null, pending: operations, updatedAt: new Date().toISOString()}).catch(() => undefined)
        .then(fetchLatest).then(latest => {
          store.rebase(latest.doc, latest.version, latest.status, []);
          setDrafts(d => [...d, {key, baseVersion: snapshot.state.baseVersion, operations, updatedAt: new Date().toISOString()}]);
        });
    }
  }, [snapshot.paused, recovery, fetchLatest, store, drawingId, identity.tab_id, snapshot.state.baseVersion]);

  const resolveRecovery = (operations: Operation[]) => {
    if (!recovery) return;
    store.rebase(recovery.savedDoc, recovery.version, recovery.status, operations);
    if (recovery.draftKey) {
      void localDrafts.remove(recovery.draftKey).catch(() => undefined);
      setDrafts(d => d.filter(x => x.key !== recovery.draftKey));
    }
    setRecovery(null);
  };

  // ---------------------------------------------------------------- read-only refresh
  useEffect(() => {
    if (editing) return;
    const timer = setInterval(async () => {
      if (document.visibilityState !== 'visible' || store.getSnapshot().hasUnsaved) return;
      try {
        const latest = await api.detail(drawingId);
        if (latest.drawing.annotation_version !== store.getSnapshot().state.baseVersion) {
          const next = await fetchLatest();
          store.replaceBase(next.doc, next.version, next.status);
        }
      } catch { /* keep showing the last loaded data */ }
    }, 5000);
    return () => clearInterval(timer);
  }, [editing, drawingId, fetchLatest, store]);

  // ---------------------------------------------------------------- navigation and view state
  const progress = useBrowserProgress(drawingId, identity.browser_id, pages, detail.drawing.last_annotation_page_id);
  const [activePageId, setActivePageId] = useState<string | null>(null);
  const views = useRef<Record<string, View>>({});
  const [selection, setSelection] = useState<Selection>({regionIds: [], groupId: null});
  const [tool, setTool] = useState<Tool>('select');
  const [layers, setLayers] = useState<Layers>(DEFAULT_LAYERS);
  const [pickMode, setPickMode] = useState(false);
  const [fitNonce, setFitNonce] = useState(0);
  const [focus, setFocus] = useState<{polygon: Polygon; nonce: number} | null>(null);
  const [focusInput, setFocusInput] = useState(0);
  const docRef = useRef(doc);
  docRef.current = doc;
  // Fill the box with the recognized text unless the annotator got there first or the region is gone.
  const ocr = useOcrSuggestions(drawingId, detail.ocr_enabled === true, (regionId, text) => {
    const region = docRef.current.regions[regionId];
    if (region && region.kind === 'TEXT' && region.legibility === 'READABLE' && !region.transcription) {
      edit([{type: 'region.update', region_id: regionId, changes: {transcription: text}}]);
    }
  });
  const [toast, setToast] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{title: string; body: string; action: () => void} | null>(null);
  const [showHelp, setShowHelp] = useState(false);

  useEffect(() => {
    if (!progress.initial || activePageId) return;
    const {pageId, progress: saved} = progress.initial;
    if (saved) views.current[pageId] = saved;
    setActivePageId(pageId);
    const region = saved?.selected_region_id && doc.regions[saved.selected_region_id];
    const group = saved?.selected_group_id && doc.groups[saved.selected_group_id];
    setSelection({regionIds: region ? [region.id] : [], groupId: group ? group.id : null});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [progress.initial]);

  const page = pages.find(p => p.id === activePageId) ?? null;
  const pageNumber = useCallback((id: string) => pages.find(p => p.id === id)?.page_number ?? 0, [pages]);
  const pageRegions = useMemo(() => Object.values(doc.regions).filter(r => r.page_id === activePageId), [doc.regions, activePageId]);
  const groups = useMemo(() => Object.values(doc.groups).sort((a, b) => (a.display_number ?? 1e9) - (b.display_number ?? 1e9)), [doc.groups]);
  const regionCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const r of Object.values(doc.regions)) counts[r.page_id] = (counts[r.page_id] ?? 0) + 1;
    return counts;
  }, [doc.regions]);

  const selectedGroup = selection.groupId ? doc.groups[selection.groupId] : undefined;
  const selectedRegions = selection.regionIds.map(id => doc.regions[id]).filter(Boolean);
  const highlighted = useMemo(() => new Set(selectedGroup ? Object.values(doc.members).filter(m => m.group_id === selectedGroup.id).map(m => m.region_id) : []),
    [selectedGroup, doc.members]);

  const badges = useMemo<Badge[]>(() => {
    if (!activePageId) return [];
    const byGroup = new Map<string, number[][]>();
    for (const m of Object.values(doc.members)) {
      const g = doc.groups[m.group_id], r = doc.regions[m.region_id];
      if (!g || !r || m.role === 'CONTEXT' || g.primary_page_id !== activePageId || r.page_id !== activePageId) continue;
      byGroup.set(g.id, [...(byGroup.get(g.id) ?? []), ...r.polygon]);
    }
    return [...byGroup.entries()].map(([groupId, points]) => {
      const box = bbox(points);
      return {groupId, number: doc.groups[groupId].display_number, x: box.right, y: box.top, selected: groupId === selection.groupId};
    });
  }, [doc, activePageId, selection.groupId]);

  const saveView = useCallback((view: View) => {
    if (!activePageId) return;
    views.current[activePageId] = view;
    progress.save({page_id: activePageId, ...view, selected_region_id: selection.regionIds[0] ?? null, selected_group_id: selection.groupId});
  }, [activePageId, progress, selection]);

  const goToPage = (pageId: string) => {
    if (pageId === activePageId) return;
    setSelection(s => ({regionIds: [], groupId: s.groupId}));
    setActivePageId(pageId);
  };

  const showRegion = (region: Region, additive = false) => {
    if (region.page_id !== activePageId) goToPage(region.page_id);
    setSelection(s => ({groupId: null, regionIds: additive ? (s.regionIds.includes(region.id) ? s.regionIds.filter(id => id !== region.id) : [...s.regionIds, region.id]) : [region.id]}));
    setFocus({polygon: region.polygon, nonce: Date.now()});
  };

  const showGroup = (group: Group) => {
    if (group.primary_page_id !== activePageId) goToPage(group.primary_page_id);
    setSelection({regionIds: [], groupId: group.id});
    const points = Object.values(doc.members).filter(m => m.group_id === group.id && m.role !== 'CONTEXT')
      .flatMap(m => doc.regions[m.region_id]?.page_id === group.primary_page_id ? doc.regions[m.region_id].polygon : []);
    if (points.length) setFocus({polygon: points, nonce: Date.now()});
  };

  // ---------------------------------------------------------------- editing actions
  const edit = (ops: Operation[], options?: {debounce?: boolean; coalesceKey?: string}) => {
    if (readOnly) return false;
    const ok = store.edit(ops, options);
    if (!ok) setToast('That change no longer applies to this drawing.');
    return ok;
  };

  const createRegion = (kind: RegionKind, polygon: Polygon) => {
    if (!activePageId) return;
    const id = crypto.randomUUID();
    if (edit([{type: 'region.create', region: {id, page_id: activePageId, kind, polygon, transcription: '', symbol_code: null,
      legibility: kind === 'IGNORE' ? 'UNREADABLE' : 'READABLE', reading_angle_degrees: 0, semantic_disposition: 'UNCLASSIFIED'}}])) {
      setSelection({regionIds: [id], groupId: null});
      if (kind !== 'IGNORE') setFocusInput(n => n + 1);
      if (kind === 'TEXT') ocr.request(id, activePageId, polygon);
    }
  };

  const commitGeometry = (regionId: string, polygon: Polygon, angleDelta: number) => {
    const region = doc.regions[regionId];
    if (!region) return;
    const changes: Partial<RegionFields> = {polygon};
    if (Math.abs(angleDelta) > 1e-6) changes.reading_angle_degrees = normalizeAngle(region.reading_angle_degrees + angleDelta);
    edit([{type: 'region.update', region_id: regionId, changes}]);
  };

  const changeRegion = (region: Region, changes: Partial<RegionFields>, typing?: boolean) =>
    edit([{type: 'region.update', region_id: region.id, changes}],
      typing ? {debounce: true, coalesceKey: `region:${region.id}:${Object.keys(changes).join(',')}`} : undefined);

  const changeGroup = (group: Group, changes: Partial<Group>, typing?: boolean) =>
    edit([{type: 'group.update', group_id: group.id, changes}],
      typing ? {debounce: true, coalesceKey: `group:${group.id}:${Object.keys(changes).join(',')}`} : undefined);

  const defaultRole = (region: Region, group: Group): Role =>
    region.kind === 'IGNORE' || region.page_id !== group.primary_page_id ? 'CONTEXT' : region.kind === 'SYMBOL' ? 'SYMBOL' : 'PRIMARY';

  const nextOrder = (groupId: string) => Math.max(-1, ...Object.values(doc.members).filter(m => m.group_id === groupId).map(m => m.order_index)) + 1;

  const groupable = selectedRegions.length > 0 && selectedRegions.every(r => r.page_id === activePageId && r.kind !== 'IGNORE');
  const groupSelection = () => {
    if (!groupable || !activePageId) return;
    const id = crypto.randomUUID();
    const type = selectedRegions.some(r => r.symbol_code?.startsWith('GDT_')) ? 'GEOMETRIC_TOLERANCE' : 'DIMENSION';
    const ops: Operation[] = [{type: 'group.create', group: {id, primary_page_id: activePageId, requirement_type: type}}];
    selectedRegions.forEach((r, index) => ops.push({type: 'member.add', group_id: id, region_id: r.id,
      role: r.kind === 'SYMBOL' ? 'SYMBOL' : 'PRIMARY', order_index: index}));
    if (edit(ops)) setSelection({regionIds: [], groupId: id});
  };

  const deleteSelection = () => {
    if (readOnly) return;
    if (selectedGroup) {
      setConfirm({title: `Delete requirement ${selectedGroup.display_number ?? ''}?`,
        body: 'Its text regions are kept; only the requirement and its links are removed.',
        action: () => { if (edit([{type: 'group.delete', group_id: selectedGroup.id}])) setSelection({regionIds: [], groupId: null}); }});
      return;
    }
    if (!selectedRegions.length) return;
    const ids = new Set(selectedRegions.map(r => r.id));
    const links = Object.values(doc.members).filter(m => ids.has(m.region_id));
    const ops: Operation[] = [...links.map(m => ({type: 'member.remove', group_id: m.group_id, region_id: m.region_id, role: m.role}) as Operation),
      ...selectedRegions.map(r => ({type: 'region.delete', region_id: r.id}) as Operation)];
    const run = () => { if (edit(ops)) setSelection({regionIds: [], groupId: null}); };
    if (links.length) {
      const numbers = [...new Set(links.map(m => doc.groups[m.group_id]?.display_number ?? '…'))].join(', ');
      setConfirm({title: `Delete ${selectedRegions.length === 1 ? 'this region' : `${selectedRegions.length} regions`}?`,
        body: `Linked to requirement ${numbers}. Deleting removes those links too.`, action: run});
    } else run();
  };

  const linkRegion = (region: Region, groupId: string, role: Role) => {
    const group = doc.groups[groupId];
    if (!group) return;
    if (role !== 'CONTEXT' && region.page_id !== group.primary_page_id) {
      setToast('Only CONTEXT links can point to a different page than the requirement.');
      return;
    }
    if (role !== 'CONTEXT' && region.kind === 'IGNORE') {
      setToast('Unreadable regions can only be CONTEXT links.');
      return;
    }
    if (doc.members[`${groupId}|${region.id}|${role}`]) return;
    edit([{type: 'member.add', group_id: groupId, region_id: region.id, role, order_index: nextOrder(groupId)}]);
  };

  const changeRole = (member: Member, role: Role) => {
    if (doc.members[`${member.group_id}|${member.region_id}|${role}`]) return;
    edit([{type: 'member.remove', group_id: member.group_id, region_id: member.region_id, role: member.role},
      {type: 'member.add', group_id: member.group_id, region_id: member.region_id, role, order_index: member.order_index}]);
  };

  const moveMember = (member: Member, direction: -1 | 1) => {
    const list = Object.values(doc.members).filter(m => m.group_id === member.group_id)
      .sort((a, b) => a.order_index - b.order_index || a.region_id.localeCompare(b.region_id));
    const index = list.findIndex(m => m.region_id === member.region_id && m.role === member.role);
    const target = index + direction;
    if (target < 0 || target >= list.length) return;
    [list[index], list[target]] = [list[target], list[index]];
    const ops: Operation[] = list.flatMap((m, i) => m.order_index === i ? [] : [{type: 'member.update', group_id: m.group_id,
      region_id: m.region_id, role: m.role, changes: {order_index: i}} as Operation]);
    edit(ops);
  };

  const onRegionClick = (regionId: string, additive: boolean) => {
    const region = doc.regions[regionId];
    if (!region) return;
    if (pickMode && selectedGroup) {
      if (!Object.values(doc.members).some(m => m.group_id === selectedGroup.id && m.region_id === regionId)) {
        linkRegion(region, selectedGroup.id, defaultRole(region, selectedGroup));
      }
      return;
    }
    setSelection(s => ({groupId: null, regionIds: additive ? (s.regionIds.includes(regionId) ? s.regionIds.filter(id => id !== regionId) : [...s.regionIds, regionId]) : [regionId]}));
  };

  // ---------------------------------------------------------------- keyboard shortcuts
  const shortcuts = useRef<(event: KeyboardEvent) => void>(() => undefined);
  shortcuts.current = (event: KeyboardEvent) => {
    const target = event.target as HTMLElement;
    if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable)) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); store.flushNow(); }
      return;
    }
    if (document.querySelector('.dialog')) return;
    const mod = event.metaKey || event.ctrlKey;
    const key = event.key.toLowerCase();
    if (mod && key === 's') { event.preventDefault(); store.flushNow(); }
    else if (mod && key === 'z') { event.preventDefault(); if (event.shiftKey) store.redo(); else store.undo(); }
    else if (mod && key === 'y') { event.preventDefault(); store.redo(); }
    else if (mod || event.altKey) return;
    else if (event.key === 'Escape') { if (pickMode) setPickMode(false); else setSelection({regionIds: [], groupId: null}); }
    else if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); deleteSelection(); }
    else if (key === 'v') setTool('select');
    else if (key === 't' && !readOnly) setTool('TEXT');
    else if (key === 's' && !readOnly) setTool('SYMBOL');
    else if (key === 'u' && !readOnly) setTool('IGNORE');
    else if (key === 'g') groupSelection();
  };
  useEffect(() => {
    const handler = (event: KeyboardEvent) => shortcuts.current(event);
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), 4000); return () => clearTimeout(t); }, [toast]);
  useEffect(() => { if (!selectedGroup) setPickMode(false); }, [selectedGroup]);
  useEffect(() => { if (readOnly) setTool('select'); }, [readOnly]);

  // ---------------------------------------------------------------- completion and reopening
  const [finishOpen, setFinishOpen] = useState(false);
  const [finishError, setFinishError] = useState<string | null>(null);
  const [issues, setIssues] = useState<CompletionIssue[]>([]);
  const [reopening, setReopening] = useState(false);

  const sendControl = async (envelope: SealedEnvelope, send: () => Promise<MutationResult>): Promise<MutationResult> => {
    await store.persistControl(envelope);
    for (let attempt = 0; ; attempt++) {
      try {
        return await send();
      } catch (error) {
        if (!(error instanceof ApiError) || !error.isRetryable || attempt >= 4) throw error;
        // Unknown outcome: consult the receipt before deciding whether to send again.
        await new Promise(r => setTimeout(r, 1000 * 2 ** attempt));
        const receipt = await api.receipt(drawingId, envelope.mutation_id).catch(() => null);
        if (receipt) return receipt.result;
      }
    }
  };

  const finish = async (emptyConfirmed: string[]) => {
    setFinishing(true);
    setFinishError(null);
    setIssues([]);
    try {
      await store.flushAndWait();
    } catch (error) {
      setFinishError(`Your latest changes could not be saved, so the drawing was not finished. ${error instanceof Error ? error.message : ''}`);
      setFinishing(false);
      return;
    }
    const credentials = store.currentLease();
    if (!credentials) {
      setFinishError('Your editing session ended. Try again once editing is available.');
      setFinishing(false);
      return;
    }
    const envelope: SealedEnvelope = {kind: 'COMPLETE', mutation_id: crypto.randomUUID(),
      expected_annotation_version: store.getSnapshot().state.baseVersion, labeling_policy_version: vocabulary.labeling_policy_version,
      coverage: pages.map(p => ({page_id: p.id, status: emptyConfirmed.includes(p.id) ? 'NO_LABELABLE_CONTENT' : 'ANNOTATED'}))};
    try {
      const result = await sendControl(envelope, () => api.complete(drawingId, envelope, identity, credentials));
      await store.persistControl(null);
      store.setStatus('COMPLETED', result.annotation_version);
      setFinishOpen(false);
      setSelection({regionIds: [], groupId: null});
    } catch (error) {
      await store.persistControl(null);
      if (error instanceof ApiError && error.code === 'COMPLETION_INVALID') setIssues(error.details);
      else setFinishError(error instanceof Error ? error.message : 'The drawing could not be finished.');
    } finally {
      setFinishing(false);
    }
  };

  const showIssue = (issue: CompletionIssue) => {
    setFinishOpen(false);
    if (issue.region_id && doc.regions[issue.region_id]) {
      showRegion(doc.regions[issue.region_id]);
      if (issue.field === 'transcription') setFocusInput(n => n + 1);
    } else if (issue.group_id && doc.groups[issue.group_id]) showGroup(doc.groups[issue.group_id]);
    else if (issue.page_id) goToPage(issue.page_id);
  };

  const reopen = async () => {
    setReopening(true);
    const envelope: SealedEnvelope = {kind: 'REOPEN', mutation_id: crypto.randomUUID(),
      expected_annotation_version: store.getSnapshot().state.baseVersion};
    try {
      await sendControl(envelope, () => api.reopen(drawingId, envelope, identity));
      await store.persistControl(null);
      const latest = await fetchLatest();
      store.rebase(latest.doc, latest.version, latest.status, []);
    } catch (error) {
      await store.persistControl(null);
      setToast(error instanceof Error ? error.message : 'The drawing could not be reopened.');
      if (error instanceof ApiError && error.status === 409) {
        const latest = await fetchLatest().catch(() => null);
        if (latest) store.replaceBase(latest.doc, latest.version, latest.status);
      }
    } finally {
      setReopening(false);
    }
  };

  // ---------------------------------------------------------------- leaving
  const [leaveBlocked, setLeaveBlocked] = useState<string | null>(null);
  const leave = async () => {
    if (!store.getSnapshot().hasUnsaved) return onBack();
    try {
      await Promise.race([store.flushAndWait(), new Promise((_, reject) => setTimeout(() => reject(new Error('Saving is taking too long.')), 10_000))]);
      onBack();
    } catch (error) {
      setLeaveBlocked(error instanceof Error ? error.message : 'Saving failed.');
    }
  };

  // ---------------------------------------------------------------- render
  const emptyPageIds = pages.filter(p => !regionCounts[p.id]).map(p => p.id);
  const now = Date.now();
  const retryIn = lease.state.kind === 'unavailable' ? Math.max(0, Math.ceil((lease.state.retryAt - now) / 1000)) : 0;
  const [, tick] = useState(0);
  useEffect(() => {
    if (lease.state.kind !== 'unavailable') return;
    const t = setInterval(() => tick(n => n + 1), 1000);
    return () => clearInterval(t);
  }, [lease.state.kind]);
  // With unsaved local work waiting, try once more when the previous lease should have expired.
  const waitingWork = snapshot.hasUnsaved || drafts.length > 0;
  useEffect(() => {
    if (lease.state.kind !== 'unavailable' || !waitingWork) return;
    const t = setTimeout(() => void lease.acquire(), Math.max(0, lease.state.retryAt - Date.now()) + 500);
    return () => clearTimeout(t);
  }, [lease, waitingWork]);

  return <div className={`workspace ${pages.length < 2 ? 'single-page' : ''}`}>
    <header className="workspace-header">
      <button className="back" onClick={() => void leave()}><ArrowLeft size={16}/>Back to drawings</button>
      <div className="title">
        <strong title={detail.drawing.filename}>{detail.drawing.filename}</strong>
        <span className={`status status-${status}`}>{STATUS_LABEL[status]}</span>
      </div>
      <SaveIndicator snapshot={snapshot} readOnly={!editing}/>
      <div className="header-actions">
        <button className="icon-button" onClick={() => setShowHelp(true)} title="How to annotate"><HelpCircle size={18}/><span>Instructions</span></button>
        {completed ? <div className="finish-block">
          <button className="secondary" onClick={() => void reopen()} disabled={reopening}><RotateCcw size={16}/>Reopen drawing</button>
          <small>Reopening removes this drawing from the current training-ready set until it is finished again.</small>
        </div> : <div className="finish-block">
          <button className="primary" disabled={!editing || finishing} onClick={() => { setFinishOpen(true); setIssues([]); setFinishError(null); }}>
            <CheckCircle2 size={16}/>Finish drawing</button>
          <small>Finish confirms that every page has been annotated. Mark unreadable text explicitly.</small>
        </div>}
      </div>
    </header>

    {!editing && <div className={`banner ${completed ? 'ok' : 'info'}`} role="status">
      {completed ? <span>This drawing is completed and available for training. It is shown read-only.</span>
        : lease.state.kind === 'unavailable' ? <>
          <span>Someone is editing this drawing. You can view it or try again shortly.{retryIn > 0 ? ` Editing may be available in ${retryIn}s.` : ''}</span>
          <button className="secondary small" onClick={() => void lease.acquire()}>Try editing again</button>
        </> : lease.state.kind === 'error' ? <>
          <span>Editing is not available: {lease.state.message}</span>
          <button className="secondary small" onClick={() => void lease.acquire()}>Try editing again</button>
        </> : <span>Opening editor…</span>}
    </div>}
    {drafts.length > 0 && !editing && <div className="banner warn" role="status">
      {completed ? `${drafts[0].operations.length} unsaved change(s) from this browser were not applied because the drawing is completed. Reopen it to review them.`
        : `${drafts[0].operations.length} unsaved change(s) from this browser are waiting and will be offered when editing is available here.`}
    </div>}
    {pickMode && selectedGroup && <div className="banner info">
      Click regions to link them to requirement {selectedGroup.display_number ?? '(new)'}. Regions on another page are linked as context. Press Esc when done.
    </div>}

    <AnnotationToolbar tool={tool} onTool={setTool} readOnly={readOnly}
      canGroup={groupable} onGroup={groupSelection} canDelete={!!selectedGroup || selectedRegions.length > 0} onDelete={deleteSelection}
      canUndo={snapshot.state.undo.length > 0} onUndo={() => store.undo()} canRedo={snapshot.state.redo.length > 0} onRedo={() => store.redo()}
      onFit={() => setFitNonce(n => n + 1)} layers={layers} onLayers={setLayers}/>

    <div className="workspace-body">
      <PageNavigation pages={pages} activePageId={activePageId ?? ''} counts={regionCounts} onSelect={goToPage}/>
      <div className="canvas-area">
        {page ? <AnnotationCanvas key={page.id} page={page} imageUrl={assetUrl(page.render_asset_id)} regions={pageRegions}
          badges={badges} selectedRegionIds={selection.regionIds} highlightedRegionIds={highlighted} tool={tool} readOnly={readOnly}
          pickMode={pickMode} layers={layers} initialView={views.current[page.id]} fitNonce={fitNonce} focus={focus}
          onViewChange={saveView} onCreateRegion={createRegion} onGeometryCommit={commitGeometry} onRegionClick={onRegionClick}
          onBackgroundClick={() => { if (!pickMode) setSelection({regionIds: [], groupId: null}); }}
          onBadgeClick={groupId => doc.groups[groupId] && showGroup(doc.groups[groupId])}/>
          : <div className="center-message">Loading page…</div>}
      </div>
      <aside className="side-panel">
        <div className="inspector">
          {selectedGroup ? <RequirementGroupInspector group={selectedGroup} doc={doc} vocabulary={vocabulary} readOnly={readOnly}
            pickMode={pickMode} pageNumber={pageNumber} onChange={(changes, typing) => changeGroup(selectedGroup, changes, typing)}
            onRoleChange={changeRole} onMove={moveMember} onRemove={m => edit([{type: 'member.remove', group_id: m.group_id, region_id: m.region_id, role: m.role}])}
            onPickMode={setPickMode} onSelectRegion={id => doc.regions[id] && showRegion(doc.regions[id])} onDelete={deleteSelection}/>
            : selectedRegions.length === 1 ? <RegionInspector key={selectedRegions[0].id} region={selectedRegions[0]} doc={doc}
              vocabulary={vocabulary} readOnly={readOnly} pageNumber={pageNumber} focusNonce={focusInput}
              reading={ocr.reading.has(selectedRegions[0].id)}
              onChange={(changes, typing) => changeRegion(selectedRegions[0], changes, typing)}
              onOpenGroup={id => doc.groups[id] && showGroup(doc.groups[id])}
              onLink={(groupId, role) => linkRegion(selectedRegions[0], groupId, role)} onDelete={deleteSelection}/>
            : selectedRegions.length > 1 ? <section className="inspector-section">
              <h2>{selectedRegions.length} regions selected</h2>
              <p className="muted">Group them as one manufacturing requirement, or delete them.</p>
              <button className="primary" disabled={readOnly || !groupable} onClick={groupSelection}>Group as requirement</button>
              {!groupable && <p className="muted small">Grouping needs readable text or symbol regions on this page.</p>}
            </section>
            : <section className="inspector-section empty-inspector">
              <h2>{readOnly ? 'Viewing' : 'Nothing selected'}</h2>
              <p className="muted">{readOnly ? 'Select a region or requirement to see its labels.'
                : 'Choose “Text region” and drag a box around a line of text, then type exactly what it shows. Shift-click to select several regions.'}</p>
            </section>}
        </div>
        <AnnotationLists doc={doc} pageRegions={pageRegions} groups={groups} pageNumber={pageNumber}
          selectedRegionIds={selection.regionIds} selectedGroupId={selection.groupId}
          onSelectRegion={r => showRegion(r)} onSelectGroup={showGroup}/>
      </aside>
    </div>

    {toast && <div className="toast" role="alert">{toast}</div>}
    {finishOpen && <FinishDialog pages={pages} emptyPageIds={emptyPageIds} busy={finishing} error={finishError} issues={issues}
      onConfirm={confirmed => void finish(confirmed)} onShowIssue={showIssue} onClose={() => setFinishOpen(false)}/>}
    {recovery && <DraftRecovery title={recovery.title} explanation={recovery.explanation} operations={recovery.operations}
      savedDoc={recovery.savedDoc} onApply={resolveRecovery} onDiscard={() => resolveRecovery([])}/>}
    {confirm && <Dialog title={confirm.title} onClose={() => setConfirm(null)} actions={<>
      <button className="secondary" onClick={() => setConfirm(null)}>Cancel</button>
      <button className="danger" onClick={() => { confirm.action(); setConfirm(null); }}>Delete</button>
    </>}><p>{confirm.body}</p></Dialog>}
    {leaveBlocked && <Dialog title="Changes are not saved to the server yet" onClose={() => setLeaveBlocked(null)} actions={<>
      <button className="secondary" onClick={() => setLeaveBlocked(null)}>Stay</button>
      <button className="danger" onClick={onBack}>Leave anyway</button>
    </>}>
      <p>{leaveBlocked}</p>
      <p>{snapshot.storageAvailable
        ? 'Your changes are kept on this device only. Reopen this drawing in this browser to finish saving them.'
        : 'This browser cannot keep an offline copy. If you leave now, unsaved changes will be lost.'}</p>
    </Dialog>}
    {showHelp && <Dialog title="How to annotate" onClose={() => setShowHelp(false)} actions={<button className="primary" onClick={() => setShowHelp(false)}>Got it</button>}>
      <ul className="help">
        <li><b>Label every page completely.</b> Draw a box around every visible text line and engineering symbol, including the title block, drawing number and revision. Unlabeled text would teach the detector that text is background.</li>
        <li><b>Type exactly what the drawing shows</b>, including Ø, ±, °, signs, decimal separators and trailing zeros. Do not rewrite “Ø20” as “diameter 20”.</li>
        <li>Use separate regions for stacked tolerances or text in a different direction. Rotate a box with its round handle.</li>
        <li><b>Unreadable</b>: mark text or symbols you cannot read reliably. Never guess characters. Do not mask lines or hatching.</li>
        <li><b>Only manufacturing requirements get numbers.</b> Select the regions that form one requirement (for example nominal and tolerances) and click “Group as requirement”. Drawing names, numbers and revisions are not requirements; mark them “Not a requirement”.</li>
        <li>Edits save automatically. Click <b>Finish drawing</b> when every page is annotated.</li>
        <li>Shortcuts: V select · T text · S symbol · U unreadable · G group · Delete · Ctrl/Cmd+Z undo · Space+drag pan · Esc cancel.</li>
      </ul>
    </Dialog>}
  </div>;
}
