# StatRep – Website & API Status Reporter

StatRep checks your websites and APIs several times a day and emails a status report to
everyone on your list. A web page shows the latest results and lets you run a check on demand.

- **When:** every day at **06:00**, **07:45**, **16:30** and **20:00** (Asia/Colombo), plus whenever
  you click **Run check & send report**.
- **What:** websites from `config/sites.yml` and APIs from `config/apis.yml`.
- **Result per target:** **UP**, **SLOW** or **DOWN**, with response time, HTTP code, SSL and domain
  expiry, and the reason when something fails.

## Quick start

```bash
cp .env.example .env          # then fill in the SMTP details and MAIL_TO
chmod 600 .env                # keep the passwords private
docker compose up -d --build
```

Open `http://<server-ip>:9090`. The container restarts automatically after reboots.

---

## How it works

```
 Schedule (06:00, 07:45, 16:30, 20:00)     "Run check & send report" button
                 └──────────────┬──────────────────┘
                         service.run_check            (one check at a time; a scheduled run
                                │                      waits for a running one to finish)
          websites (sites.yml) + APIs (apis.yml)
                                │
   Phase 1 – measure   timed request for every target, 10 at a time (TIMING_CONCURRENCY)
                       → decides UP / SLOW / DOWN and the response time
   Phase 2 – details   SSL, domain, page analysis, HTTPS… for all targets, 40 at a time
                       (CHECK_CONCURRENCY); never changes the status
                                │
                         results (kept in memory)
               ┌────────────────┼─────────────────┐
           web page        email to every       CSV / HTML
                           MAIL_TO address       download
```

### Why two phases (scaling to 40+ sites)

Checking many sites at exactly the same time makes them slow each other down: downloads share
the network and page parsing shares the CPU. In a real test with 40 sites checked all at once,
github.com measured 5.4 s instead of 0.3 s, giving false **SLOW** and even false **DOWN** results.
So the timed requests run 10 at a time with nothing else going on (accurate times), and the
untimed detail work runs 40 at a time (fast). Measured on 40 real websites: about 30 s for a full
run including one unreachable site, ~155 MB of memory, and response times identical to checking
the sites a few at a time. For more sites, raise `CHECK_CONCURRENCY`; keep `TIMING_CONCURRENCY`
around 10.

### Checking a website

1. **Load the page** (phase 1). A `GET` request follows up to 10 redirects, waits at most 10 s to
   connect and 20 s for data (`CHECK_TIMEOUT`), and reads at most 2 MB. **This step decides the
   status:**

   | What happened | Status |
   |---|---|
   | Invalid SSL certificate | **DOWN** |
   | No answer within the timeout | **DOWN** |
   | DNS failure or connection refused | **DOWN** |
   | HTTP status 400 or higher (404, 500, 503…) | **DOWN** |
   | Page is (almost) empty, under 50 characters | **DOWN** |
   | Slower than 5000 ms (`SLOW_MS`) | **SLOW** |
   | Anything else | **UP** |

   A DOWN result is retried once after 3 s (`CHECK_RETRIES`), so a single network glitch
   doesn't raise an alarm.

2. **Collect details** (phase 2). These never change the status, they add information.
   Sites that couldn't be reached at all skip the connection probe and HTTPS check.
   - **Connection:** DNS, connect and TLS time (an estimate: measured on a separate connection);
     SSL issuer, expiry date and TLS version.
   - **Domain:** expiry date and registrar from the public RDAP registry (cached 12 hours; failed
     lookups are retried after 15 minutes; at most 4 lookups at once so rdap.org doesn't block us).
     Shared hosts such as `github.io` are shown as "Shared".
   - **Page:** title, description, favicon, and when the site was last updated
     (from the `Last-Modified` header, page metadata or `sitemap.xml`).
   - **Security headers:** six recommended headers, graded A to F.
   - **HTTPS:** whether `http://` redirects to `https://`.

### Checking an API

1. **Read the config.** `${NAME}` placeholders are filled in from `.env`. A mistake in the entry
   (bad URL, unknown option, missing secret) marks the API **DOWN** with a "Config error" and it is
   not called.
2. **Call the API** with its method, headers and body (a mapping or list is sent as JSON).
3. **Compare the reply with your expectations.** All of them must pass:

   | Option | Passes when |
   |---|---|
   | `expect_status: 200` | The status is exactly that (or one of a list). Without it: any status below 400. |
   | `expect_json: {status: ok}` | The JSON reply has that field with that value. Dotted paths reach nested fields: `data.items.0.id`. A number or true/false matches its text form (`200` = `"200"`). |
   | `expect_text: "token"` | The reply contains that text. |

4. **Decide the status:** any failed expectation, timeout or connection error → **DOWN** (the first
   failure is shown, e.g. *status: expected 'ok', got 'degraded'*). All passed but slower than
   `slow_ms` (default 2000 ms) → **SLOW**. Otherwise **UP**.
5. **Retry:** GET/HEAD/OPTIONS are retried once. POST/PUT/PATCH/DELETE are not (a retry could create
   data twice) unless you set `retry: true`.
6. **Collect details:** the same connection, SSL and domain information as for websites.

### Expiry warnings

Independently of UP/SLOW/DOWN, a target is counted as **Expiring soon** when its SSL certificate
has fewer than 14 days left (`SSL_WARN_DAYS`) or its domain fewer than 30 (`DOMAIN_WARN_DAYS`).

### The report

- **Email:** a summary ("All 12 services operational" / "2 of 12 services down") and a table for
  websites and one for APIs, sorted DOWN → SLOW → UP. Each address in `MAIL_TO` gets its own copy,
  so recipients don't see each other. If one address is refused, the others still get the report.
- **Web page:** the same results; click any row for full details (timing breakdown, redirects,
  SSL, domain, security headers or API checks).
- **Downloads:** CSV (every field, for Excel/Sheets) and a standalone HTML report.

### Quick check

The **Quick check** box on the page checks any URLs once, as websites. Results are not saved or
emailed. For safety it refuses private or internal addresses (localhost, 10.x, 192.168.x,
cloud metadata…), including through redirects, and is limited to 6 requests per minute per visitor.

---

## Configuration

### Websites – `config/sites.yml`

```yaml
- https://vertexlabs.team/
- url: openats.dev          # https:// is added if missing
  name: OpenATS             # optional display name
```

### APIs – `config/apis.yml`

```yaml
- name: Orders API health
  url: https://api.example.com/health
  headers:
    Authorization: "Bearer ${ORDERS_API_TOKEN}"   # value comes from .env
  expect_status: 200
  expect_json:
    status: ok
  slow_ms: 1000
```

All options (`method`, `body`, `expect_text`, `timeout`, `retry`, `follow_redirects`) are explained
at the top of the file. **Never write tokens or passwords in this file**: put them in `.env` and refer
to them as `${NAME}`. Header values and bodies never appear in the page, email or CSV.

### Settings – `.env`

| Setting | Default | Meaning |
|---|---|---|
| `TZ` | `Asia/Colombo` | Time zone for the schedule and report times |
| `CHECK_TIMES` | `06:00,07:45,16:30,20:00` | Daily check times, `HH:MM`, comma-separated |
| `SMTP_HOST` / `SMTP_PORT` | Brevo / `587` | Mail server (587 = STARTTLS, 465 = SSL) |
| `SMTP_USER` / `SMTP_PASS` | – | Mail server login |
| `MAIL_FROM` | `SMTP_USER` | Sender address (must be verified with your provider) |
| `MAIL_TO` | – | Recipients, comma-separated, any number |
| `CHECK_TIMEOUT` | `20` | Seconds to wait for a response |
| `CHECK_RETRIES` | `1` | Extra attempts before DOWN |
| `SLOW_MS` | `5000` | Websites slower than this are SLOW |
| `API_SLOW_MS` | `2000` | Default SLOW limit for APIs |
| `SSL_WARN_DAYS` / `DOMAIN_WARN_DAYS` | `14` / `30` | "Expiring soon" thresholds |
| `TIMING_CONCURRENCY` | `10` | Timed requests at once (phase 1); keep low for accurate times |
| `CHECK_CONCURRENCY` | `40` | Targets processed at once in phase 2 |
| `PORT` | `9090` | Port of the web page on the server |
| `BIND` | `0.0.0.0` | `127.0.0.1` = reachable only through a local reverse proxy |
| `RUN_COOLDOWN` | `120` | Seconds between manual runs |
| `QUICK_CHECKS_PER_MIN` | `6` | Quick checks per visitor per minute |
| `AUTH_USER` / `AUTH_PASSWORD` | – | Optional login for the page (set both); empty = no login |
| `TRUSTED_PROXIES` | `0` | Number of reverse proxies in front, so rate limits see real visitor IPs |
| `DASHBOARD_URL` | – | Adds an "Open dashboard" button to emails |

Any other variable (e.g. `ORDERS_API_TOKEN`) can be used in `apis.yml` as `${NAME}`.
Invalid values (e.g. `CHECK_TIMES=7.45`) stop the app with a one-line message naming the setting;
see `docker compose logs`.

## Common tasks

| Task | Command |
|------|---------|
| Add/remove a website | edit `config/sites.yml`, then `docker compose restart` |
| Add/remove an API | edit `config/apis.yml`, then `docker compose restart` |
| Change recipients, times or secrets | edit `.env`, then `docker compose up -d` |
| Update after code changes | `docker compose up -d --build` |
| View logs | `docker compose logs -f` |
| Run the tests | `pip install -r requirements.txt -r requirements-dev.txt && python -m pytest` |
| Stop | `docker compose down` |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "Email not sent: SMTP settings are missing" | Fill in `SMTP_USER`, `SMTP_PASS` and `MAIL_TO` in `.env`, then `docker compose up -d` |
| "Email failed: SMTP login was rejected" | Wrong `SMTP_USER` / `SMTP_PASS` |
| "Email failed: the sender address was refused" | `MAIL_FROM` is not a verified sender with your provider |
| "Email not sent: MAIL_FROM … is not a valid email address" | Fix `MAIL_FROM` in `.env` |
| Container keeps restarting | A setting in `.env` is invalid; `docker compose logs` shows "StatRep configuration error: …" |
| A row says "Config error: …" | Fix that entry in `sites.yml` / `apis.yml`; the message says what is wrong |
| "Config error: missing in .env: NAME" | Add `NAME=...` to `.env`, then `docker compose up -d` |
| "Please wait N seconds before running another" | Manual runs are limited by `RUN_COOLDOWN` |
| 403 Forbidden when clicking a button | Behind a reverse proxy the original `Host` header must be forwarded (nginx: `proxy_set_header Host $host;`) |
| Page is empty after a restart | Results are kept in memory only; run a check or wait for the next scheduled one |

## Security notes

- By default the page has **no login**: anyone who can reach it can view results and start checks.
  Set `AUTH_USER` and `AUTH_PASSWORD` to require a username and password (the browser asks once),
  or keep it on a private network/VPN.
- Websites in `sites.yml` may be internal; Quick check can only reach public addresses. The address
  is checked and connected to in one step, so DNS tricks can't swap in an internal one.
- SMTP connections verify the server certificate. Keep `.env` private (`chmod 600 .env`).
- The page allows no inline scripts (strict Content-Security-Policy).
- The container runs as a non-root user with a read-only filesystem, no extra privileges, and memory
  and process limits. The base image and all packages are pinned; CI runs the tests and
  `pip-audit`, and Dependabot proposes updates weekly.
- See [`AUDIT.md`](AUDIT.md) for the full code audit and open recommendations.

## Project structure

```
statrep/
├── app/
│   ├── main.py            # web page, routes, scheduler, security headers, rate limits
│   ├── service.py         # runs a full check and sends the email
│   ├── checker.py         # website and API checks
│   ├── mailer.py          # builds and sends the email (one copy per recipient)
│   ├── config.py          # settings from .env, loads sites.yml and apis.yml
│   ├── filters.py         # date/time formatting for page and email
│   ├── templates/         # index.html (page), _site.html (rows), email.html (report)
│   └── static/            # style.css, app.js, logos, font
├── config/
│   ├── sites.yml          # websites to check
│   └── apis.yml           # APIs to check, with expected responses
├── tests/                 # pytest suite (python -m pytest)
├── .github/               # CI workflow (tests, pip-audit, Docker build) and Dependabot
├── .env                   # credentials + settings (not committed)
├── .env.example
├── AUDIT.md               # code audit
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt   # pytest, pip-audit
```
