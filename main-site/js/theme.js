/*
  Shared across every page: theme picker (modal + persistence),
  "Buy Augy a Coffee" link wiring, and service worker registration.
*/
(function () {
  const STORAGE_KEY = 'mb-theme';
  const DEFAULT_THEME = 'classic';

  const THEMES = [
    { id: 'classic', label: 'Classic', swatch: '#ccffcc' },
    { id: 'not-green-1', label: 'Not green 1', swatch: '#ffcccc' },
    { id: 'not-green-2', label: 'Not green 2', swatch: '#ccccff' },
    { id: 'not-green-3', label: 'Not green 3', swatch: '#ffffcc' },
    { id: 'not-green-4', label: 'Not green 4', swatch: '#ffccff' },
    { id: 'not-green-5', label: 'Not green 5', swatch: '#ccffff' },
    { id: 'really-light-green', label: 'Really really light green', swatch: '#ffffff' }
  ];

  function getSavedTheme() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved && THEMES.some(function (t) { return t.id === saved; })) return saved;
    } catch (e) { /* localStorage unavailable */ }
    return DEFAULT_THEME;
  }

  function applyTheme(id) {
    document.documentElement.setAttribute('data-theme', id);
    const meta = document.querySelector('meta[name="theme-color"]');
    const theme = THEMES.find(function (t) { return t.id === id; }) || THEMES[0];
    if (meta) meta.setAttribute('content', theme.swatch);
    try { localStorage.setItem(STORAGE_KEY, id); } catch (e) { /* ignore */ }
  }

  applyTheme(getSavedTheme());

  function buildModal() {
    if (document.getElementById('themeModal')) return;

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'themeModal';

    const optionsHtml = THEMES.map(function (t) {
      return '<button type="button" class="theme-option" data-theme-id="' + t.id + '" aria-pressed="false">' +
        '<span class="theme-swatch" style="background:' + t.swatch + '"></span>' +
        '<span>' + t.label + '</span>' +
        '<span class="theme-check">' + (window.Icons ? window.Icons.html('check', { size: 16 }) : '') + '</span>' +
        '</button>';
    }).join('');

    overlay.innerHTML =
      '<div class="modal-panel glass" role="dialog" aria-modal="true" aria-labelledby="themeModalTitle">' +
      '<div class="modal-header">' +
      '<h2 id="themeModalTitle">Choose a theme</h2>' +
      '<button type="button" class="modal-close" id="themeModalClose" aria-label="Close">' +
      (window.Icons ? window.Icons.html('close', { size: 18 }) : 'x') +
      '</button>' +
      '</div>' +
      '<p class="modal-sub">Pick an accent colour. Everything stays light theme.</p>' +
      '<div class="theme-grid">' + optionsHtml + '</div>' +
      '</div>';

    document.body.appendChild(overlay);

    function close() { overlay.classList.remove('open'); }
    function open() {
      syncPressedState();
      overlay.classList.add('open');
    }

    function syncPressedState() {
      const current = getSavedTheme();
      overlay.querySelectorAll('.theme-option').forEach(function (btn) {
        btn.setAttribute('aria-pressed', String(btn.dataset.themeId === current));
      });
    }

    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) close();
    });
    document.getElementById('themeModalClose').addEventListener('click', close);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && overlay.classList.contains('open')) close();
    });
    overlay.querySelectorAll('.theme-option').forEach(function (btn) {
      btn.addEventListener('click', function () {
        applyTheme(btn.dataset.themeId);
        syncPressedState();
      });
    });

    window.__openThemeModal = open;
  }

  function wireThemeToggle() {
    const toggle = document.getElementById('themeToggle');
    if (!toggle) return;
    if (window.Icons && !toggle.dataset.iconSet) {
      toggle.innerHTML = window.Icons.html('sun', { size: 18 }) + '<span class="btn-label">Theme</span>';
      toggle.dataset.iconSet = '1';
      toggle.setAttribute('aria-label', 'Choose theme');
    }
    buildModal();
    toggle.addEventListener('click', function () {
      window.__openThemeModal();
    });
  }

  function wireCoffeeButton() {
    const coffee = document.getElementById('coffeeButton');
    if (!coffee) return;
    if (window.Icons && !coffee.dataset.iconSet) {
      coffee.innerHTML = window.Icons.html('coffee', { size: 18 }) + '<span class="btn-label">Buy Augy a Coffee</span>';
      coffee.dataset.iconSet = '1';
      coffee.setAttribute('aria-label', 'Buy Augy a Coffee (opens in a new tab)');
    }
    if (!coffee.getAttribute('href')) {
      coffee.setAttribute('href', 'https://donate.stripe.com/28o2akeAr3hv0DK6oo');
    }
    coffee.setAttribute('target', '_blank');
    coffee.setAttribute('rel', 'noopener noreferrer');
  }

  function registerServiceWorker() {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(function () { /* offline install, ignore */ });
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    wireThemeToggle();
    wireCoffeeButton();
    registerServiceWorker();
  });

  window.MBTheme = { THEMES: THEMES, applyTheme: applyTheme, getSavedTheme: getSavedTheme };
})();
