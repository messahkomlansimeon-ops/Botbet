import contextlib
import hashlib
import json
import os
import re
import socket
import threading
import time
import zlib
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlparse

import requests

from .. import cache as kcache, config as C
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
# Domaines qui ne partent JAMAIS en direct (1xBet, 365scores : require_proxy) : sans proxy, aucune requête n'est envoyée, ton IP n'est jamais
# montrée. Seuls les tests (C.SANDBOX) peuvent les lire sans proxy (réseau simulé).
STRICT = set()
NO_PROXY = "proxy absent : rien n'est envoyé (jamais en direct)"
# Proxy injoignable (DNS, connexion refusée ou trop lente, pas une session Webshare en panne) : tenu pour en panne PROXY_DOWN_S secondes, les
# requêtes suivantes échouent tout de suite au lieu d'attendre chacune leur délai (nuit du 12 au 13/10 : un passage bloqué des minutes).
PROXY_DOWN_S = 120
DNS_WAIT = 8.0                    # secondes au plus pour trouver l'adresse du proxy (la résolution DNS n'a pas de délai dans requests)
DNS_OK_S = 600                    # adresse trouvée : pas re-vérifiée avant 10 min
_proxy_down = {}                  # « hôte:port » du proxy -> instant (monotonic) jusqu'auquel il est tenu pour injoignable
# Un SEUL échec ne met jamais tout le proxy en panne (13/10 13h06 : une coupure isolée bloquait 2 min tous les relevés 1xBet, rotation des sessions
# Webshare comprise) : il en faut HOST_FAILS en HOST_FAIL_WINDOW s, et seulement ceux qui disent que le serveur du proxy lui-même est injoignable.
HOST_FAILS, HOST_FAIL_WINDOW = 2, 60
_host_fails = {}                  # « hôte:port » -> instants (monotonic) des derniers échecs « serveur du proxy injoignable »
_dns_ok = {}                      # hôte du proxy -> instant de la dernière résolution réussie
_sessions = {}
_sleep = time.sleep
_last = {}
_crawl = {}
_robots = {}
_deadline = None


def set_deadline(seconds=None):
    """Délai global des téléchargements : `seconds` secondes à partir de maintenant (0 ou négatif : déjà échu — un téléchargement
    saute, seuls le cache et les erreurs répondent) ; None : sans limite."""
    global _deadline
    _deadline = None if seconds is None else time.monotonic() + max(0.0, seconds)


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
    _last.clear(), _crawl.clear(), _robots.clear(), _proxy_down.clear(), _dns_ok.clear(), _host_fails.clear()


def require_proxy(domain):
    """`domain` ne part jamais en direct : sans proxy (PROXIES), rien n'est envoyé (STRICT)."""
    STRICT.add(domain.lower())


def _phost(proxy):
    u = urlparse(proxy)
    return f"{u.hostname}:{u.port or 80}"


def _dns(host):
    """L'adresse de `host` se trouve-t-elle en DNS_WAIT secondes ? (dans un fil : getaddrinfo n'a pas de délai)"""
    box = {}

    def run():
        try:
            socket.getaddrinfo(host, None)
            box["ok"] = True
        except OSError:
            box["ok"] = False
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(DNS_WAIT)
    return bool(box.get("ok"))


def proxy_alive(proxy):
    """Le proxy `proxy` peut-il être essayé ? Faux s'il a été trouvé injoignable il y a moins de PROXY_DOWN_S s, ou si son adresse ne se trouve pas
    en DNS_WAIT s (alors tenu pour injoignable). Jamais de vérification DNS dans les tests (C.SANDBOX : proxys factices)."""
    t = _proxy_down.get(_phost(proxy))
    if t is not None and time.monotonic() < t:
        return False
    host = urlparse(proxy).hostname
    if not host or C.SANDBOX or time.monotonic() - _dns_ok.get(host, -1e18) < DNS_OK_S:
        return True
    if _dns(host):
        _dns_ok[host] = time.monotonic()
        return True
    _proxy_down[_phost(proxy)] = time.monotonic() + PROXY_DOWN_S
    return False


PROXY_REFUSED = {"407": "proxy : mot de passe refusé (407)", "402": "proxy : forfait épuisé (402)"}


def proxy_refusal(e):
    """Le proxy refuse-t-il de servir (mot de passe refusé 407, forfait Webshare épuisé 402) ? -> texte court (sans l'adresse), sinon None."""
    if not isinstance(e, requests.exceptions.ProxyError):
        return None
    m = re.search(r"Tunnel connection failed: (\d{3})", str(e))
    return PROXY_REFUSED.get(m.group(1)) if m else None


HOST_DOWN = ("NewConnectionError", "Failed to establish a new connection", "Connection refused", "NameResolution", "getaddrinfo",
             "Name or service not known")


def _host_unreachable(e):
    """L'échec dit-il que le SERVEUR du proxy est injoignable (connexion impossible ou trop lente, adresse introuvable) ? Une coupure en cours de
    route (« RemoteDisconnected »), un tunnel refusé par la sortie Webshare (« Tunnel connection failed: 502 ») : non, une autre session peut marcher."""
    return isinstance(e, requests.ConnectTimeout) or (isinstance(e, requests.exceptions.ProxyError) and any(k in str(e) for k in HOST_DOWN))


def why_net(e):
    """Précision d'un échec réseau, sans adresse ni mot de passe : « coupure du proxy », « tunnel 502 », « connexion au proxy impossible »… ; ''."""
    s = str(e)
    m = re.search(r"Tunnel connection failed: (\d{3})", s)
    if m:
        return f"tunnel {m.group(1)}"
    for keys, txt in ((("NameResolution", "getaddrinfo", "Name or service not known"), "adresse du proxy introuvable"),
                      (("NewConnectionError", "Failed to establish a new connection", "Connection refused"), "connexion au proxy impossible"),
                      (("RemoteDisconnected", "Connection aborted", "Connection reset"), "connexion coupée en route"),
                      (("timed out",), "délai dépassé")):
        if any(k in s for k in keys):
            return txt
    return ""


def _proxy_failed(proxy, e):
    """Échec par le proxy. Refus du proxy (mot de passe, forfait) : tenu pour en panne tout de suite (aucune session ne marchera). Serveur du proxy
    injoignable : tenu pour en panne PROXY_DOWN_S s seulement au HOST_FAILS-ième échec en HOST_FAIL_WINDOW s (une coupure isolée ne bloque plus
    rien). Session Webshare en panne, coupure en route : jamais (une autre session peut marcher : xbet)."""
    host, now = _phost(proxy), time.monotonic()
    if proxy_refusal(e):
        _proxy_down[host] = now + PROXY_DOWN_S
        return
    if not _host_unreachable(e):
        return
    recent = [t for t in _host_fails.get(host, ()) if now - t < HOST_FAIL_WINDOW] + [now]
    if len(recent) >= HOST_FAILS:
        _proxy_down[host], recent = now + PROXY_DOWN_S, []
    _host_fails[host] = recent


def _no_direct(domain, proxy):
    """Raison de NE PAS envoyer une requête vers `domain` (proxy absent pour un domaine STRICT, proxy injoignable) ; None sinon."""
    if not proxy:
        return NO_PROXY if domain in STRICT and not C.SANDBOX else None
    if not proxy_alive(proxy):
        return f"proxy injoignable (ProxyError), nouvel essai dans {PROXY_DOWN_S // 60} min"
    return None


def cause(error, who="365scores"):
    """Cause d'un échec de lecture en mots simples, pour un avertissement : « le proxy ne répond pas », « 365scores en panne (erreur 504) »…"""
    e = str(error or "")
    if NO_PROXY in e or "proxy introuvable" in e:
        return "proxy absent : rien n'est lu (jamais en direct)"
    if "mot de passe refusé" in e:
        return "le proxy refuse le mot de passe (vérifie XBET_PROXY)"
    if "forfait épuisé" in e:
        return "forfait du proxy Webshare épuisé"
    if any(k in e for k in ("proxy injoignable", "ProxyError", "ConnectTimeout")):
        return "le proxy ne répond pas"
    m = re.search(r"HTTP (\d{3})", e)
    if m and int(m.group(1)) >= 500:
        return f"{who} en panne (erreur {m.group(1)})"
    if m and m.group(1) in ("403", "429"):
        return f"{who} refuse l'accès (erreur {m.group(1)})"
    if "ReadTimeout" in e or "Timeout" in e or "délai" in e:
        return f"{who} trop lent (délai dépassé)"
    if any(k in e for k in ("ConnectionError", "SSLError", "réseau indisponible")):
        return "pas de connexion internet (ou proxy injoignable)"
    return f"{who} ne répond pas"


def env(name, default):
    """Réglage local d'un module (config.py n'est pas touché) : la variable d'environnement `name` lue comme le type de `default`, sinon `default`."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return type(default)(float(raw)) if isinstance(default, int) and not isinstance(default, bool) else type(default)(raw)
    except (TypeError, ValueError):
        return default


_CREDS = re.compile(r"[^\s/@:'\"<>()]+:[^\s/@'\"<>()]*@")      # « user-GB-20:mot-de-passe@ » (avec ou sans « http:// » devant)


def scrub(text):
    """`text` sans identifiants de proxy : tout « utilisateur:mot-de-passe@ » devient « ***@ », et une adresse de proxy connue (PROXIES) disparaît
    entière. À utiliser pour tout message d'erreur réseau qui part dans un journal ou sur Telegram."""
    out = str(text)
    for p in set(PROXIES.values()):
        if p:
            out = out.replace(p, "***")
    return _CREDS.sub("***@", out)


def err(e, limit=200):
    """« TypeError: message » d'une exception, sans identifiants de proxy (scrub) et coupé à `limit` caractères."""
    msg = scrub(e).strip()
    return f"{type(e).__name__}: {msg[:limit]}" if msg else type(e).__name__


NOT_THE_GAME = ("délai dépassé", "hors ligne", "robots.txt", "source bloquée", "bloqu", "accès refusé", "anti-robot")      # échecs qui ne disent rien de l'adresse demandée


def failure_kind(error, status=None):
    """Nature d'un échec de téléchargement pour une file d'attente : "skip" (délai du passage épuisé, hors ligne, source bloquée : rien à retenir),
    "systemic" (réseau, 5xx : peut être une panne générale, à ne retenir que si le reste du passage a marché) ou "specific" (le serveur a répondu
    par une erreur 4xx ou une réponse inutilisable : cette adresse-là est en cause)."""
    msg = str(error or "").lower()
    if any(k in msg for k in NOT_THE_GAME):
        return "skip"
    if status is None:
        m = re.match(r"http (\d{3})", msg)
        status = int(m.group(1)) if m else None
    if status is not None and 400 <= status < 500:
        return "specific"
    if status is not None and status < 400:
        return "specific"                              # réponse reçue mais inutilisable (appelant : JSON illisible...)
    return "systemic"


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
            tmp = f"{base}{ext}.{os.getpid()}.tmp"      # qualifié par le PID : deux passages sur la même clé ne s'écrasent pas l'un l'autre
            with open(tmp, mode) as f:
                f.write(data)
            os.replace(tmp, base + ext)
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
        delay = delay_for(domain)
        gap = min(delay, delay - (state.clock() - t))      # horloge reculée (changement d'heure, NTP) : jamais plus d'attente que l'écart normal
        if gap > 0:
            _sleep(gap)


def _request(url, headers=None):
    domain = urlparse(url).netloc.lower()
    h = {"User-Agent": UA, "Accept": "*/*"} | (headers or {})
    r, err = None, None
    for attempt in (0, 1):
        proxy = PROXIES.get(domain)
        stop = _no_direct(domain, proxy)
        if stop:                                    # jamais en direct pour 1xBet / 365scores ; proxy injoignable : on n'attend pas
            return None, f"{err} ; {stop}" if err else stop        # la 1re cause reste dite
        _wait(domain)
        try:
            if proxy:
                s = _sessions.get(domain) or _sessions.setdefault(domain, requests.Session())
                r = s.get(url, headers=h, timeout=TIMEOUT, proxies={"http": proxy, "https": proxy})
            else:
                r = requests.get(url, headers=h, timeout=TIMEOUT)
            _count(domain, r)
        except requests.RequestException as e:     # le message ne cite jamais le proxy (il contient le mot de passe)
            refused, detail = proxy_refusal(e), why_net(e)
            r, err = None, refused or f"réseau indisponible ({type(e).__name__}{', ' + detail if detail else ''})"
            if refused:                             # gardé pour le status et le message du matin (state « proxy »)
                with contextlib.suppress(Exception):
                    state._put("proxy", refused=refused, refused_at=state._iso(state.now()))
            if proxy:
                _proxy_failed(proxy, e)
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


SOURCES = {}              # domaine -> nom de la source (state.block) : post() applique les mêmes blocages que fetch() (xbet.py enregistre 1xbet.ci)


def register_source(domain, source):
    SOURCES[domain.lower()] = source


def post(url, body, headers=None, timeout=TIMEOUT):
    """POST JSON (ticket enregistré chez le bookmaker...) : comme _request mais en écriture — ni cache, ni robots.txt, ni
    conditionnel : une écriture n'est jamais rejouée depuis le cache, ni renvoyée si elle a pu arriver (délai de lecture, 5xx) :
    un seul nouvel essai, et seulement quand la connexion n'a pas pu s'établir (_not_sent). Même session proxy par domaine et même
    compteur WIRE. Retourne un Result : .ok si une réponse HTTP exploitable a été obtenue, .content brut, .status le code HTTP, .error sinon.
    Mêmes blocages que fetch() pour la source du domaine (register_source) : 403 / 429, Cloudflare ou page de vérification coupent la source
    24 h (state.block) et le Result porte blocked=True, sans contenu ; une source déjà coupée n'est plus appelée du tout."""
    domain = urlparse(url).netloc.lower()
    source = SOURCES.get(domain)
    if source and state.is_blocked(source):
        return Result(url, None, error=state.health(source)["last_error"] or "source bloquée", blocked=True)
    h = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"} | (headers or {})
    r, err = None, None
    for attempt in (0, 1):
        proxy = PROXIES.get(domain)
        stop = _no_direct(domain, proxy)
        if stop:                                    # jamais en direct pour 1xBet / 365scores ; proxy injoignable : on n'attend pas
            return Result(url, None, error=stop)
        _wait(domain)
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
            if proxy:
                _proxy_failed(proxy, e)
            stale = _sessions.pop(domain, None)    # connexion par le proxy cassée : le nouvel essai en ouvre une neuve
            if stale:
                stale.close()
        _last[domain] = state.clock()
        if r is not None and r.status_code < 500:
            return _post_result(url, r, source)
        if r is not None:
            err = f"HTTP {r.status_code}"           # le serveur a reçu la requête : jamais renvoyée
        if attempt == 0 and retry:
            continue
        break
    return Result(url, None, getattr(r, "status_code", None), error=err)


def _post_result(url, r, source):
    """Réponse HTTP (< 500) d'un POST -> Result, avec les règles de blocage de fetch() quand la source du domaine est connue."""
    code = r.status_code
    body = getattr(r, "content", None) or b""
    h = getattr(r, "headers", None) or {}
    if source:
        if any(str(k).lower() == "cf-mitigated" for k in h):
            state.block(source, f"HTTP {code}, Cloudflare", cloudflare=True)
            return Result(url, None, code, blocked=True, error="Cloudflare bloque les robots")
        if code in (403, 429):
            state.block(source, f"HTTP {code}")
            return Result(url, None, code, blocked=True, error=f"HTTP {code} : accès refusé")
        if body and not body.lstrip().startswith((b"{", b"[")) and is_challenge(body):
            state.block(source, "page de vérification anti-robot")
            return Result(url, None, code, blocked=True, error="page de vérification anti-robot")
    return Result(url, body, code, error=f"HTTP {code}" if code >= 400 else None)


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
        if _deadline is not None and time.monotonic() > _deadline:      # la lecture de robots.txt (jusqu'à 2 essais) a pu épuiser le délai
            return _fail(url, cached, "délai dépassé (robots.txt), téléchargement reporté au prochain passage")
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
    if code == 304:
        if not cached:                              # 304 sans copie locale : rien d'utilisable, jamais caché comme un succès
            return _fail(url, None, "HTTP 304 sans copie en cache", status=304)
        meta = cached[0] | {"fetched_at": state.clock()}
        _write(key, meta, cached[1])
        return Result(url, cached[1], 200, from_cache=True)
    h = getattr(r, "headers", None) or {}
    if any(str(k).lower() == "cf-mitigated" for k in h):
        state.block(source, f"HTTP {code}, Cloudflare", cloudflare=True)
        return _fail(url, cached, "Cloudflare bloque les robots", blocked=True, status=code)
    if code in (403, 429):
        state.block(source, f"HTTP {code}", hours=state.RATE_HOURS if code == 429 else None)     # 429 : trop de requêtes, pause d'une heure
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
