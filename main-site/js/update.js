/*
  Service worker registration and the update prompt bar.
  Classic script, exposed as window.Update to match the rest of the site.

  A new worker never activates on its own. It downloads, installs, and waits;
  the only thing that promotes it is a person pressing Reload in the bar
  below, which posts "skip-waiting" to the worker. The reload itself happens
  on controllerchange, once the new worker actually owns the page.
*/
(function (global) {
  const SW_URL = '/sw.js';
  const SITE_NAME = 'Malaysia Boleh';

  let registration = null;
  let waitingWorker = null;
  let reloading = false;
  /* Per page view only, never stored. "Not now" means not now. */
  let dismissed = false;

  /* -- The bar -- */

  function render() {
    const existing = document.querySelector('.update-notice');

    if (!waitingWorker || dismissed) {
      if (existing) existing.remove();
      return;
    }

    if (existing) return;

    const bar = document.createElement('div');
    bar.className = 'update-notice';
    bar.setAttribute('role', 'status');
    bar.setAttribute('aria-label', 'Update');

    const inner = document.createElement('div');
    inner.className = 'update-notice-inner';

    const text = document.createElement('p');
    text.textContent = 'A new version of ' + SITE_NAME + ' is ready.';

    const reload = document.createElement('button');
    reload.type = 'button';
    reload.className = 'btn btn-primary';
    reload.setAttribute('data-sw-update', '');
    reload.textContent = 'Reload';
    reload.addEventListener('click', function () {
      /* The only place anything asks for skipWaiting. The reload happens on
         controllerchange, not here. */
      if (waitingWorker) waitingWorker.postMessage('skip-waiting');
    });

    const later = document.createElement('button');
    later.type = 'button';
    later.className = 'btn';
    later.setAttribute('data-sw-later', '');
    later.textContent = 'Not now';
    later.addEventListener('click', function () {
      dismissed = true;
      render();
    });

    inner.appendChild(text);
    inner.appendChild(reload);
    inner.appendChild(later);
    bar.appendChild(inner);
    document.body.prepend(bar);
  }

  /* -- Noticing an update -- */

  function watchForUpdate() {
    if (!registration) return;

    /* A worker already waiting when the page opened. This is the ordinary
       case on the second page view after a deploy, and without it the prompt
       would only ever reach somebody who happened to have the page open at
       the moment the new worker finished installing. */
    if (registration.waiting && navigator.serviceWorker.controller) {
      waitingWorker = registration.waiting;
      render();
    }

    registration.addEventListener('updatefound', function () {
      const installing = registration.installing;
      if (!installing) return;

      installing.addEventListener('statechange', function () {
        /* "installed" with a controller present means an update. "installed"
           with no controller is a first install, which has nothing to prompt
           about: there is no previous version on screen to protect. */
        if (installing.state === 'installed' && navigator.serviceWorker.controller) {
          waitingWorker = registration.waiting || installing;
          render();
        }
      });
    });

    /* The browser only checks sw.js on navigation, so a tab left open across
       a deploy would never hear about it. Coming back to the tab is the
       moment to look. */
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') {
        registration.update().catch(function () { /* offline, try next time */ });
      }
    });
  }

  function registerWorker() {
    if (!('serviceWorker' in navigator)) return;

    navigator.serviceWorker
      .register(SW_URL)
      .then(function (reg) {
        registration = reg;
        watchForUpdate();
      })
      .catch(function (cause) {
        /* A refused registration is not a reason to break the page. Private
           browsing in some browsers, and any http origin that is not
           localhost, land here. */
        console.warn('service worker registration failed:', cause);
      });

    /* The swap, once somebody has accepted it. Reloading here rather than in
       the click handler is what makes the page come back on the new version:
       the controller has changed by this point, so the reload is served by
       the new worker and not the one being replaced. */
    navigator.serviceWorker.addEventListener('controllerchange', function () {
      if (reloading) return;
      reloading = true;
      window.location.reload();
    });
  }

  /* Registration on load, not immediately: installing fetches everything the
     worker precaches, and starting that while the page is still fetching its
     own assets makes a first visit slower for no gain. */
  if (document.readyState === 'complete') registerWorker();
  else window.addEventListener('load', registerWorker, { once: true });

  global.Update = { render: render };
})(window);
