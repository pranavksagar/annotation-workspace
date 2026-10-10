import { useEffect, useRef } from 'react';

export function Dialog({title, children, onClose, actions, wide}: {
  title: string; children: React.ReactNode; onClose: () => void; actions: React.ReactNode; wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>('button, input, select, textarea')?.focus();
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.stopPropagation(); onClose(); } };
    window.addEventListener('keydown', key, true);
    return () => { window.removeEventListener('keydown', key, true); previous?.focus?.(); };
  }, [onClose]);
  return <div className="dialog-backdrop" onPointerDown={event => event.target === event.currentTarget && onClose()}>
    <div ref={ref} className={`dialog ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}>
      <h2>{title}</h2>
      <div className="dialog-body">{children}</div>
      <div className="dialog-actions">{actions}</div>
    </div>
  </div>;
}
