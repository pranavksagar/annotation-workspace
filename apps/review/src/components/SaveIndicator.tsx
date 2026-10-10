import { AlertTriangle, Check, CloudOff, Loader2, PauseCircle, RefreshCw } from 'lucide-react';
import type { EditorSnapshot } from '../state/editorStore';

const LABELS = {
  saved: 'Saved',
  saving: 'Saving…',
  offline: 'Offline — changes saved on this device',
  retrying: 'Could not save. Retrying…',
  paused: 'Editing paused — changes need recovery',
} as const;

export function SaveIndicator({snapshot, readOnly}: {snapshot: EditorSnapshot; readOnly: boolean}) {
  if (readOnly && !snapshot.hasUnsaved) return null;
  const status = snapshot.saveStatus;
  const offlineWithoutStorage = status === 'offline' && !snapshot.storageAvailable;
  const icon = {saved: <Check size={14}/>, saving: <Loader2 size={14} className="spin"/>, offline: <CloudOff size={14}/>,
    retrying: <RefreshCw size={14}/>, paused: <PauseCircle size={14}/>}[status];
  const time = snapshot.lastSavedAt ? new Date(snapshot.lastSavedAt).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) : null;
  return <div className="save-indicator-group">
    <span className={`save-indicator ${status}`} role="status" aria-live="polite"
      title={status === 'saved' && time ? `Last saved to the server at ${time}` : undefined}>
      {icon}{offlineWithoutStorage ? 'Offline — changes are not saved' : LABELS[status]}
      {status === 'saved' && time && <small>{time}</small>}
    </span>
    {!snapshot.storageAvailable && <span className="save-indicator warn" title={
      'This browser cannot keep an offline copy of unsaved edits. Online saving still works; ' +
      'do not close the tab while changes are unsaved.'}>
      <AlertTriangle size={14}/>Local recovery unavailable
    </span>}
  </div>;
}
