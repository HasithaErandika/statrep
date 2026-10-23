"""Flask web UI + twice-daily scheduler."""
import csv
import hmac
import io
import logging
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from . import checker, config, filters, mailer, service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

scheduler = BackgroundScheduler(timezone=config.TZ)
STATUS_ORDER = {"DOWN": 0, "SLOW": 1}
MAX_ONE_TIME = 10

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' http: https: data:; "
                                "script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # Not "no-referrer": browsers then send "Origin: null" on form posts, failing the origin check
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# Quick checks make the server fetch arbitrary sites: limit per client and in total
_quick_slots = threading.BoundedSemaphore(2)
_quick_hits = defaultdict(deque)
_quick_lock = threading.Lock()


def _quick_allowed(ip):
    now = time.monotonic()
    with _quick_lock:
        for key in [k for k, hits in _quick_hits.items() if not hits or now - hits[-1] > 60]:
            del _quick_hits[key]
        hits = _quick_hits[ip]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= config.QUICK_CHECKS_PER_MIN:
            return False
        hits.append(now)
        return True


def create_app():
    app = Flask(__name__)
    if config.TRUSTED_PROXIES:
        # Use the real client IP from X-Forwarded-For, so rate limits are per visitor, not per proxy
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=config.TRUSTED_PROXIES, x_proto=config.TRUSTED_PROXIES,
                                x_host=config.TRUSTED_PROXIES)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600  # cache static files for an hour
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024    # request bodies are tiny (Quick check URLs)
    filters.register(app.jinja_env)

    for t in config.CHECK_TIMES:
        hour, minute = t.split(":")
        scheduler.add_job(
            service.run_check, CronTrigger(hour=int(hour), minute=int(minute)),
            kwargs={"trigger": "scheduled"}, id=f"check-{t}", replace_existing=True,
            misfire_grace_time=300, coalesce=True,  # still run if the host was busy or asleep at that minute
        )
    if not scheduler.running:
        scheduler.start()

    @app.before_request
    def require_login():
        """Optional HTTP Basic auth (AUTH_USER / AUTH_PASSWORD); /health stays open for Docker."""
        if not config.AUTH_USER or request.path == "/health":
            return None
        auth = request.authorization
        if (auth and auth.type == "basic"
                and hmac.compare_digest((auth.username or "").encode(), config.AUTH_USER.encode())
                and hmac.compare_digest((auth.password or "").encode(), config.AUTH_PASSWORD.encode())):
            return None
        return Response("Login required.", 401, {"WWW-Authenticate": 'Basic realm="StatRep", charset="UTF-8"'})

    @app.before_request
    def same_origin_posts():
        """Block cross-site form/fetch POSTs (e.g. another site making visitors trigger emails)."""
        if request.method == "POST":
            origin = request.headers.get("Origin") or request.headers.get("Referer")
            if origin and urlparse(origin).netloc != request.host:
                abort(403)

    @app.after_request
    def security_headers(response):
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.path.startswith("/api/") or request.path.startswith("/report."):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        results, apis = service.latest["results"], service.latest["apis"]
        everything = results + apis
        wait = request.args.get("wait", type=int)
        timed = [r.ms for r in everything if r.ms]
        next_runs = [j.next_run_time for j in scheduler.get_jobs() if j.next_run_time]
        return render_template(
            "index.html",
            run=service.latest["run"],
            note=service.latest["note"],
            running=service.latest["running"],
            results=sorted(results, key=lambda r: STATUS_ORDER.get(r.status, 2)),
            apis=sorted(apis, key=lambda r: STATUS_ORDER.get(r.status, 2)),
            counts={**{s: sum(r.status == s for r in everything) for s in ("UP", "SLOW", "DOWN")},
                    "EXPIRING": sum(r.expiring for r in everything)},
            avg_ms=int(sum(timed) / len(timed)) if timed else 0,
            sites=config.load_sites(),
            api_list=config.load_apis(),
            next_run=min(next_runs).strftime("%a %d %b, %H:%M") if next_runs else "-",
            wait=wait if wait and wait > 0 else None,
            cfg=config,
        )

    @app.post("/run")
    def run_now():
        wait = service.start_check("manual")
        return redirect(url_for("index", wait=wait) if wait else url_for("index"))

    @app.get("/report.<fmt>")
    def download_report(fmt):
        run, results, apis = service.latest["run"], service.latest["results"], service.latest["apis"]
        if not run or fmt not in ("csv", "html"):
            abort(404)
        filename = f"statrep-report-{run['started_at'].replace(' ', '-').replace(':', '')}.{fmt}"
        if fmt == "html":
            body = mailer.render_standalone(run, results, apis)
            mimetype = "text/html"
        else:
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow([
                "Type", "Name", "URL", "Status", "HTTP", "Response (ms)", "DNS (ms)", "Connect (ms)",
                "TLS (ms)", "Server (ms)", "Download (ms)", "Final URL", "Redirects", "HTTPS enforced",
                "SSL issuer", "SSL expires", "SSL days left", "TLS version", "Domain", "Registrar",
                "Domain expires", "Domain days left", "Security grade", "Missing security headers",
                "Last updated", "Updated source", "Title", "Description", "Method", "Checks passed",
                "Error", "Checked at"])
            by_status = lambda r: STATUS_ORDER.get(r.status, 2)  # noqa: E731
            for r in sorted(results, key=by_status) + sorted(apis, key=by_status):
                t = r.timing
                passed = f"{sum(ok for _, ok, _ in r.checks)}/{len(r.checks)}" if r.checks else ""
                writer.writerow([
                    "API" if r.kind == "api" else "Website", r.name, r.url, r.status, r.code or "", r.ms or "", t.get("dns", ""),
                    t.get("connect", ""), t.get("tls", ""), t.get("server", ""), t.get("download", ""),
                    r.final_url or "", " > ".join(f"{h['code']} {h['url']}" for h in r.redirects),
                    {True: "Yes", False: "No"}.get(r.https_enforced, ""),
                    r.ssl_issuer or "", filters.fmtdate(r.ssl_expires), "" if r.ssl_days is None else r.ssl_days,
                    r.tls_version or "", r.domain or "", r.domain_registrar or "",
                    filters.fmtdate(r.domain_expires) or r.domain_note or "",
                    "" if r.domain_days is None else r.domain_days, r.sec_grade or "",
                    ", ".join(n for n, _, ok in r.sec_headers if not ok),
                    filters.fmtdate(r.updated), r.updated_source or "", r.title or "",
                    r.description or "", r.method or "", passed, r.error or "", run["started_at"]])
            body = "﻿" + out.getvalue()  # BOM so Excel reads UTF-8 correctly
            mimetype = "text/csv"
        return Response(body, mimetype=mimetype,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/api/check")
    def check_once():
        """One-time check of ad-hoc URLs: not saved, not emailed."""
        if not _quick_allowed(request.remote_addr):
            return jsonify(error="Too many checks. Please wait a minute and try again."), 429
        raw = (request.get_json(silent=True) or {}).get("urls", [])
        urls = []
        for u in raw if isinstance(raw, list) else []:
            u = str(u).strip()[:2048]
            if not u:
                continue
            if "://" not in u:
                u = "https://" + u
            parsed = urlparse(u)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return jsonify(error=f"Not a valid website address: {u}"), 400
            if u not in urls:
                urls.append(u)
        if not urls:
            return jsonify(error="Enter at least one website address."), 400
        if len(urls) > MAX_ONE_TIME:
            return jsonify(error=f"Check up to {MAX_ONE_TIME} websites at a time."), 400

        if not _quick_slots.acquire(timeout=30):
            return jsonify(error="The server is busy with other checks. Please try again shortly."), 503
        try:
            results = checker.check_all(urls, public_only=True)
        finally:
            _quick_slots.release()
        results = sorted(results, key=lambda r: STATUS_ORDER.get(r.status, 2))
        return jsonify(html=render_template("_rows.html", results=results, cfg=config))

    @app.get("/api/status")
    def api_status():
        return jsonify({"running": service.latest["running"], "run": service.latest["run"],
                        "results": [vars(r) for r in service.latest["results"]],
                        "apis": [vars(r) for r in service.latest["apis"]]})

    @app.get("/health")
    def health():
        return {"ok": True}

    return app
