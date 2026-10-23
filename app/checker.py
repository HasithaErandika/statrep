"""Website and API checks: availability, timing, redirects, SSL, domain expiry, security headers,
page info, and for APIs the expected status code / JSON fields / text."""
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import cached_property
from urllib.parse import urljoin, urlparse

import requests
import tldextract
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from . import config

HEADERS = {"User-Agent": "Mozilla/5.0 (StatRep uptime checker)"}
MAX_PAGE_BYTES = 2_000_000      # stop downloading pages/sitemaps beyond this
MAX_REDIRECTS = 10
CONNECT_TIMEOUT = 10            # a server that can't even accept a connection in 10 s is down
BLOCKED_ERROR = "Private or internal addresses can't be checked"
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
RDAP_RETRY_SECONDS = 15 * 60    # failed lookups (e.g. rate limited) are retried sooner
RDAP_CACHE_MAX = 1000           # Quick check can add any domain: keep the cache bounded
_rdap_cache = OrderedDict()     # domain -> (time, info, ttl), oldest first
_rdap_lock = threading.Lock()
_rdap_slots = threading.BoundedSemaphore(4)  # rdap.org rate-limits bursts (40+ sites at once)


@dataclass
class Result:
    url: str
    name: str
    status: str                        # UP | SLOW | DOWN
    code: int | None = None
    ms: int | None = None
    error: str | None = None
    kind: str = "site"                 # site | api
    # API
    method: str | None = None
    content_type: str | None = None
    size: int | None = None            # response body bytes
    checks: list = field(default_factory=list)  # [(label, passed, detail)]
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


class DateOnly(datetime):
    """A date without a time of day (e.g. sitemap <lastmod>2025-01-31</lastmod>), stored as UTC midnight."""


def _parse_date(value):
    value = (value or "").strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return DateOnly(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    return dt


def _days_until(dt):
    return int((dt - datetime.now(timezone.utc)).total_seconds() // 86400)


def _tags(html):
    """All <meta>/<link> tags as (tag, {attr: value}) with lower-cased attribute names."""
    out = []
    for m in TAG_RE.finditer(html):
        attrs = {k.lower(): (v1 or v2) for k, v1, v2 in ATTR_RE.findall(m.group(0))}
        out.append((m.group(1).lower(), attrs))
    return out


class BlockedAddress(requests.RequestException):
    """The URL points at a private/internal address (one-time checks only)."""


def _is_public(ip):
    ip = ipaddress.ip_address(ip)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class _Blocked(Exception):
    """Raised inside urllib3; deliberately not an OSError so urllib3 doesn't wrap or retry it."""


class _PublicOnlyConnection:
    """Resolves the host once, checks every address is public, then connects to exactly that
    address. Checking and connecting with the same lookup closes the DNS-rebinding window.
    TLS still uses the hostname (self.host) for SNI and certificate checks."""

    def _new_conn(self):
        infos = socket.getaddrinfo(self._dns_host, self.port, type=socket.SOCK_STREAM)
        ips = [info[4][0] for info in infos]
        if not ips or not all(_is_public(ip) for ip in ips):
            raise _Blocked(self.host)
        host = self._dns_host
        self._dns_host = ips[0]
        try:
            return super()._new_conn()
        finally:
            self._dns_host = host


class _PublicHTTPConnection(_PublicOnlyConnection, HTTPConnection):
    pass


class _PublicHTTPSConnection(_PublicOnlyConnection, HTTPSConnection):
    pass


class _PublicHTTPPool(HTTPConnectionPool):
    ConnectionCls = _PublicHTTPConnection


class _PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _PublicHTTPSConnection


class PublicOnlyAdapter(HTTPAdapter):
    """Every connection, including each redirect hop, may only go to a public address."""

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _PublicHTTPPool, "https": _PublicHTTPSPool}

    def send(self, request, *args, **kwargs):
        try:
            return super().send(request, *args, **kwargs)
        except _Blocked as e:
            raise BlockedAddress(f"{e} is a private or internal address", request=request) from None


def new_session(public_only=False):
    """One session per site check, so the page, sitemap and HTTP probe share connections."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.max_redirects = MAX_REDIRECTS
    session.trust_env = False  # ignore ~/.netrc and proxy env vars
    if public_only:
        adapter = PublicOnlyAdapter()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    return session


class Page:
    """A response whose body was read once (capped at MAX_PAGE_BYTES). Text is decoded once and
    cached; everything else (status_code, headers, url, history, ...) comes from the response."""

    def __init__(self, response, content):
        self.response = response
        self.content = content

    def __getattr__(self, name):
        return getattr(self.response, name)

    @cached_property
    def text(self):
        # Use the declared charset; without one requests would guess Latin-1, which garbles titles
        match = re.search(r"charset=[\"']?([\w.:-]+)", self.response.headers.get("Content-Type", ""), re.I)
        try:
            return self.content.decode(match.group(1) if match else "utf-8", errors="replace")
        except LookupError:  # unknown charset name
            return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content)


def _get(session, url, timeout, deadline=None, method="GET", **kwargs):
    """Request with the body capped at MAX_PAGE_BYTES and an overall download deadline.
    Connecting may take at most CONNECT_TIMEOUT; each wait for data at most `timeout`."""
    r = session.request(method, url, timeout=(min(CONNECT_TIMEOUT, timeout), timeout), stream=True, **kwargs)
    deadline = deadline or time.monotonic() + timeout
    chunks, size = [], 0
    try:
        for chunk in r.iter_content(64 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_PAGE_BYTES:
                break
            if time.monotonic() > deadline:
                raise requests.exceptions.Timeout("download took too long")
    finally:
        r.close()
    return Page(r, b"".join(chunks)[:MAX_PAGE_BYTES])


# --- Connection probe: DNS, TCP connect, TLS handshake and certificate ---------------------

def probe_connection(url, result, public_only=False):
    parsed = urlparse(url)
    host, https = parsed.hostname, parsed.scheme == "https"
    port = parsed.port or (443 if https else 80)
    try:
        t0 = time.perf_counter()
        addr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4]
        t1 = time.perf_counter()
        if public_only and not _is_public(addr[0]):
            return
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

    info = _rdap_cached(domain)
    if info is None:
        with _rdap_slots:
            info = _rdap_cached(domain)  # another thread may have looked it up while we waited
            if info is None:
                info = _rdap_lookup(domain)

    result.domain_registrar = info.get("registrar")
    if info.get("expires"):
        result.domain_expires = info["expires"]
        result.domain_days = _days_until(info["expires"])
    else:
        result.domain_note = "Expiry not published by registry"


def _rdap_cached(domain):
    with _rdap_lock:
        entry = _rdap_cache.get(domain)
    if entry and time.time() - entry[0] < entry[2]:
        return entry[1]
    return None


def _rdap_lookup(domain):
    info, ok = {}, False
    try:
        r = requests.get(f"https://rdap.org/domain/{domain}", headers=HEADERS, timeout=10)
        ok = r.ok or r.status_code == 404  # 404: registry has no RDAP data, no point retrying soon
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
        ok = False
    with _rdap_lock:
        _rdap_cache[domain] = (time.time(), info, RDAP_CACHE_SECONDS if ok else RDAP_RETRY_SECONDS)
        _rdap_cache.move_to_end(domain)
        while len(_rdap_cache) > RDAP_CACHE_MAX:
            _rdap_cache.popitem(last=False)
    return info


# --- Page analysis --------------------------------------------------------------------------

def last_updated(session, url, response, tags):
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
        r = _get(session, urljoin(url, "/sitemap.xml"), timeout=10)
        if r.ok:
            dates = [d for d in map(_parse_date, LASTMOD_RE.findall(r.text)) if d and d <= now]
            if dates:
                return max(dates), "sitemap.xml"
    except requests.RequestException:
        pass
    return None, None


def analyse_page(session, url, response, result):
    html = response.text[:300_000]
    tags = _tags(html)
    match = TITLE_RE.search(html)
    result.title = re.sub(r"\s+", " ", match.group(1)).strip()[:150] if match else None

    for t, a in tags:
        if t == "meta" and a.get("name", "").lower() == "description" and a.get("content"):
            result.description = re.sub(r"\s+", " ", a["content"]).strip()[:300]
        if t == "link" and "icon" in a.get("rel", "").lower().split() and a.get("href") and not result.favicon:
            favicon = urljoin(response.url, a["href"].strip())
            if urlparse(favicon).scheme in ("http", "https"):
                result.favicon = favicon
    result.favicon = result.favicon or urljoin(response.url, "/favicon.ico")
    result.updated, result.updated_source = last_updated(session, url, response, tags)

    headers = response.headers
    csp = headers.get("Content-Security-Policy", "")
    result.sec_headers = [
        (name, purpose, name in headers or (name == "X-Frame-Options" and "frame-ancestors" in csp))
        for name, purpose in SECURITY_HEADERS
    ]
    present = sum(p for _, _, p in result.sec_headers)
    result.sec_grade = "A" if present >= 5 else "B" if present == 4 else "C" if present == 3 else "D" if present == 2 else "F"


def https_enforced(session, url):
    """Does the plain-HTTP version of an HTTPS site redirect to HTTPS?"""
    parsed = urlparse(url)
    try:
        r = session.get(parsed._replace(scheme="http").geturl(), timeout=10,
                        allow_redirects=False, stream=True)
        r.close()
        return r.is_redirect and r.headers.get("Location", "").startswith("https://")
    except BlockedAddress:
        return None
    except requests.RequestException:
        return True  # HTTP not served at all: nothing insecure is exposed


# --- Main check -----------------------------------------------------------------------------

def _request_error(e, timeout):
    """Short, secret-free description of a failed request."""
    if isinstance(e, BlockedAddress):
        return BLOCKED_ERROR
    if isinstance(e, requests.exceptions.TooManyRedirects):
        return f"More than {MAX_REDIRECTS} redirects"
    if isinstance(e, requests.exceptions.SSLError):
        return "SSL certificate error"
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return f"Could not connect within {min(CONNECT_TIMEOUT, timeout)}s"
    if isinstance(e, requests.exceptions.Timeout):
        return f"Timed out after {timeout}s"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "Could not connect (DNS or server down)"
    return type(e).__name__


def _fill_timing(result, response):
    """Split the response time into server wait and download, using the connection probe."""
    ttfb = round(response.elapsed.total_seconds() * 1000)
    net = sum(result.timing.get(k, 0) for k in ("dns", "connect", "tls"))
    result.timing["server"] = max(0, ttfb - net)
    result.timing["download"] = max(0, result.ms - net - result.timing["server"])


def _fetch(session, url, name):
    start = time.perf_counter()
    try:
        r = _get(session, url, timeout=config.TIMEOUT, deadline=time.monotonic() + config.TIMEOUT * 2)
    except requests.RequestException as e:
        return Result(url, name, "DOWN", error=_request_error(e, config.TIMEOUT)), None
    ms = int((time.perf_counter() - start) * 1000)

    if r.status_code >= 400:
        return Result(url, name, "DOWN", r.status_code, ms, error=f"HTTP {r.status_code} {r.reason}"), r
    if len(r.text.strip()) < 50:
        return Result(url, name, "DOWN", r.status_code, ms, error="Page is empty"), r
    return Result(url, name, "SLOW" if ms > config.SLOW_MS else "UP", r.status_code, ms), r


def site_name(url):
    return url.split("//", 1)[-1].strip("/").removeprefix("www.")


class SiteCheck:
    """Checks one website in two phases (see check_everything):
    measure() makes the timed request and decides UP/SLOW/DOWN; finish() adds the details."""

    def __init__(self, site, public_only=False):
        """site: a URL or a sites.yml entry {url, name}.
        public_only: refuse private/internal addresses (for user-entered one-time URLs)."""
        if isinstance(site, str):
            site = {"url": site}
        self.url, self.public_only = site["url"], public_only
        self.name = site.get("name") or site_name(self.url)
        self.config_error = site.get("config_error")
        self.session = self.result = self.response = None

    def measure(self):
        if self.config_error:
            self.result = Result(self.url, self.name, "DOWN", error=self.config_error)
            return
        self.session = new_session(self.public_only)
        self.result, self.response = _fetch(self.session, self.url, self.name)
        for _ in range(config.RETRIES):
            if self.result.status != "DOWN" or self.result.error == BLOCKED_ERROR:
                break
            time.sleep(3)
            self.result, self.response = _fetch(self.session, self.url, self.name)

    def finish(self):
        if self.session is None:
            return self.result
        result, response, url = self.result, self.response, self.url
        try:
            # If the site couldn't be reached at all, probing it again would only time out again
            if response is not None:
                probe_connection(url, result, self.public_only)
            domain_info(url, result)
            if response is not None:
                result.final_url = response.url
                result.redirects = [{"code": h.status_code, "url": h.url} for h in response.history]
                _fill_timing(result, response)
                if result.status != "DOWN":
                    analyse_page(self.session, url, response, result)
            if response is not None and urlparse(url).scheme == "https":
                result.https_enforced = https_enforced(self.session, url)
        finally:
            self.session.close()
            self.response = None  # free the page body
        return result


def check_site(site, public_only=False):
    """Check a single website (both phases)."""
    check = SiteCheck(site, public_only)
    check.measure()
    return check.finish()


def check_all(sites, public_only=False):
    """Check sites (URLs or sites.yml entries); used by Quick check."""
    return _run([SiteCheck(s, public_only) for s in sites])


# --- APIs (config/apis.yml) ---------------------------------------------------------------

_MISSING = object()


def _json_path(data, path):
    """Value at a dotted path like 'data.items.0.id', or _MISSING."""
    for key in str(path).split("."):
        if isinstance(data, dict) and key in data:
            data = data[key]
        elif isinstance(data, list) and key.lstrip("-").isdigit() and -len(data) <= int(key) < len(data):
            data = data[int(key)]
        else:
            return _MISSING
    return data


def _same(got, want):
    """Equal, treating a number/boolean and its text form alike: 200 == "200", true == "true"."""
    if got == want:
        return True
    scalar = (str, int, float, bool, type(None))
    if isinstance(got, scalar) and isinstance(want, scalar):
        as_text = lambda v: v if isinstance(v, str) else json.dumps(v)  # noqa: E731
        return as_text(got) == as_text(want)
    return False


def _short(value, limit=60):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _strip_query(url):
    """Drop query strings from shown URLs: they may carry API keys."""
    return urlparse(url)._replace(query="", fragment="").geturl()


def _api_expectations(api, r):
    """[(label, passed, detail)] for every expectation of the API."""
    checks = []
    expected = api["expect_status"]
    if expected:
        ok = r.status_code in expected
        want = " or ".join(map(str, expected))
        checks.append((f"HTTP status is {want}", ok, None if ok else f"Expected HTTP {want}, got {r.status_code}"))
    else:
        ok = r.status_code < 400
        checks.append(("HTTP status below 400", ok, None if ok else f"HTTP {r.status_code} {r.reason}"))

    if api["expect_json"]:
        try:
            data = r.json()
        except ValueError:
            checks.append(("Response is JSON", False, "Response is not valid JSON"))
        else:
            for path, want in api["expect_json"].items():
                got = _json_path(data, path)
                if got is _MISSING:
                    checks.append((f"{path} = {_short(want)}", False, f"Field '{path}' is missing"))
                else:
                    ok = _same(got, want)
                    checks.append((f"{path} = {_short(want)}", ok,
                                   None if ok else f"{path}: expected {_short(want)}, got {_short(got)}"))

    if api["expect_text"]:
        ok = api["expect_text"] in r.text
        checks.append((f"Contains {_short(api['expect_text'])}", ok,
                       None if ok else f"Response does not contain {_short(api['expect_text'])}"))
    return checks


def _call_api(session, api):
    result = Result(api["display_url"], api["name"], "DOWN", kind="api", method=api["method"])
    timeout = api["timeout"]
    kwargs = {"headers": api["headers"], "allow_redirects": api["follow_redirects"]}
    body = api["body"]
    if isinstance(body, (dict, list)):
        kwargs["json"] = body
    elif body is not None:
        kwargs["data"] = str(body).encode()

    start = time.perf_counter()
    try:
        r = _get(session, api["url"], timeout=timeout, deadline=time.monotonic() + timeout * 2,
                 method=api["method"], **kwargs)
    except requests.RequestException as e:
        result.error = _request_error(e, timeout)
        return result, None
    result.ms = int((time.perf_counter() - start) * 1000)
    result.code = r.status_code
    result.content_type = r.headers.get("Content-Type", "").split(";")[0].strip() or None
    result.size = len(r.content)

    result.checks = _api_expectations(api, r)
    failed = [detail for _, ok, detail in result.checks if not ok]
    if failed:
        result.error = failed[0]
    else:
        result.status = "SLOW" if result.ms > api["slow_ms"] else "UP"
    return result, r


class ApiCheck:
    """Checks one API from config/apis.yml in two phases, like SiteCheck."""

    def __init__(self, api):
        self.api = api
        self.result = self.response = None

    def measure(self):
        api = self.api
        if api.get("config_error"):
            self.result = Result(api["display_url"], api["name"], "DOWN", kind="api",
                                 method=api.get("method"), error=api["config_error"])
            return
        with new_session() as session:
            self.result, self.response = _call_api(session, api)
            for _ in range(config.RETRIES if api["retry"] else 0):
                if self.result.status != "DOWN":
                    break
                time.sleep(3)
                self.result, self.response = _call_api(session, api)

    def finish(self):
        result, response, api = self.result, self.response, self.api
        if api.get("config_error"):
            return result
        if response is not None:  # unreachable: probing again would only time out again
            probe_connection(api["url"], result)
        domain_info(api["url"], result)
        if response is not None:
            result.final_url = _strip_query(response.url)
            result.redirects = [{"code": h.status_code, "url": _strip_query(h.url)} for h in response.history]
            _fill_timing(result, response)
        self.response = None
        return result


def check_api(api):
    """Call a single API and check the response against its expectations (both phases)."""
    check = ApiCheck(api)
    check.measure()
    return check.finish()


def _run(checks):
    """Run checks in two phases and return their results in order.

    1. measure: only the timed requests, at most TIMING_CONCURRENCY at once and nothing else
       running. Many simultaneous downloads and page parsing slow each other down, which would
       inflate response times (false SLOW) or even cause timeouts (false DOWN).
    2. finish: SSL, domain, page analysis, HTTPS check, etc. for all targets, up to
       CHECK_CONCURRENCY at once. Nothing here changes UP/SLOW/DOWN, so it can run wide.
    """
    if not checks:
        return []
    with ThreadPoolExecutor(max_workers=min(config.TIMING_CONCURRENCY, len(checks)),
                            thread_name_prefix="measure") as pool:
        list(pool.map(lambda c: c.measure(), checks))
    with ThreadPoolExecutor(max_workers=min(config.CONCURRENCY, len(checks)),
                            thread_name_prefix="details") as pool:
        return list(pool.map(lambda c: c.finish(), checks))


def check_everything(sites, apis):
    """Check all sites and APIs together. Returns (site results, API results) in config order."""
    results = _run([SiteCheck(s) for s in sites] + [ApiCheck(a) for a in apis])
    return results[:len(sites)], results[len(sites):]
