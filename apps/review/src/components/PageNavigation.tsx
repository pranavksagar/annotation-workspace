import { assetUrl } from '../lib/annotationApi';
import type { Page } from '../lib/annotationTypes';

export function PageNavigation({pages, activePageId, counts, onSelect}: {
  pages: Page[]; activePageId: string; counts: Record<string, number>; onSelect: (pageId: string) => void;
}) {
  if (pages.length < 2) return null;  // collapsed for single-page drawings
  return <nav className="page-nav" aria-label="Pages">
    {pages.map(page => <button key={page.id} className={page.id === activePageId ? 'active' : ''} onClick={() => onSelect(page.id)}
      aria-current={page.id === activePageId ? 'page' : undefined}>
      <img src={assetUrl(page.thumbnail_asset_id)} alt="" loading="lazy"/>
      <span>Page {page.page_number}</span>
      <small>{counts[page.id] ?? 0} labels</small>
    </button>)}
  </nav>;
}
