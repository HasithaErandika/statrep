"""Website checks: availability, timing, redirects, SSL, domain expiry, security headers, page info."""
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
import tldextract

from . import config

HEADERS = {"User-Agent": "Mozilla/5.0 (StatRep uptime checker)"}
TAG_RE = re.compile(r"<(meta|link)\b[^>]*>", re.I)
ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
JSONLD_RE = re.compile(r"\"dateModified\"\s*:\s*\"([^\"]+)\"")
LASTMOD_RE = re.compile(r"<lastmod>\s*([^<\s]+)", re.I)
MODIFIED_META = {"article:modified_time", "og:updated_time", "datemodified", "last-modified"}

SECURITY_HEADERS = [
    ("Strict-Transport-Security", "Forces browsers to use HTTPS"),
    ("Content-Security-Policy", "Limits which scripts and resources can load"),
    ("X-Frame-Options", "Stops the site being embedded by other sites"),
    ("X-Content-Type-Options", "Stops browsers guessing file types"),
    ("Referrer-Policy", "Controls what is shared when users click links"),
    ("Permissions-Policy", "Restricts camera, microphone and similar features"),
]

# Bundled public-suffix list (no network); private suffixes mark shared hosts like github.io
EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)
RDAP_CACHE_SECONDS = 12 * 3600
_rdap_cache = {}
_rdap_lock = threading.Lock()


@dataclass
class Result:
    url: str
    name: str
    status: str                        # UP | SLOW | DOWN
    code: int | None = None
    ms: int | None = None
    error: str | None = None
    # Page
    title: str | None = None
    description: str | None = None
    favicon: str | None = None
    updated: datetime | None = None    # when the site content last changed (UTC)
    updated_source: str | None = None
    # Timing breakdown in ms: dns, connect, tls, server, download
    timing: dict = field(default_factory=dict)
    # Redirects
    final_url: str | None = None
    redirects: list = field(default_factory=list)   # [{"code": 301, "url": ...}, ...]
    https_enforced: bool | None = None
    # SSL
    ssl_days: int | None = None
    ssl_expires: datetime | None = None
    ssl_issuer: str | None = None
    tls_version: str | None = None
    # Domain
    domain: str | None = None
    domain_days: int | None = None
    domain_expires: datetime | None = None
    domain_registrar: str | None = None
    domain_note: str | None = None
    # Security headers
    sec_grade: str | None = None
    sec_headers: list = field(default_factory=list)  # [(name, purpose, present)]

    @property
    def expiring(self):
        """True if the SSL certificate or the domain expires soon."""
        return ((self.ssl_days is not None and self.ssl_days < config.SSL_WARN_DAYS)
                or (self.domain_days is not None and self.domain_days < config.DOMAIN_WARN_DAYS))


def _parse_date(value):
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _days_until(dt):
    return int((dt - datetime.now(timezone.utc)).total_seconds() // 86400)


def _tags(html):
    """All <meta>/<link> tags as (tag, {attr: value}) with lower-cased attribute names."""
    out = []
    for m in TAG_RE.finditer(html):
        attrs = {k.lower(): (v1 or v2) for k, v1, v2 in ATTR_RE.findall(m.group(0))}
        out.append((m.group(1).lower(), attrs))
    return out


# --- Connection probe: DNS, TCP connect, TLS handshake and certificate ---------------------

def probe_connection(url, result):
    parsed = urlparse(url)
    host, https = parsed.hostname, parsed.scheme == "https"
    port = parsed.port or (443 if https else 80)
    try:
        t0 = time.perf_counter()
        addr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4]
        t1 = time.perf_counter()
        with socket.create_connection(addr[:2], timeout=10) as sock:
            t2 = time.perf_counter()
            result.timing.update(dns=round((t1 - t0) * 1000), connect=round((t2 - t1) * 1000))
            if not https:
                return
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                result.timing["tls"] = round((time.perf_counter() - t2) * 1000)
                cert = tls.getpeercert()
                result.tls_version = tls.version()
        issuer = dict(item[0] for item in cert.get("issuer", ()))
        result.ssl_issuer = issuer.get("organizationName") or issuer.get("commonName")
        result.ssl_expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
        result.ssl_days = _days_until(result.ssl_expires)
    except (OSError, ssl.SSLError, KeyError, ValueError, IndexError):
        pass


# --- Domain registration (RDAP, cached) ---------------------------------------------------

def domain_info(url, result):
    ext = EXTRACT(urlparse(url).hostname or "")
    if not ext.suffix or not ext.domain:
        return
    if ext.is_private:
        result.domain = f"{ext.domain}.{ext.suffix}"
        result.domain_note = f"Shared hosting ({ext.suffix})"
        return
    domain = f"{ext.domain}.{ext.suffix}"
    result.domain = domain

    with _rdap_lock:
        cached = _rdap_cache.get(domain)
    if cached and time.time() - cached[0] < RDAP_CACHE_SECONDS:
        info = cached[1]
    else:
        info = {}
        try:
            r = requests.get(f"https://rdap.org/domain/{domain}", headers=HEADERS, timeout=10)
            if r.ok:
                data = r.json()
                for e in data.get("events", []):
                    if e.get("eventAction") == "expiration":
                        info["expires"] = _parse_date(e.get("eventDate", ""))
                for ent in data.get("entities", []):
                    if "registrar" in ent.get("roles", []):
                        for v in (ent.get("vcardArray") or [None, []])[1]:
                            if v[0] == "fn":
                                info["registrar"] = v[3]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
            pass
        with _rdap_lock:
            _rdap_cache[domain] = (time.time(), info)

    result.domain_registrar = info.get("registrar")
    if info.get("expires"):
        result.domain_expires = info["expires"]
        result.domain_days = _days_until(info["expires"])
    else:
        result.domain_note = "Expiry not published by registry"


# --- Page analysis --------------------------------------------------------------------------

def last_updated(url, response, tags):
    """Best guess of when the site was last updated: header, page metadata, then sitemap."""
    now = datetime.now(timezone.utc)
    header = response.headers.get("Last-Modified")
    if header:
        dt = _parse_date(header)
        # Dynamic pages report "now" as Last-Modified, which says nothing about real updates
        if dt and (now - dt).total_seconds() > 300:
            return dt, "Last-Modified header"

    values = [a.get("content", "") for t, a in tags if t == "meta"
              and (a.get("property") or a.get("name") or a.get("itemprop") or "").lower() in MODIFIED_META]
    values += JSONLD_RE.findall(response.text[:200_000])
    dates = [d for d in map(_parse_date, values) if d and d <= now]
    if dates:
        return max(dates), "page metadata"

    try:
        r = requests.get(urljoin(url, "/sitemap.xml"), headers=HEADERS, timeout=10)
        if r.ok:
            dates = [d for d in map(_parse_date, LASTMOD_RE.findall(r.text)) if d and d <= now]
            if dates:
                return max(dates), "sitemap.xml"
    except requests.RequestException:
        pass
    return None, None


def analyse_page(url, response, result):
    html = response.text[:300_000]
    tags = _tags(html)
    match = TITLE_RE.search(html)
    result.title = re.sub(r"\s+", " ", match.group(1)).strip()[:150] if match else None

    for t, a in tags:
        if t == "meta" and a.get("name", "").lower() == "description" and a.get("content"):
            result.description = re.sub(r"\s+", " ", a["content"]).strip()[:300]
        if t == "link" and "icon" in a.get("rel", "").lower().split() and a.get("href") and not result.favicon:
            result.favicon = urljoin(response.url, a["href"])
    result.favicon = result.favicon or urljoin(response.url, "/favicon.ico")
    result.updated, result.updated_source = last_updated(url, response, tags)

    headers = response.headers
    csp = headers.get("Content-Security-Policy", "")
    result.sec_headers = [
        (name, purpose, name in headers or (name == "X-Frame-Options" and "frame-ancestors" in csp))
        for name, purpose in SECURITY_HEADERS
    ]
    present = sum(p for _, _, p in result.sec_headers)
    result.sec_grade = "A" if present >= 5 else "B" if present == 4 else "C" if present == 3 else "D" if present == 2 else "F"


def https_enforced(url):
    """Does the plain-HTTP version of an HTTPS site redirect to HTTPS?"""
    parsed = urlparse(url)
    try:
        r = requests.get(parsed._replace(scheme="http").geturl(), headers=HEADERS, timeout=10,
                         allow_redirects=False, stream=True)
        r.close()
        return r.is_redirect and r.headers.get("Location", "").startswith("https://")
    except requests.RequestException:
        return True  # HTTP not served at all: nothing insecure is exposed


# --- Main check -----------------------------------------------------------------------------

def _fetch(url, name):
    start = time.perf_counter()
    try:
        r = requests.get(url, headers=HEADERS, timeout=config.TIMEOUT)
    except requests.exceptions.SSLError:
        return Result(url, name, "DOWN", error="SSL certificate error"), None
    except requests.exceptions.Timeout:
        return Result(url, name, "DOWN", error=f"Timed out after {config.TIMEOUT}s"), None
    except requests.exceptions.ConnectionError:
        return Result(url, name, "DOWN", error="Could not connect (DNS or server down)"), None
    except requests.RequestException as e:
        return Result(url, name, "DOWN", error=type(e).__name__), None
    ms = int((time.perf_counter() - start) * 1000)
    if "charset" not in r.headers.get("Content-Type", "").lower():
        r.encoding = "utf-8"  # requests defaults to Latin-1, which garbles titles

    if r.status_code >= 400:
        return Result(url, name, "DOWN", r.status_code, ms, error=f"HTTP {r.status_code} {r.reason}"), r
    if len(r.text.strip()) < 50:
        return Result(url, name, "DOWN", r.status_code, ms, error="Page is empty"), r
    return Result(url, name, "SLOW" if ms > config.SLOW_MS else "UP", r.status_code, ms), r


def site_name(url):
    return url.split("//", 1)[-1].strip("/").removeprefix("www.")


def check_site(url):
    name = site_name(url)
    result, response = _fetch(url, name)
    for _ in range(config.RETRIES):
        if result.status != "DOWN":
            break
        time.sleep(3)
        result, response = _fetch(url, name)

    probe_connection(url, result)
    domain_info(url, result)
    if response is not None:
        result.final_url = response.url
        result.redirects = [{"code": h.status_code, "url": h.url} for h in response.history]
        ttfb = round(response.elapsed.total_seconds() * 1000)
        net = sum(result.timing.get(k, 0) for k in ("dns", "connect", "tls"))
        result.timing["server"] = max(0, ttfb - net)
        result.timing["download"] = max(0, result.ms - net - result.timing["server"])
        if result.status != "DOWN":
            analyse_page(url, response, result)
    if urlparse(url).scheme == "https":
        result.https_enforced = https_enforced(url)
    return result


def check_all(urls):
    """Check all sites in parallel."""
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(10, len(urls))) as pool:
        return list(pool.map(check_site, urls))
