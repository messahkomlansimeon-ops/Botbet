"""Cotes (The Odds API) et construction des sélections."""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from . import budget, catalogue, config as C, learn


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
    if not key:
        raise OddsError("ODDS_API_KEY absente", code="NO_KEY")
    n = cost(path, params) if use else 0
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


def list_sports(limit=True):
    return select_sports(get("/sports"), limit)         # gratuit (0 crédit)


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_window(day, after=None):
    """Fenêtre commenceTimeFrom/To du jour UTC `day` (début = `after` si plus tard : matchs à venir)."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    if after is not None and after > start:
        start = after
    return {"commenceTimeFrom": _iso(start), "commenceTimeTo": f"{day}T23:59:59Z"}


def league_events(sport_key, window):
    """Matchs d'une ligue dans la fenêtre : endpoint GRATUIT /events (0 crédit)."""
    return get(f"/sports/{sport_key}/events", dateFormat="iso", **window) or []


def scan_today(day, after=None, sports=None, seconds=None):
    """Relevé GRATUIT (0 crédit) des matchs du jour `day` dans toutes les ligues suivies et en saison : /sports puis
    /events de chaque ligue, dans l'ordre de priorité de C.SPORTS (le plus important d'abord), au plus `seconds`
    (C.SCAN_SECONDS) pour tenir le passage du matin. Une ligue en erreur est sautée.
    -> {day, sports: [clés en saison], leagues: {clé: [matchs]}, complete, error}."""
    out = {"day": str(day), "sports": [], "leagues": {}, "complete": True, "error": None}
    window = day_window(day, after)
    try:
        sports = list_sports(limit=False) if sports is None else sports
    except OddsError as e:
        out["error"] = str(e)
        print(f"[odds] liste des sports indisponible : {e}", file=sys.stderr)
        return out
    out["sports"] = [s["key"] for s in sports]
    limit, t0 = C.SCAN_SECONDS if seconds is None else seconds, time.monotonic()
    for i, s in enumerate(sports):
        if time.monotonic() - t0 > limit:
            out["complete"] = False
            print(f"[odds] relevé des matchs coupé après {limit} s : {len(sports) - i} ligue(s) non relevée(s).", file=sys.stderr)
            break
        try:
            evs = league_events(s["key"], window)
        except Exception as e:                          # noqa: BLE001 - une ligue ne tue pas le run
            print(f"[{s['key']}] matchs du jour indisponibles : {e}", file=sys.stderr)
            continue
        if evs:
            out["leagues"][s["key"]] = [dict(ev, sport_key=s["key"]) for ev in evs]
    return out


def scan_ahead(day, sports, days=7, seconds=None):
    """Matchs des `days` jours suivant `day` (gratuit), pour dire quand les ligues reprennent un jour sans match :
    {« AAAA-MM-JJ »: {clé de ligue: nombre de matchs}}. Même limite de durée que scan_today."""
    window = {"commenceTimeFrom": f"{day + timedelta(days=1)}T00:00:00Z",
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


def match_numbers(home, away, sport_key):
    """Chiffres d'avant-match gratuits d'un match (sources.match_features) ; {"sources": []} si rien. Les sélections ne
    sont pas dans les profils de clubs : un nom proche d'un club serait une fausse piste."""
    if catalogue.region(sport_key) == "monde":
        return {"sources": []}
    try:
        from .sources import features             # import tardif : sources importe stats
        return features.match_features(home, away, sport_key)
    except Exception:                                # noqa: BLE001 - stats facultatives
        return {"sources": []}


def model_covered(ev):
    """Vrai si Kelly a un modèle corners/cartons pour ce match : profils football-data des deux équipes (c'est là que
    l'écart modèle-ligne existe, donc là que les marchés détaillés valent leurs crédits)."""
    f = match_numbers(ev["home_team"], ev["away_team"], ev.get("sport_key", ""))
    return "exp_corners" in f or "exp_cards" in f


def fetch_events(day, props=True, optional=False, use="daily", after=None, scan=None):
    """Matchs du jour `day` (date UTC) + marchés détaillés, dans la limite du budget `use`.
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
    if budget.left(use) <= 0:
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
    leagues = catalogue.prioritise(counts, C.SPORTS)[:C.MAX_SPORTS]
    print(f"[odds] ligues avec matchs : {', '.join(leagues) or 'aucune'} "
          f"(budget « {use} » : {budget.left(use)} crédits)", file=sys.stderr)
    used = 0

    def buy(keys, cap):
        """Marchés vedettes en bulk (1 appel par ligue) tant que `cap` crédits ne sont pas dépassés. -> True si le budget
        lui-même a refusé (inutile d'insister)."""
        nonlocal used
        for key in keys:
            markets = C.FEATURED.get(sport_group(key), "h2h")
            n = len(markets.split(","))
            if used and used + n > cap:                 # au moins une ligue ; le reste va aux marchés détaillés
                print(f"[budget] part des marchés vedettes atteinte ({used}/{cap}) : {key} ignorée.", file=sys.stderr)
                return False
            why = budget.why_not(n, use)
            if why:
                print(f"[budget] {key} ignorée ({why}).", file=sys.stderr)
                return True
            try:
                for ev in get(f"/sports/{key}/odds", use=use, regions="eu", oddsFormat="decimal",
                              markets=markets, **window) or []:
                    ev["sport_key"] = key
                    ev.setdefault("bookmakers", [])
                    events.append(ev)
                used += n
                scan["paid"].append(key)
            except Exception as e:                      # noqa: BLE001
                print(f"[{key}] ignoré : {e}", file=sys.stderr)
        return False
    left = budget.left(use)
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
        why = budget.why_not(1, use)
        if why:
            print(f"[budget] {len(jobs) - k} marché(s) détaillé(s) ignoré(s) ({why}).", file=sys.stderr)
            break
        try:
            data = get(f"/sports/{ev['sport_key']}/events/{ev['id']}/odds", use=use, regions="eu",
                       markets=market, oddsFormat="decimal")
        except Exception as e:                          # noqa: BLE001 - 422 = marché inconnu pour ce sport/match
            print(f"[{ev['sport_key']}] {market} ignoré : {e}", file=sys.stderr)
            continue
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
    return get(f"/sports/{sport_key}/scores", use=use, daysFrom=min(max(int(days), 1), 3), dateFormat="iso") or []


def label(market, name, point, who, group):
    unit = C.UNITS.get(market)
    if isinstance(unit, dict):
        unit = unit.get(group, "")
    if unit:
        side = "Plus" if name == "Over" else "Moins"
        return f"{who + ' : ' if who else ''}{side} de {point} {unit}"
    if market == "btts":
        return "Les 2 équipes marquent : " + ("Oui" if name == "Yes" else "Non")
    if market == "spreads":
        return f"{name} {point:+g} (handicap)"
    if market == "double_chance":
        return "Double chance : " + name.replace(" or ", " ou ").replace("Draw", "Nul")
    return "Match nul" if name == "Draw" else name


def league_name(ev):
    """Nom français de la ligue du catalogue (« Ligue des nations UEFA »), sinon le titre donné par l'API."""
    key = ev.get("sport_key", "")
    return catalogue.name(key) if catalogue.region(key) != catalogue.OTHER else ev.get("sport_title", "")


def market_weight(market):
    return C.PLAYER_WEIGHT if market.startswith("player_") else C.MARKET_WEIGHT.get(market, 1.2)


def build_picks(events, day, after=None, history=None, model=None):
    """Toutes les sélections : proba de consensus, meilleure cote, valeur et flair.

    score = valeur (meilleure cote vs consensus)
          + désaccord entre bookmakers (un book en retard = opportunité)
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
        if start.date() != day or (after and start <= after):
            continue
        group = sport_group(ev.get("sport_key", "soccer_"))
        probs, best, bests_book = {}, {}, {}
        for bk in ev.get("bookmakers", []):
            for m in bk["markets"]:
                groups = {}
                for o in m["outcomes"]:
                    pt = o.get("point")
                    if m["key"] == "spreads" and pt is not None:
                        pt = abs(pt)                    # handicap : -1.5 / +1.5 forment une paire
                    groups.setdefault((o.get("description"), pt), []).append(o)
                # double chance : 3 issues dont les probas somment à 2 (pas 1)
                mass = 2.0 if m["key"] == "double_chance" else 1.0
                for (who, _), outs in groups.items():
                    if len(outs) < 2:
                        continue
                    total = sum(1 / o["price"] for o in outs)
                    for o in outs:
                        k = (m["key"], who, o.get("point"), o["name"])
                        probs.setdefault(k, []).append(mass / o["price"] / total)
                        if o["price"] > best.get(k, 0):
                            best[k], bests_book[k] = o["price"], bk.get("title", bk["key"])
        for k, ps in probs.items():
            if len(ps) < C.MIN_BOOKMAKERS:
                continue
            market, who, point, name = k
            p, odds = sum(ps) / len(ps), best[k]
            if not C.ODDS_RANGE[0] <= odds <= C.ODDS_RANGE[1]:
                continue
            key = f'{ev["id"]}|{market}|{who or ""}|{point if point is not None else ""}|{name}'
            edge = p * odds - 1
            spread = (max(ps) - min(ps)) / p
            prev = history.get(key)
            prev_p = prev["p"] if isinstance(prev, dict) else prev
            steam = (p - prev_p) / prev_p if prev_p else 0.0
            weight = market_weight(market)
            signals = []
            if edge > 0.03:
                signals.append(f"cote {odds:.2f} chez {bests_book[k]} > consensus {1/p:.2f}")
            if spread > 0.12:
                signals.append("books en désaccord")
            if steam > 0.04:
                signals.append(f"cote en baisse ({steam*100:+.0f}% de proba)")
            p_cal = learn.calibrated(model, market, p)
            c = {
                "key": key, "id": ev["id"], "sport_key": ev.get("sport_key", ""), "group": group,
                "event": f'{ev["home_team"]} - {ev["away_team"]}',
                "home": ev["home_team"], "away": ev["away_team"],
                "league": league_name(ev), "start": start.strftime("%H:%M"),
                "commence": ev["commence_time"],
                "market": market, "who": who, "point": point, "name": name,
                "pick": label(market, name, point, who, group),
                "prob": p_cal, "prob_book": p, "odds": odds, "book": bests_book[k], "edge": p_cal * odds - 1,
                "spread": spread, "steam": steam, "n_books": len(ps), "weight": weight,
                "score": (edge + 0.5 * spread + 2 * steam) * weight + (weight - 1) * 0.05,
                "why": ", ".join(signals),
            }
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
    return {c["key"]: {"p": round(c["prob"], 4), "o": c["odds"], "t": now} for c in picks}
