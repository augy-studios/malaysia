/* Homepage directory - renders the page grid linking to every sub-app. */
(function () {
  const PAGES = [
    {
      href: '/weather/',
      icon: 'cloudSun',
      title: '7-Day Weather Forecast',
      desc: 'District-level forecasts straight from MET Malaysia - temperature, rain chance and conditions.'
    },
    {
      href: '/quake/',
      icon: 'activity',
      title: 'Earthquake Warnings',
      desc: 'Live tremor alerts from MET Malaysia, covering Sabah and the wider region.'
    },
    {
      href: '/flood-alerts/',
      icon: 'waves',
      title: 'Flood Alerts',
      desc: 'River water levels and flood warnings from JPS across Malaysia.'
    },
    {
      href: '/trains/',
      icon: 'train',
      title: 'Trains & Transit',
      desc: 'Rapid KL (LRT/MRT/monorail) and KTMB schedules, plus live KTMB train positions.'
    },
    {
      href: '/bus/',
      icon: 'bus',
      title: 'Buses',
      desc: 'Rapid KL/Penang/Kuantan and myBAS Johor Bahru bus schedules, plus live bus positions.'
    }
  ];

  function render() {
    const grid = document.getElementById('pageGrid');
    if (!grid) return;

    grid.innerHTML = PAGES.map(function (p) {
      return '<a class="page-card glass" href="' + p.href + '">' +
        '<span class="page-card-icon">' + (window.Icons ? window.Icons.html(p.icon, { size: 22 }) : '') + '</span>' +
        '<h3>' + p.title + '</h3>' +
        '<p>' + p.desc + '</p>' +
        '<span class="page-card-link">Open' + (window.Icons ? window.Icons.html('chevronRight', { size: 14 }) : '') + '</span>' +
        '</a>';
    }).join('');
  }

  document.addEventListener('DOMContentLoaded', render);
})();
