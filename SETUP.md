# StatRep – Setup Guide

How to install and start StatRep on a PC or a server. Pick **one** way to get it:

| Way | Best for | Needs |
|---|---|---|
| [A. Clone from GitHub](#a-clone-from-github-recommended) | Servers and developers; easiest to update | Git, Docker, internet |
| [B. From a shared image file](#b-from-a-shared-image-file) | A machine that shouldn't build anything | Docker + the image file |
| [C. From a code zip](#c-from-a-code-zip) | When Git isn't available | Docker, internet |

Then do the [first-time configuration](#first-time-configuration) and [check that it works](#check-that-it-works).

---

## 1. Install Docker (once)

**Linux server (Ubuntu, Debian, etc.)**
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER     # lets you run docker without sudo; log out and back in
```

**Windows or Mac:** install **Docker Desktop** from <https://www.docker.com/products/docker-desktop/>
and start it. Use PowerShell (Windows) or Terminal (Mac) for the commands below.

> A PC only sends the scheduled reports while it's switched on, so a server is better for
> everyday use. A PC is fine for trying it out.

---

## A. Clone from GitHub (recommended)

```bash
git clone https://github.com/HasithaErandika/statrep.git
cd statrep
```

> Until the latest changes are merged into `main`, clone that branch instead:
> `git clone -b feature/apis-security-scaling https://github.com/HasithaErandika/statrep.git`

Do the [first-time configuration](#first-time-configuration), then start it in one of these ways.

### Start option 1: Docker Compose (recommended, for servers and everyday use)
```bash
docker compose up -d --build
```
Builds the image on this machine (so it suits Intel and Apple-silicon alike), starts it in the
background with all the security settings, and restarts it automatically after a reboot.

| Task | Command |
|---|---|
| See the logs | `docker compose logs -f` |
| Restart (after editing `sites.yml` / `apis.yml`) | `docker compose restart` |
| Apply `.env` changes | `docker compose up -d` |
| Stop | `docker compose down` |
| Update to the latest code | `git pull && docker compose up -d --build` |

### Start option 2: Python, without Docker (for development)
Needs Python 3.12 or newer.
```bash
python3 -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r requirements.txt

set -a; source .env; set +a               # load the settings (Linux/Mac)
gunicorn --workers 1 --threads 8 --bind 0.0.0.0:9090 "app.main:create_app()"
```
Open <http://localhost:9090>. Stop with `Ctrl+C`.

- Keep `--workers 1`: the scheduler runs inside the worker, so more workers would send
  every report several times.
- For quick experiments with automatic reload on code changes:
  `flask --app "app.main:create_app()" run --port 9090 --debug`
  (development only; never on a server).
- On Windows, gunicorn doesn't run; use Docker (option 1) or the `flask` command above, and
  set the variables from `.env` in PowerShell (`$env:SMTP_USER="..."`).

### Start option 3: Run the tests
```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```
Tests use fake mail servers, so no email is sent.

---

## B. From a shared image file

Someone has sent you `statrep-image.tar.gz` (made with
`docker save statrep-statrep:latest | gzip > statrep-image.tar.gz`). It's about 46 MB, so it
usually comes as a Google Drive / OneDrive link, or copy it to a server with
`scp statrep-image.tar.gz user@SERVER-IP:~`.

**1. Load the image**
```bash
docker load -i statrep-image.tar.gz
```

**2. Make a folder for the settings**
```bash
mkdir -p statrep/config
cd statrep
```
Put these files in it (ask the sender for them; none of them contain passwords):
- `.env.example` → copy it to `.env` and fill it in ([first-time configuration](#first-time-configuration))
- `config/sites.yml` and, if you monitor APIs, `config/apis.yml`

**3. Create `docker-compose.yml` in that folder**
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

**4. Start it**
```bash
docker compose up -d
```

**Updating:** load the new image file, then `docker compose up -d`.

> The image is built for Intel/AMD (`amd64`). On an Apple-silicon Mac (M1–M4) it runs through
> emulation (slower; add `platform: linux/amd64` under `statrep:` to silence the warning).
> Way A or C builds a native image instead.

---

## C. From a code zip

You received `statrep.zip` (made with `git archive --format=zip -o statrep.zip HEAD`).
```bash
unzip statrep.zip -d statrep
cd statrep
```
Then continue exactly like [way A](#a-clone-from-github-recommended): first-time configuration,
then **Start option 1** (`docker compose up -d --build`). Updating means getting a new zip.

---

## First-time configuration

**1. Settings and passwords: `.env`**
```bash
cp .env.example .env
chmod 600 .env          # Linux/Mac: only you can read it
```
Edit `.env`. The minimum to fill in:

| Setting | Example |
|---|---|
| `SMTP_HOST`, `SMTP_PORT` | `smtp-relay.brevo.com`, `587` |
| `SMTP_USER`, `SMTP_PASS` | your mail provider's SMTP login (leave empty for a company relay that whitelists your IP; add `SMTP_TLS=off` if it has no TLS) |
| `MAIL_FROM` | a sender address verified with the provider |
| `MAIL_TO` | `ops@example.com,dev@example.com` (any number, comma-separated) |
| `CHECK_TIMES` | `06:00,07:45,16:30,20:00` |
| `TZ` | `Asia/Colombo` |

Every other setting is optional and explained in [README.md](README.md#settings--env).
**Never email or commit `.env`**: it holds the SMTP password.

**2. Websites: `config/sites.yml`**
```yaml
- https://example.com/
- url: shop.example.com
  name: Online shop
```

**3. APIs (optional): `config/apis.yml`.** All options and examples are inside the file.
Tokens go in `.env` and are used as `${NAME}`.

---

## Check that it works

1. `docker compose logs` (or the terminal, without Docker) should end with **`Scheduler started`**.
   If it says `StatRep configuration error: …`, fix that setting in `.env`.
2. Open **http://localhost:9090** (PC) or **http://SERVER-IP:9090** (server).
   The header shows the next scheduled check.
3. Click **Run check & send report**. After a few seconds the results appear and every
   `MAIL_TO` address gets the email. If not, the page shows why (see
   [Troubleshooting](README.md#troubleshooting)).

---

## Extra steps on a server

**Open the port**
```bash
sudo ufw allow 9090/tcp
```
On a cloud server (AWS, Azure, DigitalOcean, …) also allow port 9090 in its firewall /
security group in the provider's web console.

**Add a login (recommended for public servers).** The page has no login by default. Add to `.env`:
```
AUTH_USER=admin
AUTH_PASSWORD=a-long-random-password
```
then `docker compose up -d`. The browser asks for the username and password once.

**HTTPS with a domain name (optional).** Run a reverse proxy in front. With
[Caddy](https://caddyserver.com/) (gets certificates automatically), the whole `Caddyfile` is:
```
status.example.com {
    reverse_proxy 127.0.0.1:9090
}
```
and in StatRep's `.env`:
```
BIND=127.0.0.1
TRUSTED_PROXIES=1
DASHBOARD_URL=https://status.example.com
```
With nginx, forward the original host and client IP:
`proxy_set_header Host $host;` and `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`
(otherwise the page buttons return 403).

**Time zone:** the schedule follows `TZ` in `.env`, not the server's clock settings.

**Reboots:** with Docker, StatRep starts again automatically (`restart: unless-stopped`).
Make sure Docker itself starts on boot: `sudo systemctl enable docker`.

---

## Uninstall
```bash
docker compose down
docker image rm statrep-statrep        # or statrep-statrep:latest for way B
```
Then delete the folder.
