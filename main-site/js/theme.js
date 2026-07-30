/*
  Theme system: 7 brand colour swatches + light/dark mode.
  Default is always light + classic (#ccffcc), regardless of OS preference.
  Once the user picks something, it is persisted.

  Also carries the shared cross-page wiring: "Buy Augy a Coffee" link and
  service worker registration.
*/
(function (global) {
  const APP_KEY = 'malaysiaboleh';

  const COLOR_THEMES = [
    { id: 'classic', label: 'Classic', hex: '#ccffcc' },
    { id: 'not-green-1', label: 'Not green 1', hex: '#ffcccc' },
    { id: 'not-green-2', label: 'Not green 2', hex: '#ccccff' },
    { id: 'not-green-3', label: 'Not green 3', hex: '#ffffcc' },
    { id: 'not-green-4', label: 'Not green 4', hex: '#ffccff' },
    { id: 'not-green-5', label: 'Not green 5', hex: '#ccffff' },
    { id: 'really-light-green', label: 'Really really light green', hex: '#ffffff' }
  ];

  const STORAGE_KEY_COLOR = APP_KEY + '.colorTheme';
  const STORAGE_KEY_MODE = APP_KEY + '.mode';
  const LEGACY_KEY = 'mb-theme';

  function read(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }

  function write(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* storage unavailable */ }
  }

  /* Old single-key format used the same swatch ids, so it maps straight over. */
  function migrateLegacy() {
    const legacy = read(LEGACY_KEY);
    if (!legacy) return;
    if (!read(STORAGE_KEY_COLOR) && COLOR_THEMES.some(function (t) { return t.id === legacy; })) {
      write(STORAGE_KEY_COLOR, legacy);
    }
    try { localStorage.removeItem(LEGACY_KEY); } catch (e) { /* ignore */ }
  }

  function hexToRgb(hex) {
    const n = parseInt(hex.replace('#', ''), 16);
    return ((n >> 16) & 255) + ', ' + ((n >> 8) & 255) + ', ' + (n & 255);
  }

  function getStoredColorTheme() {
    const saved = read(STORAGE_KEY_COLOR);
    if (saved && COLOR_THEMES.some(function (t) { return t.id === saved; })) return saved;
    return 'classic';
  }

  function getStoredMode() {
    return read(STORAGE_KEY_MODE) === 'dark' ? 'dark' : 'light';
  }

  function applyColorTheme(id) {
    const theme = COLOR_THEMES.find(function (t) { return t.id === id; }) || COLOR_THEMES[0];
    document.documentElement.setAttribute('data-color-theme', theme.id);
    document.documentElement.style.setProperty('--brand', theme.hex);
    document.documentElement.style.setProperty('--brand-rgb', hexToRgb(theme.hex));
    write(STORAGE_KEY_COLOR, theme.id);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme.hex);
    return theme;
  }

  function applyMode(mode) {
    const resolved = mode === 'dark' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-mode', resolved);
    write(STORAGE_KEY_MODE, resolved);
    return resolved;
  }

  function initTheme() {
    migrateLegacy();
    applyColorTheme(getStoredColorTheme());
    applyMode(getStoredMode());
  }

  /* -- Modal wiring -- */

  function buildThemeModal() {
    const grid = document.getElementById('swatchGrid');
    if (!grid) return;

    grid.innerHTML = COLOR_THEMES.map(function (t) {
      return '<button class="swatch" data-theme-id="' + t.id + '" style="--swatch-color:' + t.hex +
        '" type="button" aria-label="' + t.label + '">' +
        '<span class="swatch-dot"></span>' +
        '<span class="swatch-label">' + t.label + '</span>' +
        '</button>';
    }).join('');

    syncThemeModalState();

    grid.addEventListener('click', function (e) {
      const btn = e.target.closest('[data-theme-id]');
      if (!btn) return;
      applyColorTheme(btn.dataset.themeId);
      syncThemeModalState();
    });

    const toggle = document.getElementById('modeToggle');
    if (toggle) {
      toggle.addEventListener('click', function (e) {
        const btn = e.target.closest('[data-mode]');
        if (!btn) return;
        applyMode(btn.dataset.mode);
        syncThemeModalState();
      });
    }
  }

  function syncThemeModalState() {
    const activeTheme = getStoredColorTheme();
    const activeMode = getStoredMode();
    document.querySelectorAll('#swatchGrid .swatch').forEach(function (el) {
      el.classList.toggle('active', el.dataset.themeId === activeTheme);
    });
    document.querySelectorAll('#modeToggle .mode-btn').forEach(function (el) {
      el.classList.toggle('active', el.dataset.mode === activeMode);
    });
    updateThemeButtonIcon();
  }

  function updateThemeButtonIcon() {
    const btn = document.getElementById('themeBtn');
    if (!btn) return;
    const span = btn.querySelector('[data-icon]');
    if (!span) return;
    span.setAttribute('data-icon', getStoredMode() === 'dark' ? 'moon' : 'sun');
    if (global.UI) global.UI.hydrateIcons(btn);
  }

  function wireModals() {
    document.querySelectorAll('[data-close-modal]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (global.UI) global.UI.closeModal(btn.dataset.closeModal);
      });
    });
    document.querySelectorAll('.modal-backdrop').forEach(function (backdrop) {
      backdrop.addEventListener('click', function (e) {
        if (e.target === backdrop && global.UI) global.UI.closeModal(backdrop.id);
      });
    });
    const themeBtn = document.getElementById('themeBtn');
    if (themeBtn) {
      themeBtn.addEventListener('click', function () {
        if (global.UI) global.UI.openModal('themeModal');
      });
    }
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      document.querySelectorAll('.modal-backdrop:not(.hidden)').forEach(function (backdrop) {
        if (global.UI) global.UI.closeModal(backdrop.id);
      });
    });
  }

  /* -- Shared page furniture -- */

  function wireCoffeeButton() {
    const coffee = document.getElementById('coffeeButton');
    if (!coffee) return;
    if (global.Icons && !coffee.dataset.iconSet) {
      coffee.innerHTML = global.Icons.html('coffee', { size: 18 }) +
        '<span class="btn-label">Buy Augy a Coffee</span>';
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

  initTheme();

  document.addEventListener('DOMContentLoaded', function () {
    /* Scoped to the theme UI; the page scripts own their own icon hydration. */
    const modal = document.getElementById('themeModal');
    if (global.UI && modal) global.UI.hydrateIcons(modal);
    updateThemeButtonIcon();
    buildThemeModal();
    wireModals();
    wireCoffeeButton();
    registerServiceWorker();
  });

  global.Theme = {
    COLOR_THEMES: COLOR_THEMES,
    applyColorTheme: applyColorTheme,
    applyMode: applyMode,
    getStoredColorTheme: getStoredColorTheme,
    getStoredMode: getStoredMode,
    initTheme: initTheme
  };
})(window);
