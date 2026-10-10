import { Eye, EyeOff, Group, Maximize, MousePointer2, Redo2, ScanText, Shapes, SquareDashed, Trash2, Undo2 } from 'lucide-react';
import { useState } from 'react';
import type { Layers, Tool } from './AnnotationCanvas';

interface Props {
  tool: Tool; onTool: (tool: Tool) => void; readOnly: boolean;
  canGroup: boolean; onGroup: () => void; canDelete: boolean; onDelete: () => void;
  canUndo: boolean; onUndo: () => void; canRedo: boolean; onRedo: () => void;
  onFit: () => void; layers: Layers; onLayers: (layers: Layers) => void;
}

const TOOLS: {tool: Tool; label: string; key: string; icon: React.ReactNode}[] = [
  {tool: 'select', label: 'Select', key: 'V', icon: <MousePointer2 size={16}/>},
  {tool: 'TEXT', label: 'Text region', key: 'T', icon: <ScanText size={16}/>},
  {tool: 'SYMBOL', label: 'Symbol region', key: 'S', icon: <Shapes size={16}/>},
  {tool: 'IGNORE', label: 'Unreadable region', key: 'U', icon: <SquareDashed size={16}/>},
];

export function AnnotationToolbar(p: Props) {
  const [layersOpen, setLayersOpen] = useState(false);
  return <div className="toolbar" role="toolbar" aria-label="Annotation tools">
    {TOOLS.map(t => <button key={t.tool} className={p.tool === t.tool ? 'active' : ''} aria-pressed={p.tool === t.tool}
      disabled={p.readOnly && t.tool !== 'select'} onClick={() => p.onTool(t.tool)} title={`${t.label} (${t.key})`}>
      {t.icon}<span>{t.label}</span>
    </button>)}
    <span className="toolbar-sep"/>
    <button disabled={p.readOnly || !p.canGroup} onClick={p.onGroup} title="Group selected regions as one manufacturing requirement (G)">
      <Group size={16}/><span>Group as requirement</span>
    </button>
    <button disabled={p.readOnly || !p.canDelete} onClick={p.onDelete} title="Delete selection (Delete)"><Trash2 size={16}/><span>Delete</span></button>
    <button disabled={p.readOnly || !p.canUndo} onClick={p.onUndo} title="Undo (Ctrl/Cmd+Z)"><Undo2 size={16}/><span>Undo</span></button>
    <button disabled={p.readOnly || !p.canRedo} onClick={p.onRedo} title="Redo (Ctrl/Cmd+Shift+Z)"><Redo2 size={16}/><span>Redo</span></button>
    <span className="toolbar-sep"/>
    <button onClick={p.onFit} title="Fit page (0)"><Maximize size={16}/><span>Fit page</span></button>
    <div className="layers">
      <button aria-expanded={layersOpen} onClick={() => setLayersOpen(o => !o)} title="Layer visibility"><Eye size={16}/><span>Layers</span></button>
      {layersOpen && <div className="layers-menu" role="menu">
        {([['TEXT', 'Text regions'], ['SYMBOL', 'Symbol regions'], ['IGNORE', 'Unreadable regions'], ['badges', 'Requirement numbers']] as const)
          .map(([key, label]) => <label key={key}>
            <input type="checkbox" checked={p.layers[key]} onChange={e => p.onLayers({...p.layers, [key]: e.target.checked})}/>
            {p.layers[key] ? <Eye size={14}/> : <EyeOff size={14}/>}{label}
          </label>)}
      </div>}
    </div>
  </div>;
}
