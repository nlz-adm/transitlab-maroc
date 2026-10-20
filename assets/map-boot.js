// Petit point d'entrée différé : la page peut peindre son interface avant de
// parser le moteur cartographique et ses données volumineuses.
const start = () => import('./map-app.js?v=maroc-v9-20261021').catch((error) => {
  const map = document.getElementById('map');
  if (!map) return;
  map.textContent = 'Impossible de charger le moteur de la carte. ' + (error?.message || '');
  map.style.cssText = 'padding:24px;color:#a42c36;font:13px system-ui';
  console.error('TransitLab map boot:', error);
});

if ('requestIdleCallback' in window) {
  window.requestIdleCallback(start, { timeout: 180 });
} else {
  window.setTimeout(start, 0);
}
