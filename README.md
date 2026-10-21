# StatRep – Website Status Reporter

Checks a list of websites and emails a status report:
- automatically every day at **07:30** and **19:30** (Asia/Colombo)
- on demand from the web UI (**Run check & email report**)

Each site gets: UP / SLOW / DOWN, HTTP code, response time, SSL certificate days left,
and the page title (or the error). Failing sites are retried once before being marked DOWN.

## Project structure

```
statrep/
├── app/
│   ├── main.py          # Flask web UI + scheduler
│   ├── service.py       # runs a check and sends the email
│   ├── checker.py       # the actual website checks
│   ├── mailer.py        # SMTP email sending
│   ├── config.py        # settings from .env
│   ├── templates/       # index.html (UI), email.html (report)
│   └── static/style.css
├── config/sites.txt     # websites to check, one per line
├── .env                 # SMTP credentials + settings (not committed)
├── .env.example
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Run on a Linux server

```bash
cp .env.example .env      # then fill in SMTP details
docker compose up -d --build
```

Open `http://<server-ip>:8080`. The container restarts automatically after reboots.

## Common tasks

| Task | Command |
|------|---------|
| Add/remove a site | edit `config/sites.txt`, then `docker compose restart` |
| Change times / email | edit `.env`, then `docker compose up -d` |
| View logs | `docker compose logs -f` |
| Stop | `docker compose down` |

The UI shows the latest report only (no database); it is empty again after a restart
until the next check runs.
