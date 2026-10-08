"""Téléchargement poli pour les sources publiques (football-data, Understat, FBref).

Règles : User-Agent honnête, délai minimal par domaine (FBref >= 6 s, autres >= 3 s, ou le
Crawl-delay de robots.txt s'il est plus grand), robots.txt respecté, timeouts, un seul nouvel
essai (5xx / timeout), requêtes conditionnelles (ETag / If-Modified-Since), cache disque.
AUCUN contournement : sur 403 / 429 / page de vérification anti-robot, la source est bloquée
24 h (état dans data/) et le bot continue avec les autres.
"""
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlparse

import requests

from . import state

UA = "KellyBot/1.0 (usage personnel)"
ROBOTS_TOKEN = "KellyBot"
DEFAULT_DELAY = 3.0
MIN_DELAY = {"fbref.com": 6.0}          # délais par domaine (suffixe) ; jamais sous FLOOR
FLOOR = {"fbref.com": 6.0}
TIMEOUT = (10, 30)
HOUR = 3600
TTL_CSV_CURRENT = 12 * HOUR              # saison en cours (football-data)
TTL_UNDERSTAT = 12 * HOUR
TTL_FBREF = 24 * HOUR
FOREVER = None                           # saisons passées : jamais rafraîchies
ROBOTS_TTL = 24 * HOUR
MAX_CRAWL_DELAY = 60.0

_sleep = time.sleep                      # remplaçables dans les tests
_last = {}                               # domaine -> instant de la dernière réponse
_crawl = {}                              # domaine -> Crawl-delay de robots.txt
_robots = {}                             # domaine -> (instant, RobotFileParser | None)


def cache_dir():
    return os.environ.get("SOURCES_CACHE_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".cache", "sources")


def reset():
    """Oublie l'état mémoire (délais, robots) : tests."""
    _last.clear(), _crawl.clear(), _robots.clear()


@dataclass
class Result:
    url: str
    content: bytes = None
    status: int = None
    from_cache: bool = False
    stale: bool = False                  # contenu périmé servi faute de mieux
    blocked: bool = False
    error: str = None

    @property
    def ok(self):
        return self.content is not None

    @property
    def text(self):
        if self.content is None:
            return ""
        try:
            return self.content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return self.content.decode("latin-1")


# --- Cache disque ----------------------------------------------------------

def _key(url, headers=None):
    raw = url + "|" + json.dumps(sorted((headers or {}).items()))
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


def _read(key):
    base = os.path.join(cache_dir(), key)
    try:
        with open(base + ".json") as f:
            meta = json.load(f)
        with open(base + ".bin", "rb") as f:
            return meta, f.read()
    except (OSError, ValueError):
        return None


def _write(key, meta, body):
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        base = os.path.join(cache_dir(), key)
        for ext, data, mode in ((".bin", body, "wb"), (".json", json.dumps(meta), "w")):
            with open(base + ext + ".tmp", mode) as f:
                f.write(data)
            os.replace(base + ext + ".tmp", base + ext)
    except OSError:
        pass                              # cache indisponible : on travaille sans


def invalidate(url, headers=None):
    """Oublie un téléchargement (contenu inexploitable : ne pas le garder en cache)."""
    base = os.path.join(cache_dir(), _key(url, headers))
    for ext in (".json", ".bin"):
        try:
            os.remove(base + ext)
        except OSError:
            pass


def is_cached(url, ttl, headers=None):
    """Vrai si un téléchargement frais est déjà en cache (aucune requête nécessaire)."""
    c = _read(_key(url, headers))
    return bool(c) and _fresh(c[0], ttl)


def cached(url, headers=None):
    """Contenu déjà en cache disque, périmé ou non, SANS aucune requête : Result ou None (règlement gratuit)."""
    c = _read(_key(url, headers))
    return Result(url, c[1], c[0].get("status", 200), from_cache=True) if c else None


def _fresh(meta, ttl):
    return ttl is None or state.clock() - meta.get("fetched_at", 0) < ttl


# --- Politesse -------------------------------------------------------------

def delay_for(domain):
    domain = domain.lower().split(":")[0]
    floor = next((v for k, v in FLOOR.items() if domain.endswith(k)), DEFAULT_DELAY)
    conf = next((v for k, v in MIN_DELAY.items() if domain.endswith(k)), DEFAULT_DELAY)
    return max(floor, conf, min(_crawl.get(domain, 0.0), MAX_CRAWL_DELAY))


def _wait(domain):
    t = _last.get(domain)
    if t is not None:
        gap = delay_for(domain) - (state.clock() - t)
        if gap > 0:
            _sleep(gap)


def _request(url, headers=None):
    """GET poli avec un seul nouvel essai. (réponse, None) ou (None, message d'erreur)."""
    domain = urlparse(url).netloc.lower()
    h = {"User-Agent": UA, "Accept": "*/*"} | (headers or {})
    r, err = None, None
    for attempt in (0, 1):
        _wait(domain)
        try:
            r = requests.get(url, headers=h, timeout=TIMEOUT)
        except requests.RequestException as e:
            r, err = None, f"réseau indisponible ({type(e).__name__})"
        _last[domain] = state.clock()
        if r is not None and r.status_code < 500:
            return r, None
        if r is not None:
            err = f"HTTP {r.status_code}"
        if attempt == 0:
            continue
    return (r, None) if r is not None else (None, err)


def _load_robots(scheme, domain):
    """(RobotFileParser | None, bloqué) ; None = injoignable (on ne télécharge pas)."""
    url = f"{scheme}://{domain}/robots.txt"
    cached = _read(_key(url))
    text, code = None, None
    if cached and state.clock() - cached[0].get("fetched_at", 0) < ROBOTS_TTL:
        text, code = cached[1].decode("utf-8", "replace"), cached[0].get("status", 200)
    else:
        r, err = _request(url)
        if r is None or r.status_code >= 500:
            return None, False
        code = r.status_code
        text = r.content.decode("utf-8", "replace") if code == 200 else ""
        _write(_key(url), {"fetched_at": state.clock(), "status": code}, text.encode())
    if code in (401, 403, 429):
        return None, True
    rp = robotparser.RobotFileParser()
    rp.parse(text.splitlines() if code == 200 else [])
    try:
        cd = rp.crawl_delay(ROBOTS_TOKEN)
        _crawl[domain] = float(cd) if cd else 0.0
    except (TypeError, ValueError):
        pass
    return rp, False


def allowed(url):
    """(autorisé, raison, bloqué) selon robots.txt (mis en cache 24 h)."""
    p = urlparse(url)
    domain = p.netloc.lower()
    ent = _robots.get(domain)
    if ent is None or state.clock() - ent[0] > ROBOTS_TTL:
        rp, blocked = _load_robots(p.scheme, domain)
        if blocked:
            return False, f"robots.txt refusé par {domain}", True
        if rp is None:
            return False, f"robots.txt de {domain} injoignable", False
        ent = _robots[domain] = (state.clock(), rp)
    if ent[1].can_fetch(ROBOTS_TOKEN, url):
        return True, None, False
    return False, f"robots.txt interdit {p.path or '/'} sur {domain}", False


# --- Page de vérification anti-robot (détectée, jamais contournée) ----------

_CHALLENGE_TITLE = re.compile(r"<title>[^<]*(just a moment|attention required|access denied|are you a robot|"
                              r"verify(ing)? you are human|captcha)", re.I)
_CHALLENGE_BODY = ("cf_chl_opt", "cf-browser-verification", "verify you are human", "captcha-delivery")


def is_challenge(body):
    head = body[:60000].decode("utf-8", "ignore")
    low = head.lower()
    return bool(_CHALLENGE_TITLE.search(head)) or (len(body) < 150000 and any(m in low for m in _CHALLENGE_BODY))


# --- Point d'entrée ----------------------------------------------------------

def _fail(url, cached, msg, blocked=False, status=None):
    if cached:                            # mieux vaut des données anciennes que rien
        return Result(url, cached[1], status, from_cache=True, stale=True, blocked=blocked, error=msg)
    return Result(url, None, status, blocked=blocked, error=msg)


def fetch(url, source, ttl, headers=None, check_robots=True):
    """Télécharge `url` pour la source `source` (cache disque, robots, délais, blocage).
    `ttl` en secondes (FOREVER = jamais périmé). Ne lève jamais : voir Result.ok / .error."""
    key = _key(url, headers)
    cached = _read(key)
    if state.is_blocked(source):
        return _fail(url, cached, state.health(source)["last_error"] or "source bloquée", blocked=True)
    if cached and _fresh(cached[0], ttl):
        return Result(url, cached[1], cached[0].get("status", 200), from_cache=True)
    if check_robots:
        ok, why, blocked = allowed(url)
        if not ok:
            if blocked:
                state.block(source, why)
            return _fail(url, cached, why, blocked=blocked)
    cond = {}
    if cached:
        if cached[0].get("etag"):
            cond["If-None-Match"] = cached[0]["etag"]
        if cached[0].get("last_modified"):
            cond["If-Modified-Since"] = cached[0]["last_modified"]
    r, err = _request(url, (headers or {}) | cond)
    if r is None:
        return _fail(url, cached, err)
    code = r.status_code
    if code == 304 and cached:
        meta = cached[0] | {"fetched_at": state.clock()}
        _write(key, meta, cached[1])
        return Result(url, cached[1], 200, from_cache=True)
    h = getattr(r, "headers", None) or {}
    if any(str(k).lower() == "cf-mitigated" for k in h):          # Cloudflare exige un défi : on n'insiste pas
        state.block(source, f"HTTP {code}, Cloudflare", cloudflare=True)
        return _fail(url, cached, "Cloudflare bloque les robots", blocked=True, status=code)
    if code in (403, 429):
        state.block(source, f"HTTP {code}")
        return _fail(url, cached, f"HTTP {code} : accès refusé", blocked=True, status=code)
    if code >= 400:
        return _fail(url, cached, f"HTTP {code}", status=code)
    body = r.content or b""
    if is_challenge(body):
        state.block(source, "page de vérification anti-robot")
        return _fail(url, cached, "page de vérification anti-robot", blocked=True, status=code)
    _write(key, {"url": url, "fetched_at": state.clock(), "status": code,
                 "etag": h.get("ETag"), "last_modified": h.get("Last-Modified")}, body)
    return Result(url, body, code)
