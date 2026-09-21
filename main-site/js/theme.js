/*
  Theme system: 7 brand colour swatches + light/dark/time-based mode.
  Default is always light + classic (#ccffcc), regardless of OS preference.
  Once the user picks something, it is persisted.

  Also carries the shared cross-page wiring for the "Buy Augy a Coffee" link.
  Service worker registration and the update bar live in js/update.js.
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

  /* Mode preference and mode are different things. The preference is what the
     person chose and can be "time"; the mode is what the document is in and
     is only ever light or dark. */

  const MODE_PREFERENCES = ['light', 'dark', 'time'];

  /* The daylight window. Duplicated in the pre-paint script in every page's
     head, which has to resolve this before first paint and cannot import
     anything. Change both together. */
  const LIGHT_FROM_HOUR = 9;
  const LIGHT_UNTIL_HOUR = 18;

  function getModePreference() {
    const v = read(STORAGE_KEY_MODE);
    return MODE_PREFERENCES.indexOf(v) !== -1 ? v : 'light';
  }

  function isDaylightHours(now) {
    const hour = (now || new Date()).getHours();
    return hour >= LIGHT_FROM_HOUR && hour < LIGHT_UNTIL_HOUR;
  }

  function resolveMode(preference) {
    if (preference === 'time') return isDaylightHours() ? 'light' : 'dark';
    return preference === 'dark' ? 'dark' : 'light';
  }

  /* The mode the document is in right now, resolved. What the theme button
     icon and anything else reading the active mode wants. */
  function getStoredMode() {
    return resolveMode(getModePreference());
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

  function applyMode(preference) {
    const chosen = MODE_PREFERENCES.indexOf(preference) !== -1 ? preference : 'light';
    const resolved = resolveMode(chosen);

    document.documentElement.setAttribute('data-mode', resolved);
    document.documentElement.setAttribute('data-mode-preference', chosen);
    /* The preference, never the resolved value: storing "dark" on a winter
       evening would silently end the setting the person actually chose. */
    write(STORAGE_KEY_MODE, chosen);

    scheduleModeCheck();

    return resolved;
  }

  /* -- Keeping the time based mode honest while the page stays open -- */

  let modeTimer = null;
  let watchingVisibility = false;

  /* Milliseconds until the next 09:00 or 18:00, whichever comes first. */
  function msUntilNextBoundary(now) {
    now = now || new Date();
    const next = new Date(now);
    next.setMinutes(0, 0, 0);

    const hour = now.getHours();
    if (hour < LIGHT_FROM_HOUR) {
      next.setHours(LIGHT_FROM_HOUR);
    } else if (hour < LIGHT_UNTIL_HOUR) {
      next.setHours(LIGHT_UNTIL_HOUR);
    } else {
      next.setDate(next.getDate() + 1);
      next.setHours(LIGHT_FROM_HOUR);
    }

    /* A second of slack, so a timer that fires a fraction early does not land
       back in the hour it just left and reschedule itself in a tight loop. */
    return Math.max(1000, next.getTime() - now.getTime() + 1000);
  }

  function scheduleModeCheck() {
    if (modeTimer !== null) {
      clearTimeout(modeTimer);
      modeTimer = null;
    }

    if (getModePreference() !== 'time') return;

    modeTimer = setTimeout(function () {
      modeTimer = null;
      refreshTimeMode();
    }, msUntilNextBoundary());

    /* A device that sleeps through the boundary fires its timer late, and
       coming back to the tab is the moment to notice. */
    if (!watchingVisibility && typeof document !== 'undefined') {
      watchingVisibility = true;
      document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') refreshTimeMode();
      });
    }
  }

  function refreshTimeMode() {
    if (getModePreference() !== 'time') return;

    const resolved = resolveMode('time');
    const current = document.documentElement.getAttribute('data-mode');

    if (resolved !== current) {
      document.documentElement.setAttribute('data-mode', resolved);
      document.dispatchEvent(new CustomEvent('uwu:modechange', {
        detail: { mode: resolved, preference: 'time' }
      }));
    }

    scheduleModeCheck();
  }

  function initTheme() {
    migrateLegacy();
    applyColorTheme(getStoredColorTheme());
    /* The preference, not the resolved mode. Passing the resolved one would
       quietly rewrite a stored "time" into "dark" the first evening. */
    applyMode(getModePreference());
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

    /* A tab left open across 09:00 or 18:00 re-resolves itself; redraw the
       modal so the note and pressed state stay in step with the change. */
    document.addEventListener('uwu:modechange', syncThemeModalState);
  }

  function syncThemeModalState() {
    const activeTheme = getStoredColorTheme();
    const activePreference = getModePreference();
    const resolvedMode = getStoredMode();

    document.querySelectorAll('#swatchGrid .swatch').forEach(function (el) {
      el.classList.toggle('active', el.dataset.themeId === activeTheme);
    });
    document.querySelectorAll('#modeToggle .mode-btn').forEach(function (el) {
      const isActive = el.dataset.mode === activePreference;
      el.classList.toggle('active', isActive);
      el.setAttribute('aria-pressed', String(isActive));
    });

    const note = document.getElementById('modeNote');
    if (note) {
      note.hidden = activePreference !== 'time';
      if (activePreference === 'time') {
        note.textContent = 'Following the clock. Currently ' + resolvedMode + '.';
      }
    }

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

  /* Every page except the home directory points at the Telegram bot covering
     the same data. The page declares which one via data-bot; the label, icon
     and href are filled in here so the five pages stay in step. */
  const BOTS = {
    weather: { username: 'malaysiaweather_bot', label: 'Weather Bot' },
    trains: { username: 'malaysiatrains_bot', label: 'Trains Bot' },
    buses: { username: 'malaysiabuses_bot', label: 'Buses Bot' }
  };

  function wireBotButton() {
    const link = document.getElementById('botButton');
    if (!link) return;

    const bot = BOTS[link.dataset.bot];
    if (!bot) {
      link.remove();
      return;
    }

    link.setAttribute('href', 'https://t.me/' + bot.username);
    link.setAttribute('target', '_blank');
    link.setAttribute('rel', 'noopener noreferrer');
    link.setAttribute('title', '@' + bot.username);
    link.setAttribute('aria-label', bot.label + ' on Telegram, @' + bot.username +
      ' (opens in a new tab)');

    if (global.Icons && !link.dataset.iconSet) {
      link.innerHTML = global.Icons.html('telegram', { size: 18 }) +
        '<span class="btn-label">' + bot.label + '</span>';
      link.dataset.iconSet = '1';
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
    wireBotButton();
  });

  global.Theme = {
    COLOR_THEMES: COLOR_THEMES,
    MODE_PREFERENCES: MODE_PREFERENCES,
    LIGHT_FROM_HOUR: LIGHT_FROM_HOUR,
    LIGHT_UNTIL_HOUR: LIGHT_UNTIL_HOUR,
    applyColorTheme: applyColorTheme,
    applyMode: applyMode,
    getStoredColorTheme: getStoredColorTheme,
    getStoredMode: getStoredMode,
    getModePreference: getModePreference,
    isDaylightHours: isDaylightHours,
    resolveMode: resolveMode,
    refreshTimeMode: refreshTimeMode,
    initTheme: initTheme
  };
})(window);
