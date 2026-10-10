import { useEffect, useState } from 'react';
import { AnnotationPage } from './pages/AnnotationPage';
import { DrawingsPage } from './pages/DrawingsPage';

type Route = {name: 'drawings'} | {name: 'drawing'; id: string};

export function parseRoute(hash: string): Route {
  const match = /^#\/drawings\/([0-9a-f-]{36})$/i.exec(hash);
  return match ? {name: 'drawing', id: match[1].toLowerCase()} : {name: 'drawings'};
}

export function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  useEffect(() => {
    if (!window.location.hash) window.history.replaceState(null, '', '#/drawings');
    const change = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener('hashchange', change);
    return () => window.removeEventListener('hashchange', change);
  }, []);
  const go = (hash: string) => { window.location.hash = hash; };
  if (route.name === 'drawing') return <AnnotationPage key={route.id} drawingId={route.id} onBack={() => go('#/drawings')}/>;
  return <DrawingsPage onOpen={id => go(`#/drawings/${id}`)}/>;
}
