/*
  Shared UI helpers: icon hydration and modal open/close.
  Classic script, exposed as window.UI to match the rest of the site.
*/
(function (global) {
  /*
    Safe to call repeatedly; re-renders only when data-icon changes.
    Also sets data-icon-set, the guard the page scripts use, so bus.js and
    trains.js leave anything hydrated here alone.
  */
  function hydrateIcons(root) {
    root = root || document;
    root.querySelectorAll('[data-icon]').forEach(function (el) {
      const name = el.dataset.icon;
      if (el.dataset.iconRendered === name) return;
      el.innerHTML = global.Icons ? global.Icons.icon(name) : '';
      el.dataset.iconRendered = name;
      el.dataset.iconSet = '1';
    });
  }

  function openModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('hidden');
    document.body.classList.add('modal-open');
  }

  function closeModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.add('hidden');
    if (!document.querySelector('.modal-backdrop:not(.hidden)')) {
      document.body.classList.remove('modal-open');
    }
  }

  global.UI = { hydrateIcons: hydrateIcons, openModal: openModal, closeModal: closeModal };
})(window);
