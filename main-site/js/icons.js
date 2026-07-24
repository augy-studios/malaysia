/*
  Shared inline SVG icon set. Every icon is stroke-based (currentColor) so it
  inherits colour from its container - no emoji anywhere on the site.
  Usage: Icons.html('sun', {size: 20, className: 'icon'})
*/
(function (global) {
  const PATHS = {
    palette: '<circle cx="12" cy="12" r="9"/><circle cx="8.5" cy="10.5" r="1" fill="currentColor" stroke="none"/><circle cx="12" cy="7.5" r="1" fill="currentColor" stroke="none"/><circle cx="15.5" cy="10.5" r="1" fill="currentColor" stroke="none"/><circle cx="14" cy="15" r="1" fill="currentColor" stroke="none"/><path d="M12 21a9 9 0 0 1 0-18v0a2 2 0 0 1 2 2 2 2 0 0 0 2 2h1a3 3 0 0 1 3 3 3 3 0 0 1-3 3h-.5a1.5 1.5 0 0 0 0 3H17a2 2 0 0 1 2 2 3 3 0 0 1-3 3"/>',
    coffee: '<path d="M17 8h1a3 3 0 0 1 0 6h-1"/><path d="M3 8h14v6a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V8z"/><line x1="6" y1="2" x2="6" y2="4"/><line x1="10" y1="2" x2="10" y2="4"/><line x1="14" y1="2" x2="14" y2="4"/>',
    close: '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/>',
    check: '<polyline points="20 6 9 17 4 12"/>',
    chevronRight: '<polyline points="9 18 15 12 9 6"/>',
    chevronLeft: '<polyline points="15 18 9 12 15 6"/>',
    externalLink: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
    refresh: '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    clock: '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/>',
    alertTriangle: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    alertOctagon: '<polygon points="7.86 2 16.14 2 22 7.86 22 16.14 16.14 22 7.86 22 2 16.14 2 7.86 7.86 2"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
    sun: '<circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="2" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>',
    cloud: '<path d="M17.5 19H8a4.5 4.5 0 1 1 1.06-8.87A6 6 0 0 1 20 12a4 4 0 0 1 0 8 3 3 0 0 1-.5-.02"/>',
    cloudSun: '<path d="M12 2v2M4.93 4.93l1.41 1.41M2 12h2M21.17 8a5 5 0 1 0-7.7 5.7"/><path d="M12.5 20h4.5a3.5 3.5 0 0 0 .4-6.98A5.5 5.5 0 0 0 7 14.5a3.5 3.5 0 0 0 .5 6.98"/>',
    cloudRain: '<path d="M17.5 15H8a4.5 4.5 0 1 1 1.06-8.87A6 6 0 0 1 20 8a4 4 0 0 1 0 8"/><line x1="9" y1="18" x2="9" y2="21"/><line x1="13" y1="18" x2="13" y2="21"/><line x1="17" y1="18" x2="17" y2="21"/>',
    cloudLightning: '<path d="M17.5 13H8a4.5 4.5 0 1 1 1.06-8.87A6 6 0 0 1 20 6a4 4 0 0 1 0 8h-1"/><polyline points="12.5 15 10.5 19 13 19 11 23"/>',
    wind: '<path d="M3 8h9.5a2.5 2.5 0 1 0-2.4-3.2"/><path d="M3 12h13.5a2.5 2.5 0 1 1-2.4 3.2"/><path d="M3 16h7.5a2 2 0 1 1-1.9 2.6"/>',
    droplet: '<path d="M12 2s6 7 6 11a6 6 0 0 1-12 0c0-4 6-11 6-11z"/>',
    waves: '<path d="M2 12c1.5-2 3.5-2 5 0s3.5 2 5 0 3.5-2 5 0 3.5 2 5 0"/><path d="M2 18c1.5-2 3.5-2 5 0s3.5 2 5 0 3.5-2 5 0 3.5 2 5 0"/>',
    activity: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    train: '<rect x="4" y="3" width="16" height="13" rx="3"/><path d="M4 11h16"/><path d="M9 16 6 21"/><path d="M15 16l3 5"/><circle cx="8.5" cy="13.5" r="0.6" fill="currentColor" stroke="none"/><circle cx="15.5" cy="13.5" r="0.6" fill="currentColor" stroke="none"/>',
    mapPin: '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0z"/><circle cx="12" cy="10" r="3"/>',
    home: '<path d="M3 11.5 12 4l9 7.5"/><path d="M5 10v9a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1v-9"/>',
    signal: '<line x1="4" y1="20" x2="4" y2="16"/><line x1="9" y1="20" x2="9" y2="12"/><line x1="14" y1="20" x2="14" y2="8"/><line x1="19" y1="20" x2="19" y2="4"/>',
    bus: '<rect x="4" y="4" width="16" height="12" rx="2"/><path d="M4 12h16"/><path d="M7 16v3"/><path d="M17 16v3"/><circle cx="8" cy="19" r="1" fill="currentColor" stroke="none"/><circle cx="16" cy="19" r="1" fill="currentColor" stroke="none"/><line x1="8" y1="7" x2="8" y2="9"/><line x1="16" y1="7" x2="16" y2="9"/>'
  };

  function html(name, opts) {
    opts = opts || {};
    const size = opts.size || 20;
    const cls = opts.className ? ' ' + opts.className : '';
    const body = PATHS[name] || PATHS.alertTriangle;
    return '<svg class="icon' + cls + '" width="' + size + '" height="' + size +
      '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + body + '</svg>';
  }

  global.Icons = { html: html, PATHS: PATHS };
})(window);
