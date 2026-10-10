import { useMemo, useState } from 'react';
import type { DocState, Operation } from '../lib/annotationTypes';
import { applyOps, describeOperation } from '../state/docOps';
import { Dialog } from './Dialog';

interface Props {
  title: string;
  explanation: string;
  operations: Operation[];
  savedDoc: DocState;
  onApply: (operations: Operation[]) => void;
  onDiscard: () => void;
  onClose?: () => void;
}

/**
 * Conflict recovery: the latest saved drawing is shown; for each local change the engineer chooses
 * Keep saved version or Apply my change. Chosen changes are revalidated against the saved drawing and
 * submitted as new version-checked operations. No automatic rebase onto different data.
 */
export function DraftRecovery({title, explanation, operations, savedDoc, onApply, onDiscard, onClose}: Props) {
  const [choices, setChoices] = useState<boolean[]>(() => operations.map(() => true));
  const evaluation = useMemo(() => {
    let doc = savedDoc;
    return operations.map((op, index) => {
      if (!choices[index]) return {applies: true, chosen: false};
      try {
        doc = applyOps(doc, [op]);
        return {applies: true, chosen: true};
      } catch {
        return {applies: false, chosen: true};
      }
    });
  }, [operations, savedDoc, choices]);
  const chosen = operations.filter((_, i) => choices[i] && evaluation[i].applies);

  return <Dialog title={title} wide onClose={onClose ?? (() => undefined)} actions={<>
    <button className="secondary" onClick={onDiscard}>Keep saved version for all</button>
    <button className="primary" onClick={() => onApply(chosen)} disabled={!chosen.length}>Apply {chosen.length} selected change{chosen.length === 1 ? '' : 's'}</button>
  </>}>
    <p>{explanation}</p>
    <ol className="recovery-list">
      {operations.map((op, index) => <li key={index} className={evaluation[index].applies ? '' : 'stale'}>
        <span>{describeOperation(op, savedDoc)}</span>
        {evaluation[index].applies ? <div className="segmented small">
          <label className={!choices[index] ? 'active' : ''}>
            <input type="radio" checked={!choices[index]} onChange={() => setChoices(c => c.map((v, i) => i === index ? false : v))}/>Keep saved version
          </label>
          <label className={choices[index] ? 'active' : ''}>
            <input type="radio" checked={choices[index]} onChange={() => setChoices(c => c.map((v, i) => i === index ? true : v))}/>Apply my change
          </label>
        </div> : <small className="muted">No longer applies to the saved drawing</small>}
      </li>)}
    </ol>
  </Dialog>;
}
