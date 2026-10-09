/*
  Full screen for a Leaflet map, with the page's nav as a floating tray.
  Classic script, exposed as window.MapFullscreen to match the rest of the site.

  A button under the zoom control fills the screen with the map. While it is
  full screen, the page's tabs, refresh, Telegram bot and theme buttons float
  top right as a column of glass buttons that slides off the edge behind an
  arrow tab: sg-psi's tray, which is wordrain-game's.

  The map element is moved to <body> for the duration. Its card is .glass, and
  backdrop-filter makes any ancestor the containing block for position: fixed,
  so left where it is the map would fill the card, not the screen.
*/
(function (global) {
  const TRAY_KEY = 'malaysiaboleh.trayOpen';

  let active = null;
  let tray = null;

  function icon(name, size) {
    return global.Icons ? global.Icons.html(name, { size: size || 20 }) : '';
  }

  function readTrayOpen() {
    try { return localStorage.getItem(TRAY_KEY) !== '0'; } catch (e) { return true; }
  }

  /* -- The tray -- */

  function glassButton(label, iconName) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'glass-btn';
    btn.setAttribute('aria-label', label);
    btn.title = label;
    btn.innerHTML = icon(iconName, 22);
    return btn;
  }

  // The tab's own icon and words, read off the page so the tray never drifts
  // from the tabs it stands in for.
  function tabParts(tab) {
    const iconEl = tab.querySelector('[data-icon]');
    return {
      label: tab.textContent.replace(/\s+/g, ' ').trim(),
      icon: iconEl ? iconEl.getAttribute('data-icon') : 'chevronRight'
    };
  }

  function buildTray(opts) {
    const hud = document.createElement('div');
    hud.className = 'hud-right';

    const box = document.createElement('div');
    box.className = 'tray';

    const tab = document.createElement('button');
    tab.type = 'button';
    tab.className = 'tray-tab';
    tab.setAttribute('aria-controls', 'trayButtons');
    tab.innerHTML = icon('chevronRight', 16);

    const buttons = document.createElement('div');
    buttons.className = 'tray-buttons';
    buttons.id = 'trayButtons';

    const views = document.createElement('nav');
    views.className = 'tray-views';
    views.setAttribute('aria-label', 'Sections');
    opts.tabs.forEach(function (pageTab) {
      const parts = tabParts(pageTab);
      const btn = glassButton(parts.label, parts.icon);
      btn.setAttribute('aria-pressed', String(pageTab.getAttribute('aria-selected') === 'true'));
      btn.addEventListener('click', function () {
        if (pageTab.getAttribute('aria-selected') === 'true') return;
        // Another section has no map to fill the screen with.
        exit();
        pageTab.click();
      });
      views.appendChild(btn);
    });
    buttons.appendChild(views);

    const rule = document.createElement('span');
    rule.className = 'tray-rule';
    rule.setAttribute('aria-hidden', 'true');
    buttons.appendChild(rule);

    if (opts.refresh) {
      const refresh = glassButton('Refresh', 'refresh');
      refresh.addEventListener('click', function () { opts.refresh.click(); });
      buttons.appendChild(refresh);
    }

    const bot = document.getElementById('botButton');
    if (bot && bot.getAttribute('href')) {
      const link = document.createElement('a');
      link.className = 'glass-btn';
      link.href = bot.getAttribute('href');
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.setAttribute('aria-label', bot.getAttribute('aria-label') || 'Telegram bot');
      link.title = bot.getAttribute('title') || 'Telegram bot';
      link.innerHTML = icon('telegram', 22);
      buttons.appendChild(link);
    }

    const theme = glassButton('Theme', 'palette');
    theme.addEventListener('click', function () {
      if (global.UI) global.UI.openModal('themeModal');
    });
    buttons.appendChild(theme);

    const leave = glassButton('Exit full screen', 'minimize');
    leave.addEventListener('click', exit);
    buttons.appendChild(leave);

    box.appendChild(tab);
    box.appendChild(buttons);
    hud.appendChild(box);

    tab.addEventListener('click', function () {
      setTrayOpen(box.classList.contains('collapsed'));
    });

    return { hud: hud, box: box, tab: tab, buttons: buttons };
  }

  /* Open until somebody closes it; the choice is a per-browser convenience,
     so storage failing only means it opens again next time. */
  function setTrayOpen(open, save) {
    if (!tray) return;
    tray.box.classList.toggle('collapsed', !open);
    tray.tab.setAttribute('aria-expanded', String(open));
    tray.tab.setAttribute('aria-label', open ? 'Hide menu' : 'Show menu');
    // Off screen buttons must not take focus.
    tray.buttons.inert = !open;
    if (save !== false) {
      try { localStorage.setItem(TRAY_KEY, open ? '1' : '0'); } catch (e) { /* this view only */ }
    }
  }

  /* -- Entering and leaving -- */

  function enter(entry) {
    if (active) return;
    active = entry;

    const el = entry.map.getContainer();
    entry.placeholder = document.createElement('div');
    entry.placeholder.hidden = true;
    el.parentNode.insertBefore(entry.placeholder, el);
    document.body.appendChild(el);

    el.classList.add('is-map-fullscreen');
    document.body.classList.add('map-fullscreen');
    entry.map.scrollWheelZoom.enable();
    setButton(entry, true);

    tray = buildTray(entry.opts);
    document.body.appendChild(tray.hud);
    setTrayOpen(readTrayOpen(), false);

    // Where the browser allows it, the browser's own chrome goes too. iPhone
    // Safari has no full screen for pages, and gets the filled viewport alone.
    const root = document.documentElement;
    if (root.requestFullscreen && !document.fullscreenElement) {
      root.requestFullscreen({ navigationUI: 'hide' }).catch(function () { /* the viewport will do */ });
    }

    entry.map.invalidateSize();
  }

  function exit() {
    if (!active) return;
    const entry = active;
    active = null;

    const el = entry.map.getContainer();
    el.classList.remove('is-map-fullscreen');
    document.body.classList.remove('map-fullscreen');
    entry.placeholder.parentNode.replaceChild(el, entry.placeholder);
    entry.placeholder = null;
    entry.map.scrollWheelZoom.disable();
    setButton(entry, false);

    if (tray) {
      tray.hud.remove();
      tray = null;
    }
    if (document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen().catch(function () { /* already out */ });
    }

    entry.map.invalidateSize();
    entry.button.focus();
  }

  function setButton(entry, on) {
    const label = on ? 'Exit full screen' : 'Full screen';
    entry.button.innerHTML = icon(on ? 'minimize' : 'maximize', 18);
    entry.button.setAttribute('aria-label', label);
    entry.button.title = label;
    entry.button.setAttribute('aria-pressed', String(on));
  }

  /* -- Wiring -- */

  /*
    map:     the Leaflet map
    tabs:    the page's tab buttons, in order, each with a [data-icon] and words
    refresh: the button that reloads the map's data (optional)
  */
  function attach(map, opts) {
    if (!global.L || !map) return;
    const entry = { map: map, opts: { tabs: Array.prototype.slice.call(opts.tabs || []), refresh: opts.refresh || null } };

    const Control = global.L.Control.extend({
      options: { position: 'topleft' },
      onAdd: function () {
        const bar = global.L.DomUtil.create('div', 'leaflet-bar leaflet-control map-fullscreen-control');
        const btn = global.L.DomUtil.create('a', '', bar);
        btn.href = '#';
        btn.setAttribute('role', 'button');
        entry.button = btn;
        setButton(entry, false);
        global.L.DomEvent.disableClickPropagation(bar);
        global.L.DomEvent.on(btn, 'click', function (e) {
          global.L.DomEvent.preventDefault(e);
          if (active === entry) exit();
          else enter(entry);
        });
        return bar;
      }
    });
    map.addControl(new Control());
  }

  // Escape leaves, unless it is closing a modal opened from the tray. Capture
  // phase on window, so this sees the modal still open before theme.js's own
  // Escape handler closes it.
  global.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || !active) return;
    if (document.querySelector('.modal-backdrop:not(.hidden)')) return;
    exit();
  }, true);

  // The browser's own Escape, or its "exit full screen" UI, ends ours too.
  document.addEventListener('fullscreenchange', function () {
    if (!document.fullscreenElement && active) exit();
  });

  global.MapFullscreen = { attach: attach, exit: exit };
})(window);
