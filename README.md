# StatRep - Website & API Status Reporter

StatRep checks your websites and APIs several times a day and emails a status report to your team.
Its web page shows the results and lets you re-check one site, check all, or email the report at any time.

- **What:** websites in `config/sites.yml`, APIs in `config/apis.yml`.
- **When:** every day at `CHECK_TIMES` (default 06:00, 07:45, 16:30, 20:00); scheduled reports are always emailed.
- **Result:** Operational, Slow or Down, with the reason (Forbidden, Server error, Timeout…), response time, SSL and domain expiry.

```bash
git clone https://github.com/HasithaErandika/statrep.git && cd statrep
cp .env.example .env            # fill in the SMTP settings and MAIL_TO
docker compose up -d --build    # then open http://localhost:9090
```

**[SETUP.md](SETUP.md)** covers Docker on Windows and Linux, every setting, how to restart after changes, and troubleshooting.
