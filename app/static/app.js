// StatRep page behaviour. Kept out of the HTML so the CSP can forbid inline scripts.
(function () {
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  function setBusy(btn, busy, idleIcon, idleText) {
    btn.disabled = busy;
    btn.classList.toggle('is-loading', busy);
    btn.querySelector('use').setAttribute('href', busy ? '#i-loader' : idleIcon);
    btn.querySelector('span').textContent = busy ? 'Checking…' : idleText;
  }

  document.getElementById('run-form').addEventListener('submit', function () {
    setBusy(this.querySelector('button'), true);
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
    fetch(document.body.dataset.checkUrl, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({urls: input.value.split(/[\s,]+/).filter(Boolean)})
    })
      .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.d.error || 'Check failed.');
        out.innerHTML = res.d.html;
        out.hidden = false;
      })
      .catch(function (err) {
        errBox.innerHTML = '<svg class="i" aria-hidden="true"><use href="#i-x"/></svg>' + esc(err.message);
        errBox.hidden = false;
      })
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
