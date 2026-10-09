import hashlib
import json
import os
import re
import time
import zlib
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlparse

import requests

from .. import cache as kcache
from . import state

UA = "KellyBot/1.0 (usage personnel)"
ROBOTS_TOKEN = "KellyBot"
DEFAULT_DELAY = 3.0
MIN_DELAY = {"fbref.com": 6.0, "fotmob.com": 6.0, "betexplorer.com": 5.0}     # sites les plus sollicités : plus lents
FLOOR = {"fbref.com": 6.0}
TIMEOUT = (10, 30)
HOUR = 3600
TTL_CSV_CURRENT = 12 * HOUR
TTL_UNDERSTAT = 12 * HOUR
TTL_FBREF = 24 * HOUR
TTL_FOTMOB = 12 * HOUR
TTL_BETEXPLORER = 12 * HOUR
FOREVER = None
ROBOTS_TTL = 24 * HOUR
MAX_CRAWL_DELAY = 60.0

# Proxy par domaine (sources/xbet.py : 1xbet.ci passe par le proxy de l'utilisateur, jamais en direct) : connexion gardée ouverte
# (une seule poignée de main TLS) ; WIRE compte ce qui passe réellement par le réseau (octets compressés + en-têtes), par domaine.
PROXIES = {}
WIRE = {}
_sessions = {}
_sleep = time.sleep
_last = {}
_crawl = {}
_robots = {}
_deadline = None


def set_deadline(seconds=None):
    global _deadline
    _deadline = time.monotonic() + seconds if seconds else None


def monotonic():
    """Horloge du délai global (celle que fetch compare à get_deadline) ; remplaçable en bloc dans les tests en remplaçant `http.time`."""
    return time.monotonic()


def get_deadline():
    """Fin (time.monotonic) du délai global des téléchargements, ou None (sources.refresh_all le découpe entre les sources)."""
    return _deadline


def set_deadline_at(instant):
    global _deadline
    _deadline = instant


def cache_dir():
    return os.environ.get("SOURCES_CACHE_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".cache", "sources")


def reset():
    _last.clear(), _crawl.clear(), _robots.clear()


@dataclass
class Result:
    url: str
    content: bytes = None
    status: int = None
    from_cache: bool = False
    stale: bool = False
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


def _read_meta(key):
    """Seulement les métadonnées d'une entrée du cache (pas son corps, qui pèse jusqu'à ~1 Mo) ; None si absente."""
    try:
        with open(os.path.join(cache_dir(), key + ".json")) as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else None
    except (OSError, ValueError):
        return None


def fetched_at(url, headers=None):
    """Instant (état de l'horloge, voir state.clock) du dernier téléchargement de `url` gardé en cache, ou None s'il n'y en a pas."""
    meta = _read_meta(_key(url, headers))
    return meta.get("fetched_at") if meta else None


def age(url, headers=None):
    """Secondes écoulées depuis le dernier téléchargement de `url` ; None s'il n'a jamais été téléchargé (la page la plus ancienne qui soit)."""
    t = fetched_at(url, headers)
    return None if t is None else max(0.0, state.clock() - t)


def _touch(key):
    """Marque l'entrée comme utilisée maintenant (date de modification du fichier de métadonnées) : prune() oublie celles qu'on n'utilise plus."""
    try:
        os.utime(os.path.join(cache_dir(), key + ".json"))
    except OSError:
        pass


def prune(max_age_days, now=None):
    """Efface du cache disque les entrées inutilisées depuis plus de `max_age_days` jours (ni téléchargées ni relues : saisons de CSV hors de
    la fenêtre, anciennes pages de ligue, anciens chemins), ainsi que les fichiers temporaires et corps orphelins. Une entrée utilisée
    chaque matin (CSV des trois saisons suivies, pages de ligue) n'est jamais effacée : le dossier ne grossit plus sans fin. -> nombre
    d'entrées effacées."""
    now = time.time() if now is None else now
    cut = now - max_age_days * 86400
    d, n = cache_dir(), 0
    try:
        files = os.listdir(d)
    except OSError:
        return 0
    for f in files:
        path = os.path.join(d, f)
        try:
            if f.endswith(".tmp"):
                if os.path.getmtime(path) < now - 3600:              # reste d'une écriture interrompue (pas celle d'un autre processus en cours)
                    os.remove(path)
            elif f.endswith(".json"):
                if os.path.getmtime(path) < cut:
                    for ext in (".json", ".bin"):
                        try:
                            os.remove(os.path.join(d, f[:-5] + ext))
                        except OSError:
                            pass
                    n += 1
            elif f.endswith(".bin") and not os.path.exists(os.path.join(d, f[:-4] + ".json")):
                os.remove(path)
        except OSError:
            continue
    return n


def _write(key, meta, body):
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        base = os.path.join(cache_dir(), key)
        for ext, data, mode in ((".bin", body, "wb"), (".json", json.dumps(meta), "w")):
            with open(base + ext + ".tmp", mode) as f:
                f.write(data)
            os.replace(base + ext + ".tmp", base + ext)
    except OSError:
        pass


def invalidate(url, headers=None):
    base = os.path.join(cache_dir(), _key(url, headers))
    for ext in (".json", ".bin"):
        try:
            os.remove(base + ext)
        except OSError:
            pass


def is_cached(url, ttl, headers=None):
    c = _read(_key(url, headers))
    return bool(c) and _fresh(c[0], ttl)


def cached(url, headers=None):
    c = _read(_key(url, headers))
    return Result(url, c[1], c[0].get("status", 200), from_cache=True) if c else None


def _fresh(meta, ttl):
    return ttl is None or state.clock() - meta.get("fetched_at", 0) < ttl


DELAY_OVERRIDE = {}       # domaine -> écart imposé (xbet.live_mode : 1 s pendant une vérification en direct demandée sur Telegram)
DELAY_REDUCED = {}        # domaine -> écart réduit qui cède toujours au Crawl-delay du robots.txt (s365 : rattrapage ponctuel)


def delay_for(domain):
    domain = domain.lower().split(":")[0]
    if domain in DELAY_OVERRIDE:
        return DELAY_OVERRIDE[domain]
    if domain in DELAY_REDUCED:
        return max(DELAY_REDUCED[domain], min(_crawl.get(domain, 0.0), MAX_CRAWL_DELAY))
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
    domain = urlparse(url).netloc.lower()
    h = {"User-Agent": UA, "Accept": "*/*"} | (headers or {})
    r, err = None, None
    for attempt in (0, 1):
        _wait(domain)
        proxy = PROXIES.get(domain)
        try:
            if proxy:
                s = _sessions.get(domain) or _sessions.setdefault(domain, requests.Session())
                r = s.get(url, headers=h, timeout=TIMEOUT, proxies={"http": proxy, "https": proxy})
            else:
                r = requests.get(url, headers=h, timeout=TIMEOUT)
            _count(domain, r)
        except requests.RequestException as e:     # le message ne cite jamais le proxy (il contient le mot de passe)
            r, err = None, f"réseau indisponible ({type(e).__name__})"
            stale = _sessions.pop(domain, None)     # connexion par le proxy cassée : le nouvel essai en ouvre une neuve
            if stale:
                stale.close()
        _last[domain] = state.clock()
        if r is not None and r.status_code < 500:
            return r, None
        if r is not None:
            err = f"HTTP {r.status_code}"
        if attempt == 0:
            continue
    return (r, None) if r is not None else (None, err)


def _not_sent(e):
    """L'erreur est survenue AVANT l'envoi de la requête (connexion impossible : délai de connexion, proxy, connexion refusée) : la
    rejouer ne risque pas de doubler une écriture. Un délai de lecture, une coupure en route ou un 5xx : la requête a pu arriver."""
    if isinstance(e, (requests.ConnectTimeout, requests.exceptions.ProxyError)):
        return True
    return isinstance(e, requests.ConnectionError) and not isinstance(e, requests.Timeout) and "NewConnectionError" in repr(e)


def post(url, body, headers=None, timeout=TIMEOUT):
    """POST JSON (ticket enregistré chez le bookmaker...) : comme _request mais en écriture — ni cache, ni robots.txt, ni
    conditionnel : une écriture n'est jamais rejouée depuis le cache, ni renvoyée si elle a pu arriver (délai de lecture, 5xx) :
    un seul nouvel essai, et seulement quand la connexion n'a pas pu s'établir (_not_sent). Même session proxy par domaine et même
    compteur WIRE. Retourne un Result : .ok si une réponse HTTP (<500) a été obtenue, .content brut, .error sinon."""
    domain = urlparse(url).netloc.lower()
    h = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"} | (headers or {})
    r, err = None, None
    for attempt in (0, 1):
        _wait(domain)
        proxy = PROXIES.get(domain)
        retry = False
        try:
            if proxy:
                s = _sessions.get(domain) or _sessions.setdefault(domain, requests.Session())
                r = s.post(url, headers=h, json=body, timeout=timeout, proxies={"http": proxy, "https": proxy})
            else:
                r = requests.post(url, headers=h, json=body, timeout=timeout)
            _count(domain, r)
        except requests.RequestException as e:     # le message ne cite jamais le proxy (il contient le mot de passe)
            r, err, retry = None, f"réseau indisponible ({type(e).__name__})", _not_sent(e)
            stale = _sessions.pop(domain, None)    # connexion par le proxy cassée : le nouvel essai en ouvre une neuve
            if stale:
                stale.close()
        _last[domain] = state.clock()
        if r is not None and r.status_code < 500:
            return Result(url, r.content, r.status_code)
        if r is not None:
            err = f"HTTP {r.status_code}"           # le serveur a reçu la requête : jamais renvoyée
        if attempt == 0 and retry:
            continue
        break
    return Result(url, None, error=err)


def _count(domain, r):
    """Octets réellement transférés pour `r` (corps compressé + en-têtes, estimés) ajoutés à WIRE[domaine]."""
    content = getattr(r, "content", None) or b""
    enc = str((getattr(r, "headers", None) or {}).get("Content-Encoding") or "").lower()
    # corps compressé par le site : sa taille réelle n'est pas exposée (transfert par morceaux) ; on la retrouve en recompressant,
    # + 10 % de marge (compteur prudent pour le quota du proxy)
    body = len(zlib.compress(content, 6)) * 1.1 if enc in ("gzip", "deflate", "br") else len(content)
    w = WIRE.setdefault(domain, {"bytes": 0, "requests": 0})
    w["bytes"] += int(body) + 600 + sum(len(k) + len(str(v)) + 4 for k, v in (getattr(r, "headers", None) or {}).items())
    w["requests"] += 1


def _load_robots(scheme, domain):
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


_CHALLENGE_TITLE = re.compile(r"<title>[^<]*(just a moment|attention required|access denied|are you a robot|"
                              r"verify(ing)? you are human|captcha)", re.I)
_CHALLENGE_BODY = ("cf_chl_opt", "cf-browser-verification", "verify you are human", "captcha-delivery")


def is_challenge(body):
    head = body[:60000].decode("utf-8", "ignore")
    low = head.lower()
    return bool(_CHALLENGE_TITLE.search(head)) or (len(body) < 150000 and any(m in low for m in _CHALLENGE_BODY))


def _fail(url, cached, msg, blocked=False, status=None):
    if cached:
        return Result(url, cached[1], status, from_cache=True, stale=True, blocked=blocked, error=msg)
    return Result(url, None, status, blocked=blocked, error=msg)


def fetch(url, source, ttl, headers=None, check_robots=True):
    key = _key(url, headers)
    cached = _read(key)
    if cached:
        _touch(key)                       # « utilisée » (même servie périmée) : prune() n'efface que ce qu'on ne relit plus
    if state.is_blocked(source):
        return _fail(url, cached, state.health(source)["last_error"] or "source bloquée", blocked=True)
    if cached and _fresh(cached[0], ttl):
        kcache.hit(source, 1)
        return Result(url, cached[1], cached[0].get("status", 200), from_cache=True)
    if kcache.OFFLINE:
        return Result(url, cached[1], cached[0].get("status", 200), from_cache=True, stale=True) if cached else Result(
            url, None, error="hors ligne : pas de téléchargement en dehors des passages programmés")
    if _deadline is not None and time.monotonic() > _deadline:
        return _fail(url, cached, "délai dépassé, téléchargement reporté au prochain passage")
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
    kcache.miss(source, 1)
    r, err = _request(url, (headers or {}) | cond)
    if r is None:
        return _fail(url, cached, err)
    code = r.status_code
    if code == 304 and cached:
        meta = cached[0] | {"fetched_at": state.clock()}
        _write(key, meta, cached[1])
        return Result(url, cached[1], 200, from_cache=True)
    h = getattr(r, "headers", None) or {}
    if any(str(k).lower() == "cf-mitigated" for k in h):
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
