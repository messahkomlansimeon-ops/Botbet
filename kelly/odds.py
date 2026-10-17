"""Cotes (The Odds API) et construction des sélections."""
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from . import budget, cache, catalogue, config as C, fmt, learn


def sport_group(sport_key):
    return sport_key.split("_", 1)[0]


# Derniers crédits connus (en-têtes x-requests-remaining / x-requests-used de chaque réponse).
CREDITS = {"remaining": None, "used": None}
_warned = set()


class OddsError(Exception):
    """Erreur The Odds API ou réseau, sans jamais exposer la clé."""

    def __init__(self, msg, status=None, code=None):
        super().__init__(msg)
        self.status, self.code = status, code


class BudgetError(OddsError):
    """Appel payant refusé par le budget (kelly/budget.py) : à sauter, jamais fatal."""


def _scrub(text):
    key = os.environ.get("ODDS_API_KEY")
    return str(text).replace(key, "***") if key else str(text)


def _note_credits(r):
    for field, header in (("remaining", "x-requests-remaining"), ("used", "x-requests-used")):
        try:
            CREDITS[field] = int(float(r.headers[header]))
        except (KeyError, ValueError, TypeError):
            pass
    if CREDITS["remaining"] is not None:
        print(f"[odds] crédits restants : {CREDITS['remaining']} (utilisés : {CREDITS['used']})", file=sys.stderr)


def low_credits():
    """Vrai si les crédits restants (derniers connus) sont sous C.MIN_CREDITS."""
    rem = CREDITS["remaining"]
    return rem is not None and rem < C.MIN_CREDITS


def credits_ok(what):
    """Garde-fou des appels optionnels : False (et avertissement, une fois) si crédits bas."""
    if not low_credits():
        return True
    if what not in _warned:
        _warned.add(what)
        print(f"⚠️ [odds] crédits bas ({CREDITS['remaining']} < {C.MIN_CREDITS}) : {what} ignoré(s).",
              file=sys.stderr)
    return False


def cost(path, params):
    """Crédits facturés par The Odds API pour cet appel (0 = gratuit) : /odds coûte
    (nb de marchés) x (nb de régions), /scores 1 (2 avec daysFrom), le reste est gratuit."""
    if path.endswith("/scores"):
        return 2 if params.get("daysFrom") else 1
    if path.endswith("/odds"):
        n = lambda v, d: max(1, len([x for x in str(v if v is not None else d).split(",") if x.strip()]))
        return n(params.get("markets"), "h2h") * n(params.get("regions"), "eu")
    return 0


def get(path, use=None, **params):
    """GET The Odds API v4 -> JSON. Lève OddsError (message sans clé) ; 1 nouvel essai sur 429.
    `use` ("daily", "closing"...) : usage budgété d'un appel payant. Refusé par le budget -> BudgetError
    (rien n'est envoyé) ; réussi -> crédits décomptés dans data/budget.json. Sans `use` : pas de budget
    (appels gratuits : /sports, /events)."""
    key = os.environ.get("ODDS_API_KEY")
    if cache.OFFLINE:
        raise OddsError("réseau interdit hors des passages programmés (commandes Telegram)", code="OFFLINE")
    if not key:
        raise OddsError("ODDS_API_KEY absente", code="NO_KEY")
    n = cost(path, params)
    if n and not use:                               # un appel payant sans usage budgété passerait outre le budget
        raise BudgetError(f"appel payant ({n} crédit(s)) sans usage budgété : refusé", code="BUDGET")
    why = budget.why_not(n, use) if n else None
    if why:
        raise BudgetError(f"budget « {use} » : {n} crédit(s) refusé(s), {why}", code="BUDGET")
    params["apiKey"] = key
    for attempt in (0, 1):
        try:
            r = requests.get(f"{C.ODDS_API}{path}", params=params, timeout=30)
        except requests.RequestException as e:
            raise OddsError(f"réseau : {_scrub(e)}") from None
        _note_credits(r)
        if r.status_code == 429 and attempt == 0:      # limite de fréquence : on patiente un peu
            time.sleep(2)
            continue
        break
    if not r.ok:
        try:
            body = r.json()
            msg, code = body.get("message", ""), body.get("error_code")
        except ValueError:
            msg, code = r.text[:200], None
        if code == "OUT_OF_USAGE_CREDITS":
            CREDITS["remaining"] = 0
        budget.sync()
        raise OddsError(f"HTTP {r.status_code} {code or ''} {_scrub(msg)}".strip(), r.status_code, code)
    budget.spend(n, use) if n else budget.sync()
    cache.miss("odds" if n else "events", n or 1)
    return r.json()


def select_sports(sports, limit=True):
    """Sports configurés parmi la liste de /sports, dans l'ordre de C.SPORTS (hors outrights).
    Entrée finissant par "_" = préfixe, sinon clé exacte. `limit` : coupe à C.MAX_SPORTS."""
    live = [s for s in sports if not s.get("has_outrights")]
    out, seen = [], set()
    for pat in C.SPORTS:
        for s in live:
            if s["key"] not in seen and (s["key"].startswith(pat) if pat.endswith("_") else s["key"] == pat):
                seen.add(s["key"])
                out.append(s)
    return out[:C.MAX_SPORTS] if limit else out


def cached_get(key, hours, call, src="events", saved=1, frm=None):
    """`call()` (un appel d'API) avec cache disque : la réponse du cache est relue si elle a moins de `hours` heures (et, si
    `frm` est donné, si elle couvrait au moins cette période). Compteurs : relu = `saved` crédits / requêtes évités."""
    got = cache.load(key, hours)
    if got is not None and (frm is None or got[1].get("frm", "~") <= frm):
        cache.hit(src, saved)
        return got[0]
    data = call()
    cache.save(key, data, frm=frm)
    return data


def list_sports(limit=True):
    """Sports en saison : /sports gratuit (0 crédit), mis en cache EVENTS_CACHE_HOURS (tous les modes le relisent)."""
    return select_sports(cached_get("sports", C.EVENTS_CACHE_HOURS, lambda: get("/sports")), limit)


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_end(day):
    """Fin (exclue) du « jour Kelly » `day` : le lendemain à C.DAY_END_HOUR h UTC. Le run du matin (C.DAY_END_HOUR h UTC) couvre ainsi
    les matchs du soir des Amériques (00:30 UTC = 21:30 locale), qui sinon tomberaient entre deux runs."""
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1, hours=C.DAY_END_HOUR)


def in_day(start, day, after=None):
    """Vrai si le coup d'envoi `start` (datetime UTC) est dans le jour Kelly `day` : de 00:00 UTC (ou `after`, matchs déjà
    commencés exclus) jusqu'au lendemain C.DAY_END_HOUR h UTC (exclu)."""
    lo = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start >= lo and start < day_end(day) and not (after and start <= after)


def day_window(day, after=None):
    """Fenêtre commenceTimeFrom/To du jour Kelly `day` (début = `after` si plus tard : matchs à venir ; fin = lendemain
    C.DAY_END_HOUR h UTC, voir day_end ; l'API compte la borne incluse, d'où la seconde de moins)."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    if after is not None and after > start:
        start = after
    return {"commenceTimeFrom": _iso(start), "commenceTimeTo": _iso(day_end(day) - timedelta(seconds=1))}


def league_events(sport_key, window):
    """Matchs d'une ligue dans la fenêtre : endpoint GRATUIT /events (0 crédit), mis en cache EVENTS_CACHE_HOURS (clé :
    ligue + fin de fenêtre ; une fenêtre qui commence plus tard est servie par filtrage du cache)."""
    frm = window["commenceTimeFrom"]
    evs = cached_get(f"events|{sport_key}|{window['commenceTimeTo']}", C.EVENTS_CACHE_HOURS,
                     lambda: get(f"/sports/{sport_key}/events", dateFormat="iso", **window) or [], frm=frm)
    return [e for e in evs if e.get("commence_time", frm) >= frm]


def scan_today(day, after=None, sports=None, seconds=None):
    """Relevé GRATUIT (0 crédit) des matchs du jour Kelly `day` (voir day_window) dans toutes les ligues suivies et en saison : /sports puis
    /events de chaque ligue, dans l'ordre de priorité de C.SPORTS (le plus important d'abord), au plus `seconds`
    (C.SCAN_SECONDS) pour tenir le passage du matin. Une ligue en erreur est sautée.
    -> {day, sports: [clés en saison], leagues: {clé: [matchs]}, complete, error, failed}. Une ligue dont /events échoue (panne, réseau, quota)
    n'est PLUS silencieuse : sa clé va dans `failed` et `complete` passe à False (le tour du monde dit « relevé partiel », jamais « aucun
    match » pour une ligue qu'on n'a pas pu interroger) ; si AUCUNE ligue n'a répondu, `error` le dit comme une panne de la liste des sports."""
    out = {"day": str(day), "sports": [], "leagues": {}, "complete": True, "error": None, "failed": []}
    window = day_window(day, after)
    try:
        sports = list_sports(limit=False) if sports is None else sports
    except OddsError as e:
        out["error"] = str(e)
        print(f"[odds] liste des sports indisponible : {e}", file=sys.stderr)
        return out
    out["sports"] = [s["key"] for s in sports]
    limit, t0, tried, first_err = C.SCAN_SECONDS if seconds is None else seconds, time.monotonic(), 0, None
    for i, s in enumerate(sports):
        if time.monotonic() - t0 > limit:
            out["complete"] = False
            print(f"[odds] relevé des matchs coupé après {limit} s : {len(sports) - i} ligue(s) non relevée(s).", file=sys.stderr)
            break
        tried += 1
        try:
            evs = league_events(s["key"], window)
        except Exception as e:                          # noqa: BLE001 - une ligue ne tue pas le run
            print(f"[{s['key']}] matchs du jour indisponibles : {e}", file=sys.stderr)
            out["failed"].append(s["key"])
            out["complete"] = False
            first_err = first_err or str(e)
            continue
        if evs:
            out["leagues"][s["key"]] = [dict(ev, sport_key=s["key"]) for ev in evs]
    if tried and len(out["failed"]) == tried:           # aucune ligue n'a répondu : /events est en panne, pas « aucun match aujourd'hui »
        out["error"] = f"/events injoignable pour les {tried} ligues ({(first_err or '?')[:100]})"
        print(f"[odds] {out['error']}", file=sys.stderr)
    elif out["failed"]:
        print(f"[odds] relevé partiel : {len(out['failed'])} ligue(s) sur {tried} en erreur.", file=sys.stderr)
    return out


def scan_ahead(day, sports, days=7, seconds=None):
    """Matchs des `days` jours suivant le jour Kelly `day` (gratuit), pour dire quand les ligues reprennent un jour sans match :
    {« AAAA-MM-JJ »: {clé de ligue: nombre de matchs}}. Même limite de durée que scan_today."""
    window = {"commenceTimeFrom": _iso(day_end(day)),            # là où s'arrête le jour Kelly (pas de doublon avec scan_today)
              "commenceTimeTo": f"{day + timedelta(days=days)}T23:59:59Z"}
    out, limit, t0 = {}, C.SCAN_SECONDS if seconds is None else seconds, time.monotonic()
    for key in sports:
        if time.monotonic() - t0 > limit:
            break
        try:
            evs = league_events(key, window)
        except Exception as e:                          # noqa: BLE001
            print(f"[{key}] prochains matchs indisponibles : {e}", file=sys.stderr)
            continue
        for ev in evs:
            d = out.setdefault(ev["commence_time"][:10], {})
            d[key] = d.get(key, 0) + 1
    return out


def match_numbers(home, away, sport_key, referee=None, on=None):
    """Chiffres d'avant-match gratuits d'un match (sources.match_features) ; {"sources": []} si rien. Les sélections ne
    sont pas dans les profils de clubs : un nom proche d'un club serait une fausse piste. `referee` : arbitre désigné (stats.referee_for), qui
    corrige cartons et fautes (sources.features, strength.referee_factor) ; inconnu = rien de changé."""
    if catalogue.region(sport_key) == "monde":
        return {"sources": []}
    try:
        from .sources import features             # import tardif : sources importe stats
        return features.match_features(home, away, sport_key, referee=referee, on=on)
    except Exception:                                # noqa: BLE001 - stats facultatives
        return {"sources": []}


def model_covered(ev):
    """Vrai si Kelly a un modèle corners/cartons pour ce match : profils football-data des deux équipes (c'est là que
    l'écart modèle-ligne existe, donc là que les marchés détaillés valent leurs crédits)."""
    f = match_numbers(ev["home_team"], ev["away_team"], ev.get("sport_key", ""))
    # un profil API-Football encore « utilisable » (3 à 5 matchs, features.aps_quality) ne vaut pas des crédits : il faut « bon » (6 matchs)
    thin = f.get("aps_q", 1.0) < C.FAIR_COMBO_MIN_Q
    return any(k in f and not (thin and k in (f.get("aps_keys") or ())) for k in ("exp_corners", "exp_cards"))


def covered_leagues(scan, sample=6):
    """Ligues du relevé dont Kelly a des profils d'équipe (football-data ou API-Football) pour les premiers matchs : on les paie
    avant les autres à rang égal (kelly/catalogue.prioritise), leurs corners et cartons ayant un modèle."""
    out = set()
    for key, evs in (scan.get("leagues") or {}).items():
        try:
            if any(model_covered(dict(ev, sport_key=key)) for ev in evs[:sample] if ev.get("home_team") and ev.get("away_team")):
                out.add(key)
        except Exception:                                # noqa: BLE001 - le classement ne doit jamais casser l'achat
            pass
    return out


def _bulk_key(day, key, markets):
    return f"odds|{day}|{key}|{markets}"


def read_stamp(ts=None):
    """Heure ISO (UTC, à la seconde) d'une lecture de The Odds API : `ts` = instant (secondes, comme cache.clock) ; maintenant sans argument.
    Posée sur les matchs lus (ev["read_at"], ev["read_m"]) puis sur chaque pari (c["sharp_at"], voir build_picks) : la fraîcheur d'une proba sharp
    (sharpgap, ensemble.sharp_for) est celle de la VRAIE lecture, pas celle de l'écriture de today.json."""
    return datetime.fromtimestamp(cache.clock() if ts is None else ts, tz=timezone.utc).isoformat(timespec="seconds")


def _cached_bulk(day, key, markets, frm):
    """Réponse payante du jour déjà en cache (relevé du matin) : relue sans crédit si elle couvre la période, sinon None. Les matchs portent l'heure
    de leur VRAIE lecture (ev["read_at"] : celle de l'écriture du cache)."""
    got = cache.load(_bulk_key(day, key, markets), C.ODDS_CACHE_HOURS)
    if got is None or got[1].get("frm", "~") > frm:
        return None
    stamp = read_stamp(got[1].get("fetched_at")) if got[1].get("fetched_at") else None
    out = [e for e in got[0] if e.get("commence_time", frm) >= frm]
    if stamp:
        for e in out:
            e.setdefault("read_at", stamp)
    return out


def _prepaid_today(day, after, scan=None):
    """Vrai si au moins une ligue a déjà été payée aujourd'hui (cache) : même sans budget restant, on peut la relire."""
    frm = day_window(day, after)["commenceTimeFrom"]
    keys = list(scan["leagues"]) if scan and scan.get("leagues") else [k for k in C.SPORTS if not k.endswith("_")]
    return any(_cached_bulk(day, k, C.FEATURED.get(sport_group(k), "h2h"), frm) is not None for k in keys)


def fetch_events(day, props=True, optional=False, use="daily", after=None, scan=None):
    """Matchs du jour Kelly `day` (de 00:00 UTC ou `after` au lendemain C.DAY_END_HOUR h UTC, voir day_window) + marchés détaillés, dans la limite du budget `use`.
    1. relevé gratuit (scan_today ; `scan` : relevé déjà fait) : seules les ligues qui ont des matchs sont payées,
       dans l'ordre de catalogue.prioritise (région, ligues majeures, nombre de matchs), au plus C.MAX_SPORTS ;
    2. marchés vedettes en bulk (1 appel par ligue ; au plus C.FEATURED_SHARE du budget quand des marchés détaillés
       suivront, sinon tout le budget : pas de profil de stats, pas de crédit dépensé en corners/cartons) ;
    3. marchés détaillés, par ordre de priorité de C.PROP_MARKETS (corners, cartons... d'abord), seulement sur les
       matchs les mieux cotés qui ont un profil football-data (model_covered), tant que le budget le permet.
    `optional` : balayage facultatif (alertes, relevé du soir), sauté si crédits bas ; `after` : n'achète
    que les matchs à venir. Une ligue, un match ou un marché refusé ou en erreur est sauté (log stderr).
    Les clés des ligues payées sont ajoutées à scan["paid"]."""
    events = []
    if budget.left(use) <= 0 and not (C.BUDGET_SHARES.get(use) and _prepaid_today(day, after, scan)):
        why = f"allocation du jour : {budget.allowance()} crédits, déjà utilisée" if C.BUDGET_SHARES.get(use) else "aucune part dans BUDGET_SHARES"
        print(f"[budget] « {use} » n'a pas de budget aujourd'hui ({why}) : aucun appel payant.", file=sys.stderr)
        return events
    if optional and not credits_ok("balayage large"):
        return events
    window = day_window(day, after)
    scan = scan if scan is not None else scan_today(day, after)
    scan.setdefault("paid", [])
    if scan.get("error"):
        return events
    counts = {k: len(v) for k, v in scan["leagues"].items()}
    leagues = catalogue.prioritise(counts, C.SPORTS, covered_leagues(scan))[:C.MAX_SPORTS]
    print(f"[odds] ligues avec matchs : {', '.join(leagues) or 'aucune'} "
          f"(budget « {use} » : {budget.left(use)} crédits)", file=sys.stderr)
    used = 0
    frm = window["commenceTimeFrom"]
    cached_bulk = lambda key, markets: _cached_bulk(day, key, markets, frm)         # noqa: E731

    def buy(keys, cap):
        """Marchés vedettes en bulk (1 appel par ligue, ou relecture du cache du jour) tant que `cap` crédits ne sont pas
        dépassés. -> True si le budget lui-même a refusé (inutile d'insister)."""
        nonlocal used
        for key in keys:
            markets = C.FEATURED.get(sport_group(key), "h2h")
            n = len(markets.split(","))
            if used and used + n > cap:                 # au moins une ligue ; le reste va aux marchés détaillés
                print(f"[budget] part des marchés vedettes atteinte ({used}/{cap}) : {key} ignorée.", file=sys.stderr)
                return False
            data = cached_bulk(key, markets)
            if data is not None:                        # déjà payé aujourd'hui : zéro crédit
                cache.hit("odds", n)
                for ev in data:
                    ev["sport_key"] = key
                    ev.setdefault("bookmakers", [])
                    events.append(ev)
                used += n
                scan["paid"].append(key)
                continue
            why = budget.why_not(n, use)
            if why:
                print(f"[budget] {key} ignorée ({why}).", file=sys.stderr)
                return True
            try:
                raw = get(f"/sports/{key}/odds", use=use, regions="eu", oddsFormat="decimal", markets=markets, **window) or []
                cache.save(_bulk_key(day, key, markets), raw, frm=frm)
                stamp = read_stamp()                    # lecture réelle, posée APRÈS l'écriture du cache : le cache ne garde pas l'heure ici
                for ev in raw:
                    ev["sport_key"] = key
                    ev["read_at"] = stamp
                    ev.setdefault("bookmakers", [])
                    events.append(ev)
                used += n
                scan["paid"].append(key)
            except Exception as e:                      # noqa: BLE001
                print(f"[{key}] ignoré : {e}", file=sys.stderr)
        return False
    # Les ligues déjà payées aujourd'hui (cache) ne comptent pas contre le budget restant : on les ajoute à `left`
    # pour qu'une relance fasse exactement les mêmes choix que le premier passage (et ne dépense rien de plus).
    prepaid = sum(len(C.FEATURED.get(sport_group(k), "h2h").split(",")) for k in leagues
                  if cached_bulk(k, C.FEATURED.get(sport_group(k), "h2h")) is not None)
    left = budget.left(use) + prepaid
    refused = buy(leagues, int(left * C.FEATURED_SHARE + 1e-9) if props else left)
    # Aucun match acheté n'a de profil de stats : les marchés détaillés (corners, cartons) n'auraient pas d'écart modèle-ligne
    # à exploiter. La moitié mise de côté pour eux va alors à d'autres ligues, une par une, jusqu'à en trouver une dont les
    # matchs ont un profil : le reste du budget sert à ses corners et cartons.
    if props and not refused:
        for key in [k for k in leagues if k not in scan["paid"]]:
            if refused or any(model_covered(ev) for ev in events):
                break
            refused = buy([key], left)
    if not props:
        return events
    events.sort(key=lambda e: -len(e["bookmakers"]))
    top = [ev for ev in events if model_covered(ev)][:C.PROP_EVENTS]
    lists = [C.PROP_MARKETS.get(sport_group(ev["sport_key"]), []) for ev in top]
    # marché par marché (priorité), sur tous les matchs : corners partout avant les cartons.
    jobs = [(ev, ms[i]) for i in range(max(map(len, lists), default=0)) for ev, ms in zip(top, lists) if i < len(ms)]
    for k, (ev, market) in enumerate(jobs):
        ck = f"odds|{day}|{ev['sport_key']}|{ev['id']}|{market}"
        got = cache.load(ck, C.ODDS_CACHE_HOURS)
        stamp = None
        if got is not None:                             # déjà demandé aujourd'hui (même un refus 422) : zéro crédit
            cache.hit("odds", 1)
            data = got[0]
            stamp = read_stamp(got[1].get("fetched_at")) if got[1].get("fetched_at") else None
        else:
            why = budget.why_not(1, use)
            if why:
                print(f"[budget] {len(jobs) - k} marché(s) détaillé(s) ignoré(s) ({why}).", file=sys.stderr)
                break
            try:
                data = get(f"/sports/{ev['sport_key']}/events/{ev['id']}/odds", use=use, regions="eu",
                           markets=market, oddsFormat="decimal")
            except Exception as e:                      # noqa: BLE001 - 422 = marché inconnu pour ce sport/match
                print(f"[{ev['sport_key']}] {market} ignoré : {e}", file=sys.stderr)
                if getattr(e, "status", None) == 422:
                    cache.save(ck, {"bookmakers": []})
                continue
            cache.save(ck, data)
            stamp = read_stamp()
        if stamp:
            ev.setdefault("read_m", {})[market] = stamp      # heure de lecture de CE marché détaillé (sinon celle du relevé vedette, ev["read_at"])
        for bk in (data or {}).get("bookmakers", []):
            mine = next((b for b in ev["bookmakers"] if b["key"] == bk["key"]), None)
            if mine is None:
                ev["bookmakers"].append(bk)
            else:
                mine["markets"].extend(bk["markets"])
    return events


def fetch_scores(sport_key, days=3, use="daily"):
    """Scores terminés des `days` derniers jours (1 à 3, max de l'API ; 2 crédits, budgétés).
    `scores` vaut null tant que le match n'a pas commencé, sinon [{name, score}] (texte).
    Repli du règlement : le foot se règle gratuitement via API-Football (settle.py)."""
    d = min(max(int(days), 1), 3)
    return cached_get(f"scores|{sport_key}|{d}", 6, lambda: get(f"/sports/{sport_key}/scores", use=use, daysFrom=d,
                                                                 dateFormat="iso") or [], src="odds", saved=2)


def label(market, name, point, who, group):
    """Libellé du pari tel qu'on le lit chez le bookmaker (voir fmt.label)."""
    return fmt.label(market, name, point, who, group)


def league_name(ev):
    """Nom français de la ligue du catalogue (« Ligue des nations UEFA »), sinon le titre donné par l'API."""
    key = ev.get("sport_key", "")
    return catalogue.name(key) if catalogue.region(key) != catalogue.OTHER else ev.get("sport_title", "")


def market_weight(market):
    return C.PLAYER_WEIGHT if market.startswith("player_") else C.MARKET_WEIGHT.get(market, 1.2)


def few_books_factor(n):
    """Facteur (>= 1) qui durcit un seuil quand il y a peu de books : un écart y est plus souvent du bruit."""
    return 1 + C.FEW_BOOKS / max(1, n)


def disagree_threshold(n):
    """Écart (max - min des probas) / proba à partir duquel « les bookmakers ne sont pas d'accord » : plus haut avec peu
    de books (DISAGREE_BASE x facteur)."""
    return C.DISAGREE_BASE * few_books_factor(n)


def typical_price(prices):
    """(cote typique, book) d'une liste [(cote, book)] : le prix du (n // 4 + 1)-ième meilleur book, au moins le 2e (75e
    centile). Un seul prix aberrant (exchange sans commission, book en retard) ne la déplace pas ; au moins un book sur
    quatre la propose ou mieux. Voir config.TYPICAL_*."""
    ranked = sorted(prices, key=lambda x: -x[0])
    rank = min(len(ranked), max(C.TYPICAL_MIN_RANK, int(len(ranked) * C.TYPICAL_RANK_SHARE) + 1))
    return ranked[rank - 1]


def half_line(point):
    """Vrai pour une ligne en .5 (2,5 ; -1,5) : la seule que le règlement et les probas de Poisson gèrent. Les lignes
    entières (remboursement) et les quarts asiatiques (2,25 ; 2,75 : demi-gain ou demi-perte) sont écartés."""
    try:
        x = float(point) * 2
    except (TypeError, ValueError):
        return False
    return abs(x - round(x)) < 1e-9 and round(x) % 2 != 0


def complete(market, group, outs):
    """Vrai si le book affiche TOUTES les issues du groupe (même ligne, même joueur) : 3 pour le 1X2 du foot (avec Draw) et
    la double chance, 2 pour les autres marchés. Sinon la normalisation sans marge gonflerait les probas (> 1 en cumulé)."""
    names = {o["name"] for o in outs}
    if market == "double_chance" or "3_way" in market:
        need = 3
    elif market == "h2h":
        need = 3 if group == "soccer" or "Draw" in names else 2
    else:
        need = 2
    return len(outs) == need and len(names) == need and all(o.get("price") and o["price"] > 1 for o in outs)


def _shin_p(q, s, z):
    """p_i(z) = (sqrt(z^2 + 4(1-z) q^2 / S) - z) / (2(1-z)) (Štrumbelj 2014, package R « implied »), écrite sous la forme
    équivalente 2a / (sqrt(z^2 + 4(1-z)a) + z), a = q^2 / S : sans division par 1 - z ni soustraction de grands voisins."""
    a = q * q / s
    return 2 * a / (math.sqrt(z * z + 4 * (1 - z) * a) + z)


def shin_z(prices):
    """Part z (0 <= z < 1) de trades « d'initiés » estimée par Shin (1993) sur un marché de cotes décimales `prices`, ou 0.0
    s'il n'y a pas de marge (somme des 1/cote <= 1). z est la racine de sum(p_i(z)) = 1 (bissection : la somme décroît
    de sqrt(S) > 1 en z = 0 à sum(q_i^2) / S < 1 en z = 1)."""
    q = [1 / x for x in prices]
    s = sum(q)
    if s <= 1 + 1e-12:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if sum(_shin_p(x, s, mid) for x in q) > 1:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-14:
            break
    return (lo + hi) / 2


def shin_probs(prices):
    """Probabilités « vraies » d'un marché à n issues de cotes décimales `prices` (marge retirée par la méthode de Shin, 1993).
    Elles somment à 1. Avec q_i = 1/cote_i et S = sum q_i (S > 1 : marge), p_i = (sqrt(z^2 + 4(1-z) q_i^2 / S) - z) / (2(1-z)),
    z étant choisi pour que sum p_i = 1 (shin_z). Contrairement à la normalisation proportionnelle q_i / S, Shin retire
    plus de marge aux outsiders qu'aux favoris (biais favori-outsider : le public surmise les longshots), donc ses
    probas d'outsiders sont plus basses. Sans marge (S <= 1) : q_i / S. Valable à 2 issues comme à 3 ou plus."""
    q = [1 / x for x in prices]
    s = sum(q)
    if s <= 1 + 1e-12:
        return [x / s for x in q]
    z = shin_z(prices)
    ps = [_shin_p(x, s, z) for x in q]
    t = sum(ps)                                     # renormalise l'erreur résiduelle de la bissection (~1e-14)
    return [x / t for x in ps]


def consensus(entries):
    """Proba de consensus d'une issue à partir de [(clé du book, proba sans marge)], un élément par book.
    Si un book de référence (C.SHARP_BOOKS, Pinnacle d'abord) l'affiche : SHARP_WEIGHT x moyenne des books de référence +
    (1 - SHARP_WEIGHT) x moyenne des autres books (la moyenne des références seule s'il n'y a pas d'autre book).
    Sinon : moyenne de tous les books. -> (proba, source, nombre de books de référence) ; source « pinnacle » si Pinnacle
    en fait partie, « sharp » pour un autre book de référence, « consensus » sans référence."""
    sharp = [p for b, p in entries if b in C.SHARP_BOOKS]
    rest = [p for b, p in entries if b not in C.SHARP_BOOKS]
    if not sharp:
        return sum(rest) / len(rest), "consensus", 0
    ps = sum(sharp) / len(sharp)
    p = C.SHARP_WEIGHT * ps + (1 - C.SHARP_WEIGHT) * sum(rest) / len(rest) if rest else ps
    return p, "pinnacle" if any(b == "pinnacle" for b, _ in entries) else "sharp", len(sharp)


def _dc_parts(name, home, away):
    """Issues du 1X2 couvertes par une double chance (« Alpha or Draw » -> (Alpha, Draw)), ou None si le nom n'est pas lisible."""
    for a, b in ((home, "Draw"), (home, away), (away, "Draw")):
        if name in (f"{a} or {b}", f"{b} or {a}"):
            return a, b
    return None


def build_picks(events, day, after=None, history=None, model=None):
    """Toutes les sélections : proba de consensus, cote typique, valeur et flair.

    Proba de consensus : chaque book est dé-margé par la méthode de Shin (shin_probs), puis Pinnacle, s'il affiche toutes les
    issues, pèse C.SHARP_WEIGHT face à la moyenne des autres (voir consensus). c["prob_src"] dit laquelle a servi.

    La cote d'un pari est la cote TYPIQUE (75e centile des books, voir typical_price), pas le maximum : l'écart de valeur,
    les combinés et les mises la prennent comme référence. La meilleure cote reste dans c["best_odds"] (et c["book"]) pour
    la note « jusqu'à 4,60 chez Coolbet ».

    score = valeur (cote typique vs proba de consensus)
          + désaccord entre bookmakers (au prorata du nombre de books : peu de books = signal faible)
          + mouvement de la cote depuis le dernier relevé (argent des pros)
          x poids du marché (secondaires = mal pricés)
    Si un modèle appris est adopté (learn.train_clv), le score devient le CLV
    prédit, et la proba est calibrée par les résultats quand c'est actif.
    """
    now = datetime.now(timezone.utc)
    history = history or {}
    model = model or {}
    picks = []
    for ev in events:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if not in_day(start, day, after):                   # jour Kelly : jusqu'au lendemain C.DAY_END_HOUR h UTC
            continue
        group = sport_group(ev.get("sport_key", "soccer_"))
        probs, prices = {}, {}
        for bk in ev.get("bookmakers", []):
            for m in bk["markets"]:
                groups = {}
                for o in m["outcomes"]:
                    pt = o.get("point")
                    if pt is not None and not half_line(pt):
                        continue                        # ligne entière ou quart (Asie) : ni règlement ni Poisson ne les gèrent
                    if m["key"] == "spreads" and pt is not None:
                        pt = abs(pt)                    # handicap : -1.5 / +1.5 forment une paire
                    groups.setdefault((o.get("description"), pt), []).append(o)
                # double chance : 3 issues dont les probas somment à 2 (pas 1) ; Shin n'est défini que pour une somme de 1
                dc = m["key"] == "double_chance"
                for (who, _), outs in groups.items():
                    if not complete(m["key"], group, outs):
                        continue                        # un book qui n'affiche pas toutes les issues fausserait le sans-marge
                    if dc:                              # repli proportionnel de masse 2 (le consensus 1X2 le remplace, voir plus bas)
                        total = sum(1 / o["price"] for o in outs)
                        book_ps = [2 / o["price"] / total for o in outs]
                    else:
                        book_ps = shin_probs([o["price"] for o in outs])
                    for o, bp in zip(outs, book_ps):
                        k = (m["key"], who, o.get("point"), o["name"])
                        probs.setdefault(k, []).append((bk["key"].lower(), bp))
                        prices.setdefault(k, []).append((o["price"], bk.get("title", bk["key"])))
        cons = {k: consensus(es) for k, es in probs.items()}
        # Double chance : somme des deux issues du 1X2 de consensus (Pinnacle compris) quand les 3 issues du 1X2 ont assez de
        # books. Le 1X2 est le marché le plus liquide et le mieux dé-margé (Shin) ; la double chance des books mous, à marge
        # élevée et sans Pinnacle, n'apporte pas d'information propre. Sinon : consensus proportionnel de masse 2.
        h2h = {k[3]: cons[k] for k, es in probs.items()
               if k[0] == "h2h" and k[1] is None and k[2] is None and len(es) >= C.MIN_BOOKMAKERS}
        dc_from_h2h = set()                             # double chances dont la proba sharp vient du 1X2 : leur fraîcheur est celle du 1X2 (A1, audit du 17/10)
        if len(h2h) == 3 and {ev["home_team"], ev["away_team"], "Draw"} == set(h2h):
            for k in probs:
                parts = _dc_parts(k[3], ev["home_team"], ev["away_team"]) if k[0] == "double_chance" else None
                if parts:
                    cs = [h2h[x] for x in parts]
                    cons[k] = (sum(c[0] for c in cs), cs[0][1] if cs[0][1] == cs[1][1] else "consensus", min(c[2] for c in cs))
                    dc_from_h2h.add(k)
        for k, es in probs.items():
            ps = [p for _, p in es]
            if len(ps) < C.MIN_BOOKMAKERS:
                continue
            market, who, point, name = k
            p, src, n_sharp = cons[k]
            odds, _ = typical_price(prices[k])
            best, best_book = max(prices[k], key=lambda x: x[0])
            if not C.ODDS_RANGE[0] <= odds <= C.ODDS_RANGE[1]:
                continue
            key = f'{ev["id"]}|{market}|{who or ""}|{point if point is not None else ""}|{name}'
            edge = p * odds - 1
            spread = (max(ps) - min(ps)) / p
            prev = history.get(key)
            prev_p = prev.get("pb") if isinstance(prev, dict) else None      # « pb » seul : les anciens relevés (« p » mêlé au modèle) sont ignorés
            prev_src = prev.get("src") if isinstance(prev, dict) else None      # source changée (Pinnacle apparu/disparu) : pas un mouvement
            steam = (p - prev_p) / prev_p if prev_p and prev_src in (None, src) else 0.0
            weight = market_weight(market)
            signals = []
            n_books = len(ps)
            if edge > 0.03:
                signals.append(f"cote plus haute qu'ailleurs ({fmt.num(odds)} contre {fmt.num(1 / p)} en moyenne)")
            if spread > disagree_threshold(n_books):
                signals.append("les bookmakers ne sont pas d'accord entre eux")
            if steam > 0.04:
                signals.append("la cote baisse depuis le dernier relevé : les parieurs pros misent dessus")
            p_cal = learn.calibrated(model, market, p)
            c = {
                "key": key, "id": ev["id"], "sport_key": ev.get("sport_key", ""), "group": group,
                "event": f'{ev["home_team"]} - {ev["away_team"]}',
                "home": ev["home_team"], "away": ev["away_team"],
                "league": league_name(ev), "start": start.strftime("%H:%M"),
                "commence": ev["commence_time"],
                "market": market, "who": who, "point": point, "name": name,
                "pick": label(market, name, point, who, group),
                "prob": p_cal, "prob_book": p, "prob_src": src, "n_sharp": n_sharp, "odds": odds, "best_odds": best, "book": best_book,
                "edge": p_cal * odds - 1, "spread": spread, "steam": steam, "n_books": n_books, "weight": weight,
                "score": (edge + 0.5 * min(1.0, n_books / C.DISAGREE_FULL_BOOKS) * spread + 2 * steam) * weight
                         + (weight - 1) * 0.05,
                "why": ", ".join(signals),
            }
            read = (ev.get("read_m") or {}).get(market) or ev.get("read_at")      # VRAIE heure de lecture chez The Odds API (fraîcheur du sharp)
            if k in dc_from_h2h:                    # la proba sharp d'une double chance est la somme du 1X2 : sa fraîcheur est la lecture du 1X2, pas celle du marché double chance
                read = (ev.get("read_m") or {}).get("h2h") or ev.get("read_at")
                c["sharp_src"] = "h2h"
            if read:
                c["sharp_at"] = read
            if model and model.get("adopted"):
                c["heuristic"] = c["score"]
                c["score"] = learn.predict(model, learn.features(c, now))
            picks.append(c)
    return picks


def rescore(picks, model):
    """Recalcule le score des sélections avec le modèle appris (s'il est adopté) APRÈS les estimations d'avant-match
    (stats.enrich) : leurs écarts modèle-ligne sont des variables du modèle."""
    if model and model.get("adopted"):
        now = datetime.now(timezone.utc)
        for c in picks:
            c["heuristic"] = c.get("heuristic", c["score"])
            c["score"] = learn.predict(model, learn.features(c, now))
    return picks


def snapshot(picks):
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    # « p » = consensus des books sans marge (prob_book), jamais la proba calibrée / mêlée au modèle (c["prob"]) : sinon le
    # moindre écart modèle-books ressemble à un mouvement de cote (faux steam, fausses alertes). « pb » (même valeur) est la
    # seule clé que build_picks lit pour le steam : les anciens relevés, sans « pb », ne comptent plus (pas de migration) ;
    # find_alerts (run_track.py) lit encore « p ».
    return {c["key"]: {"p": round(c.get("prob_book", c["prob"]), 4), "pb": round(c.get("prob_book", c["prob"]), 4),
                       "src": c.get("prob_src", "consensus"), "o": c.get("typ_odds", c["odds"]), "t": now} for c in picks}
