# StatRep – Setup Guide

How to install StatRep on **Windows** or **Linux**, configure it, and restart it after changes.
StatRep runs in Docker, so the commands are the same on both systems once Docker is installed.

1. [Install Docker](#1-install-docker-once)
2. [Get StatRep](#2-get-statrep)
3. [Configure](#3-configure)
4. [Start and check it works](#4-start-and-check-that-it-works)
5. [After you change something: restart or rebuild](#5-after-you-change-something)
6. [Using the page](#6-using-the-page) · [All settings](#7-all-settings-env) · [Server extras](#8-extra-steps-on-a-server) · [Troubleshooting](#9-troubleshooting) · [Without Docker](#10-without-docker-development) · [Uninstall](#11-uninstall)

> StatRep only sends the scheduled reports while the machine is on and Docker is running.
> A server that is always on is best; a PC is fine for trying it out.

---

## 1. Install Docker (once)

### Windows 10 / 11
1. Download **Docker Desktop** from <https://www.docker.com/products/docker-desktop/> and run the installer.
   Keep **"Use WSL 2"** ticked. If it asks to install or update WSL, open **PowerShell as
   Administrator**, run `wsl --install`, and restart the PC.
2. Start **Docker Desktop** and wait until it shows **Engine running**.
3. In Docker Desktop, open **Settings → General** and tick **Start Docker Desktop when you sign in**,
   so StatRep comes back after a restart.
4. Check it in **PowerShell**:
   ```powershell
   docker --version
   docker compose version
   ```
5. Install **Git for Windows** from <https://git-scm.com/download/win> (or skip it and use a
   [code zip](#c-from-a-code-zip)).

Use **PowerShell** for all commands in this guide. Also make sure the PC doesn't go to sleep
(**Settings → System → Power**), or the scheduled checks won't run.

### Linux (Ubuntu, Debian, etc.)
```bash
curl -fsSL https://get.docker.com | sudo sh     # installs Docker and the compose plugin
sudo systemctl enable --now docker              # start Docker now and on every boot
sudo usermod -aG docker $USER                   # run docker without sudo (log out and in again)
docker --version && docker compose version
sudo apt install -y git                         # if git is missing
```

### Mac
Install Docker Desktop (as on Windows) and use **Terminal**; the Linux commands below work as-is.

---

## 2. Get StatRep

Pick **one** of these:

### A. Clone from GitHub (recommended: easiest to update)
```bash
git clone https://github.com/HasithaErandika/statrep.git
cd statrep
```

### B. From a shared image file (machine without internet or build tools)
You received `statrep-image.tar.gz` (made on another machine with
`docker save statrep-statrep:latest | gzip > statrep-image.tar.gz`).
```bash
docker load -i statrep-image.tar.gz
mkdir statrep
cd statrep
mkdir config
```
Put `.env.example` and `config/sites.yml` (and `config/apis.yml` if you check APIs) in that folder,
then create `docker-compose.yml` there with:
```yaml
services:
  statrep:
    image: statrep-statrep:latest
    container_name: statrep
    env_file: .env
    ports:
      - "${BIND:-0.0.0.0}:${PORT:-9090}:8080"
    volumes:
      - ./config:/app/config:ro
    restart: unless-stopped
    read_only: true
    tmpfs: [/tmp]
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
    mem_limit: 512m
    pids_limit: 200
```
With this way, use `docker compose up -d` (**without** `--build`) everywhere below.

### C. From a code zip
On GitHub: **Code → Download ZIP**, or someone sends you `statrep.zip`. Extract it, open a
terminal in the extracted folder (Windows: in File Explorer, type `powershell` in the address bar),
and continue like way A.

---

## 3. Configure

### Settings and passwords: `.env`

| Windows (PowerShell) | Linux |
|---|---|
| `Copy-Item .env.example .env` | `cp .env.example .env` |
| `notepad .env` | `nano .env` |
| | `chmod 600 .env` (only you can read it) |

> **Windows / Notepad:** when saving, keep the name `.env` and choose **Save as type: All files**,
> or Notepad saves it as `.env.txt` and StatRep won't find it.

The minimum to fill in:

| Setting | Example |
|---|---|
| `SMTP_HOST`, `SMTP_PORT` | `smtp-relay.brevo.com`, `587` |
| `SMTP_USER`, `SMTP_PASS` | your mail provider's SMTP login. Leave both empty for a company relay that allows this machine by IP; add `SMTP_TLS=off` if that relay has no TLS |
| `MAIL_FROM` | a sender address your mail provider accepts |
| `MAIL_TO` | `ops@example.com,dev@example.com` (any number, comma-separated) |
| `CHECK_TIMES` | `06:00,07:45,16:30,20:00` |

All other settings are optional; see [section 7](#7-all-settings-env).
**Never email or commit `.env`**: it holds the SMTP password.

### Websites: `config/sites.yml`
```yaml
- https://example.com/
- url: portal.example.com        # https:// is added if missing
  name: Customer portal          # optional display name
```

### APIs (optional): `config/apis.yml`
All options and examples are at the top of the file. Put tokens in `.env` (e.g.
`ORDERS_API_TOKEN=...`) and use them in the file as `${ORDERS_API_TOKEN}`; they are never shown
on the page or in the email.

---

## 4. Start and check that it works

```bash
docker compose up -d --build
```
This builds StatRep, starts it in the background and restarts it automatically after a reboot
(as long as Docker starts). The first build takes a minute or two.

1. `docker compose logs` should end with **`Scheduler started`**.
   If it says `StatRep configuration error: …`, fix that setting in `.env` and see [section 5](#5-after-you-change-something).
2. Open **<http://localhost:9090>** on the same machine, or **http://SERVER-IP:9090** from another one.
   The header shows the next scheduled check.
3. Click **Check all & email**. After about half a minute the results appear and everyone in
   `MAIL_TO` gets the report. If not, the page says why ([troubleshooting](#9-troubleshooting)).

---

## 5. After you change something

The same commands work in PowerShell (Windows) and the Linux terminal. Run them in the `statrep` folder.

| You changed… | Run | Why |
|---|---|---|
| `config/sites.yml` or `config/apis.yml` | `docker compose restart` | The files are read when StatRep starts |
| `.env` (recipients, times, passwords…) | `docker compose up -d` | `.env` is only read when the container is created; `restart` is **not** enough |
| The code: after `git pull`, a new zip, or your own edits | `docker compose up -d --build` | Rebuilds the image with the new code |
| A new image file (way B) | `docker load -i statrep-image.tar.gz` then `docker compose up -d` | Uses the new image |
| Not sure / something seems stuck | `docker compose down` then `docker compose up -d --build` | Starts completely fresh |

**Updating to the latest version (way A):**
```bash
git pull
docker compose up -d --build
```

Results are kept in memory only, so after any restart the page is empty until the next check:
click **Check all** (or **Check all & email**) to fill it straight away.

Other useful commands:

| Task | Command |
|---|---|
| Is it running? | `docker compose ps` |
| Watch the logs (stop watching with `Ctrl+C`) | `docker compose logs -f` |
| Stop it | `docker compose down` |
| Start it again | `docker compose up -d` |

---

## 6. Using the page

| Button | What it does |
|---|---|
| **Check all** | Checks every website and API now; nothing is emailed |
| **Check all & email** | Checks everything and emails the report to everyone in `MAIL_TO` |
| **Email these results** | Emails the results shown now (including single re-checks) without checking again |
| ↻ / **Check this website again** | Checks one website only and updates its row; nothing is emailed |
| **Download** | CSV (for Excel) or a printable HTML report of the last results |
| **Quick check** | Checks any public addresses once; not saved or emailed |

With many sites, use the **status map** (one square per site, click to jump), the **search** box
(press `/`), the **filters** (Down, Slow, Operational, Expiring) and **sort**.

Every Down result shows **why**: by HTTP code (*Forbidden 403*, *Login required 401*, *Not found 404*,
*Server error 500*, *Bad gateway 502*, *Service unavailable 503*…) or without a response (*Timeout*,
*Unreachable*, *No connection*, *SSL error*). Hover over the reason for what it usually means.
*Forbidden* often means the site is up but a firewall blocks automated checks.

Manual runs and emails are limited to one per `RUN_COOLDOWN` seconds (default 120); the same
website can be re-checked every 20 seconds.

---

## 7. All settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `TZ` | `Asia/Colombo` | Time zone for the schedule and report times |
| `CHECK_TIMES` | `06:00,07:45,16:30,20:00` | Daily check times, `HH:MM`, comma-separated |
| `SMTP_HOST` / `SMTP_PORT` | Brevo / `587` | Mail server (587 = STARTTLS, 465 = SSL) |
| `SMTP_USER` / `SMTP_PASS` | – | Mail server login; leave both empty for a relay that allows this machine by IP |
| `SMTP_TLS` | `on` | Set `off` only for an internal relay without TLS |
| `MAIL_FROM` | `SMTP_USER` | Sender address |
| `MAIL_TO` | – | Recipients, comma-separated; each gets their own copy |
| `CHECK_TIMEOUT` | `20` | Seconds to wait for a response |
| `CHECK_RETRIES` | `1` | Extra attempts before Down |
| `SLOW_MS` | `5000` | Websites slower than this (ms) are Slow |
| `API_SLOW_MS` | `2000` | Default Slow limit for APIs |
| `SSL_WARN_DAYS` / `DOMAIN_WARN_DAYS` | `14` / `30` | "Expiring soon" thresholds |
| `TIMING_CONCURRENCY` | `10` | Timed requests at once; keep low for accurate response times |
| `CHECK_CONCURRENCY` | `40` | Sites processed at once for SSL/domain/page details |
| `PORT` | `9090` | Port of the web page |
| `BIND` | `0.0.0.0` | `127.0.0.1` = reachable only through a local reverse proxy |
| `RUN_COOLDOWN` | `120` | Seconds between manual runs / emails |
| `QUICK_CHECKS_PER_MIN` | `6` | Quick checks per visitor per minute |
| `AUTH_USER` / `AUTH_PASSWORD` | – | Optional login for the page (set both) |
| `TRUSTED_PROXIES` | `0` | Number of reverse proxies in front (for real visitor IPs) |
| `DASHBOARD_URL` | – | Adds an "Open dashboard" button to the emails |

An invalid value (e.g. `CHECK_TIMES=7.45`) stops StatRep with a one-line message naming the
setting; see `docker compose logs`. After editing `.env`, run `docker compose up -d`.

---

## 8. Extra steps on a server

**Open the port.** Linux: `sudo ufw allow 9090/tcp`. Windows: allow port 9090 in
**Windows Defender Firewall → Advanced settings → Inbound Rules → New Rule → Port**. On a cloud
server, also allow 9090 in the provider's firewall / security group.

**Add a login** (recommended if others can reach the page). In `.env`:
```
AUTH_USER=admin
AUTH_PASSWORD=a-long-random-password
```
then `docker compose up -d`. The browser asks for the username and password once.

**HTTPS with a domain name (optional).** Put a reverse proxy in front. With
[Caddy](https://caddyserver.com/) the whole `Caddyfile` is:
```
status.example.com {
    reverse_proxy 127.0.0.1:9090
}
```
and in `.env`: `BIND=127.0.0.1`, `TRUSTED_PROXIES=1`, `DASHBOARD_URL=https://status.example.com`.
With nginx, forward the host and client IP (`proxy_set_header Host $host;` and
`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`), or the page buttons return 403.

**Time zone:** the schedule follows `TZ` in `.env`, not the machine's clock settings.

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `docker: command not found` / `cannot connect to the Docker daemon` | Windows: start Docker Desktop and wait for *Engine running*. Linux: `sudo systemctl start docker`; if it says *permission denied*, log out and in after the `usermod` step |
| Page doesn't open | `docker compose ps` should show *running*; check the port in `PORT`; on a server, open the firewall ([section 8](#8-extra-steps-on-a-server)) |
| Container keeps restarting | A setting in `.env` is invalid: `docker compose logs` shows `StatRep configuration error: …` |
| `env file .env not found` | Create `.env` ([section 3](#3-configure)); on Windows check it isn't called `.env.txt` |
| "Email not sent: SMTP settings are missing" | Fill in `SMTP_HOST`, `MAIL_FROM` and `MAIL_TO`, then `docker compose up -d` |
| "Email failed: SMTP login was rejected" | Wrong `SMTP_USER` / `SMTP_PASS` |
| "Email failed: the sender address was refused" | `MAIL_FROM` isn't accepted by your mail provider |
| A change to `.env` has no effect | Use `docker compose up -d`, not `restart` ([section 5](#5-after-you-change-something)) |
| A row says "Config error: …" | Fix that entry in `sites.yml` / `apis.yml` (the message says what), then `docker compose restart` |
| "Please wait N seconds…" | Manual runs and emails are limited by `RUN_COOLDOWN` |
| A site shows *Forbidden 403* but opens in your browser | It blocks automated checks (firewall, IP allow-list); ask its admins to allow this machine's IP |
| OneDrive / SharePoint links always Operational | They redirect to the Microsoft sign-in page, so StatRep can only tell that Microsoft 365 is reachable, not that the folder exists |
| 403 when clicking a button behind a proxy | Forward the original `Host` header ([section 8](#8-extra-steps-on-a-server)) |
| Page empty after a restart | Results are kept in memory; click **Check all** |

---

## 10. Without Docker (development)

Needs Python 3.12 or newer.

**Linux / Mac**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
set -a; source .env; set +a       # load the settings
gunicorn --workers 1 --threads 8 --bind 0.0.0.0:9090 "app.main:create_app()"
```

**Windows (PowerShell)** — gunicorn doesn't run on Windows, so use Flask's development server:
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object { $k, $v = $_ -split '=', 2; Set-Item "env:$($k.Trim())" $v.Trim() }
flask --app "app.main:create_app()" run --port 9090
```

Open <http://localhost:9090>; stop with `Ctrl+C`. After changing code, stop and start it again.
Keep one worker: the scheduler runs inside it, and more workers would send every report several times.

**Tests** (fake mail servers, no email is sent):
```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

---

## 11. Uninstall
```bash
docker compose down
docker image rm statrep-statrep
```
Then delete the `statrep` folder.
