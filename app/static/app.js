// StatRep page behaviour. Kept out of the HTML so the CSP can forbid inline scripts.
(function () {
  var STATUS_RANK = {DOWN: 0, SLOW: 1, UP: 2, PENDING: 3};
  var LABELS = {UP: 'Operational', SLOW: 'Slow', DOWN: 'Down'};

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  function iconHTML(name) { return '<svg class="i" aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }
  function setBusy(btn, busy, idleIcon, idleText) {
    btn.disabled = busy;
    btn.classList.toggle('is-loading', busy);
    btn.querySelector('use').setAttribute('href', busy ? '#i-loader' : idleIcon);
    btn.querySelector('span').textContent = busy ? (idleText ? 'Checking…' : '') : idleText;
  }
  function postJSON(url, body) {
    return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.d.error || 'Check failed.');
        return res.d;
      });
  }
  function showError(box, message) {
    box.innerHTML = iconHTML('x') + esc(message);
    box.hidden = false;
  }
  function toast(message, level) {
    var box = document.getElementById('toasts');
    var el = document.createElement('div');
    el.className = 'toast toast-' + (level || 'info');
    el.innerHTML = iconHTML({up: 'check', slow: 'alert', down: 'x'}[level] || 'check') + '<span>' + esc(message) + '</span>';
    box.appendChild(el);
    setTimeout(function () { el.classList.add('is-leaving'); }, 4500);
    setTimeout(function () { el.remove(); }, 5000);
  }
  function byKey(container, key) {
    return container && container.querySelector('[data-key="' + CSS.escape(key) + '"]');
  }

  document.getElementById('run-form').addEventListener('submit', function (e) {
    var form = this, clicked = e.submitter || form.querySelector('[type=submit]');
    clicked.classList.add('is-loading');
    clicked.querySelector('use').setAttribute('href', '#i-loader');
    clicked.querySelector('span').textContent = 'Starting…';
    // Disable after the browser has collected the clicked button's value (disabled ones are left out)
    setTimeout(function () { form.querySelectorAll('button').forEach(function (b) { b.disabled = true; }); }, 0);
  });

  // Favicons that fail to load are removed, leaving the globe icon (no inline onerror: strict CSP)
  document.addEventListener('error', function (e) {
    if (e.target.tagName === 'IMG' && e.target.parentNode.classList.contains('favicon')) e.target.remove();
  }, true);
  document.querySelectorAll('.favicon img').forEach(function (img) {
    if (img.complete && img.naturalWidth === 0) img.remove();  // failed before this script ran
  });

  document.addEventListener('click', function (e) {
    document.querySelectorAll('details.menu[open]').forEach(function (m) { if (!m.contains(e.target)) m.removeAttribute('open'); });
  });

  // ---------- Website list: search, filter, sort ----------
  var list = document.getElementById('site-list');
  var search = document.getElementById('site-search');
  var sortSel = document.getElementById('site-sort');
  var empty = document.getElementById('site-empty');
  var filter = 'all';

  function rows() { return Array.prototype.slice.call(list.children).filter(function (el) { return el.dataset.key !== undefined; }); }
  function matchesFilter(row) {
    if (filter === 'all') return true;
    if (filter === 'expiring') return !!row.dataset.expiring;
    return row.dataset.status === filter;
  }
  function applyFilter() {
    var q = search.value.trim().toLowerCase();
    var shown = 0;
    rows().forEach(function (row) {
      var ok = matchesFilter(row) && (!q || row.dataset.search.indexOf(q) !== -1);
      row.hidden = !ok;
      if (ok) shown++;
    });
    empty.hidden = shown > 0;
  }
  function updateCounts() {
    var counts = {all: 0, DOWN: 0, SLOW: 0, UP: 0, expiring: 0};
    rows().forEach(function (row) {
      counts.all++;
      if (counts[row.dataset.status] !== undefined) counts[row.dataset.status]++;
      if (row.dataset.expiring) counts.expiring++;
    });
    document.querySelectorAll('[data-count]').forEach(function (b) {
      var n = counts[b.dataset.count];
      b.textContent = n;
      b.parentNode.classList.toggle('is-zero', !n && b.dataset.count !== 'all');
    });
  }
  function applySort() {
    var mode = sortSel.value;
    var num = function (v, missing) { return v === '' || v === undefined ? missing : +v; };
    rows().map(function (row, i) { return {row: row, i: i}; }).sort(function (a, b) {
      var x = a.row.dataset, y = b.row.dataset, d = 0;
      if (mode === 'name') d = x.name.localeCompare(y.name);
      else if (mode === 'ms') d = num(y.ms, -1) - num(x.ms, -1);
      else if (mode === 'config') d = num(x.order, 1e9) - num(y.order, 1e9);
      else d = (STATUS_RANK[x.status] - STATUS_RANK[y.status]) || num(y.ms, -1) - num(x.ms, -1);
      return d || a.i - b.i;
    }).forEach(function (o) { list.appendChild(o.row); });
  }
  function setFilter(value) {
    filter = value;
    document.querySelectorAll('.filter').forEach(function (b) {
      var on = b.dataset.filter === value;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    applyFilter();
  }

  document.querySelectorAll('.filter').forEach(function (b) {
    b.addEventListener('click', function () { setFilter(b.dataset.filter); });
  });
  search.addEventListener('input', applyFilter);
  sortSel.addEventListener('change', function () {
    applySort();
    try { localStorage.setItem('statrep-sort', sortSel.value); } catch (e) { /* storage unavailable */ }
  });
  document.getElementById('site-reset').addEventListener('click', function () {
    search.value = '';
    setFilter('all');
  });
  document.addEventListener('keydown', function (e) {
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (e.key === '/' && !typing) { e.preventDefault(); search.focus(); }
    if (e.key === 'Escape' && document.activeElement === search) { search.value = ''; applyFilter(); search.blur(); }
  });
  try {
    var saved = localStorage.getItem('statrep-sort');
    if (saved && sortSel.querySelector('option[value="' + saved + '"]')) sortSel.value = saved;
  } catch (e) { /* storage unavailable */ }
  updateCounts();
  applySort();

  // ---------- Status map: jump to a row ----------
  var tiles = document.getElementById('tiles');
  if (tiles) {
    tiles.addEventListener('click', function (e) {
      var tile = e.target.closest('[data-jump]');
      if (!tile) return;
      e.preventDefault();
      var row = byKey(document.querySelector('main'), tile.dataset.jump);
      if (!row) return;
      if (row.hidden) { search.value = ''; setFilter('all'); }
      if (row.tagName === 'DETAILS') row.open = true;
      row.scrollIntoView({behavior: 'smooth', block: 'center'});
      row.classList.remove('is-flash');
      void row.offsetWidth;  // restart the animation
      row.classList.add('is-flash');
    });
  }

  // ---------- Check one website again (not emailed) ----------
  var siteErr = document.getElementById('site-error');
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-site-check]');
    if (!btn) return;
    e.preventDefault();
    var row = btn.closest('.site');
    var label = btn.querySelector('span').textContent;
    siteErr.hidden = true;
    setBusy(btn, true, '#i-refresh', label);
    postJSON(document.body.dataset.siteCheckUrl, {url: btn.dataset.siteCheck})
      .then(function (d) {
        var tpl = document.createElement('template');
        tpl.innerHTML = d.html.trim();
        var fresh = tpl.content.firstElementChild;
        fresh.dataset.order = row.dataset.order;
        row.replaceWith(fresh);
        fresh.classList.add('is-flash');
        var status = fresh.dataset.status;
        var tile = byKey(tiles, fresh.dataset.key);
        if (tile) {
          tile.className = 'tile tile-' + status.toLowerCase() + (fresh.dataset.expiring ? ' tile-warn' : '');
          tile.title = fresh.querySelector('.site-name').textContent + ': ' + LABELS[status] +
            (fresh.dataset.reason ? ' · ' + fresh.dataset.reason : '') + (fresh.dataset.ms ? ' · ' + fresh.dataset.ms + ' ms' : '');
        }
        updateCounts();
        var name = fresh.querySelector('.site-name').textContent;
        var detail = fresh.dataset.reason || (fresh.dataset.ms ? fresh.dataset.ms + ' ms' : '');
        toast(name + ': ' + LABELS[status] + (detail ? ' (' + detail + ')' : '') + '. Not emailed.',
              status.toLowerCase());
      })
      .catch(function (err) {
        showError(siteErr, err.message);
        setBusy(btn, false, '#i-refresh', label);
      });
  });

  // ---------- Quick check ----------
  var form = document.getElementById('quick-form');
  var input = document.getElementById('quick-urls');
  var errBox = document.getElementById('quick-error');
  var out = document.getElementById('quick-results');
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) form.requestSubmit();
  });
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var btn = form.querySelector('button');
    errBox.hidden = true;
    setBusy(btn, true);
    postJSON(document.body.dataset.checkUrl, {urls: input.value.split(/[\s,]+/).filter(Boolean)})
      .then(function (d) {
        out.innerHTML = d.html;
        out.hidden = false;
      })
      .catch(function (err) { showError(errBox, err.message); })
      .then(function () { setBusy(btn, false, '#i-search', 'Check'); });
  });

  if (document.body.dataset.running === 'true') {
    (function poll() {
      fetch(document.body.dataset.statusUrl, {cache: 'no-store'})
        .then(function (r) { return r.json(); })
        .then(function (d) { d.running ? setTimeout(poll, 2000) : location.reload(); })
        .catch(function () { setTimeout(poll, 4000); });
    })();
  }
})();
