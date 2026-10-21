"""Flask web UI + twice-daily scheduler."""
import csv
import io
import logging
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

from . import checker, config, filters, mailer, service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

scheduler = BackgroundScheduler(timezone=config.TZ)
STATUS_ORDER = {"DOWN": 0, "SLOW": 1}
MAX_ONE_TIME = 10


def create_app():
    app = Flask(__name__)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600  # cache static files for an hour
    filters.register(app.jinja_env)

    for t in config.CHECK_TIMES:
        hour, minute = t.split(":")
        scheduler.add_job(
            service.run_check, CronTrigger(hour=int(hour), minute=int(minute)),
            kwargs={"trigger": "scheduled"}, id=f"check-{t}", replace_existing=True,
        )
    if not scheduler.running:
        scheduler.start()

    @app.get("/")
    def index():
        results = service.latest["results"]
        timed = [r.ms for r in results if r.ms]
        next_runs = [j.next_run_time for j in scheduler.get_jobs() if j.next_run_time]
        return render_template(
            "index.html",
            run=service.latest["run"],
            note=service.latest["note"],
            running=service.latest["running"],
            results=sorted(results, key=lambda r: STATUS_ORDER.get(r.status, 2)),
            counts={**{s: sum(r.status == s for r in results) for s in ("UP", "SLOW", "DOWN")},
                    "EXPIRING": sum(r.expiring for r in results)},
            avg_ms=int(sum(timed) / len(timed)) if timed else 0,
            sites=config.load_sites(),
            next_run=min(next_runs).strftime("%a %d %b, %H:%M") if next_runs else "-",
            cfg=config,
        )

    @app.post("/run")
    def run_now():
        service.start_check("manual")
        return redirect(url_for("index"))

    @app.get("/report.<fmt>")
    def download_report(fmt):
        run, results = service.latest["run"], service.latest["results"]
        if not run or fmt not in ("csv", "html"):
            abort(404)
        filename = f"statrep-report-{run['started_at'].replace(' ', '-').replace(':', '')}.{fmt}"
        if fmt == "html":
            body = mailer.render_standalone(run, results)
            mimetype = "text/html"
        else:
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow([
                "Website", "URL", "Status", "HTTP", "Response (ms)", "DNS (ms)", "Connect (ms)",
                "TLS (ms)", "Server (ms)", "Download (ms)", "Final URL", "Redirects", "HTTPS enforced",
                "SSL issuer", "SSL expires", "SSL days left", "TLS version", "Domain", "Registrar",
                "Domain expires", "Domain days left", "Security grade", "Missing security headers",
                "Last updated", "Updated source", "Title", "Description", "Error", "Checked at"])
            for r in sorted(results, key=lambda r: STATUS_ORDER.get(r.status, 2)):
                t = r.timing
                writer.writerow([
                    r.name, r.url, r.status, r.code or "", r.ms or "", t.get("dns", ""),
                    t.get("connect", ""), t.get("tls", ""), t.get("server", ""), t.get("download", ""),
                    r.final_url or "", " > ".join(f"{h['code']} {h['url']}" for h in r.redirects),
                    {True: "Yes", False: "No"}.get(r.https_enforced, ""),
                    r.ssl_issuer or "", filters.fmtdate(r.ssl_expires), "" if r.ssl_days is None else r.ssl_days,
                    r.tls_version or "", r.domain or "", r.domain_registrar or "",
                    filters.fmtdate(r.domain_expires) or r.domain_note or "",
                    "" if r.domain_days is None else r.domain_days, r.sec_grade or "",
                    ", ".join(n for n, _, ok in r.sec_headers if not ok),
                    filters.fmtdate(r.updated), r.updated_source or "", r.title or "",
                    r.description or "", r.error or "", run["started_at"]])
            body = "﻿" + out.getvalue()  # BOM so Excel reads UTF-8 correctly
            mimetype = "text/csv"
        return Response(body, mimetype=mimetype,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/api/check")
    def check_once():
        """One-time check of ad-hoc URLs: not saved, not emailed."""
        raw = (request.get_json(silent=True) or {}).get("urls", [])
        urls = []
        for u in raw if isinstance(raw, list) else []:
            u = str(u).strip()
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

        results = sorted(checker.check_all(urls), key=lambda r: STATUS_ORDER.get(r.status, 2))
        return jsonify(html=render_template("_rows.html", results=results, cfg=config))

    @app.get("/api/status")
    def api_status():
        return jsonify({"running": service.latest["running"], "run": service.latest["run"],
                        "results": [vars(r) for r in service.latest["results"]]})

    @app.get("/health")
    def health():
        return {"ok": True}

    return app
