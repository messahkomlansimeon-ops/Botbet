"""Cotes (The Odds API) et construction des sélections."""
import os
import sys
import time
from datetime import datetime, timezone

import requests

from . import budget, config as C, learn


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


def fetch_events(day, props=True, optional=False, use="daily", after=None):
    """Matchs du jour `day` (date UTC) + marchés détaillés, dans la limite du budget `use`.
    1. /events (gratuit) : seules les ligues qui ont des matchs sont payées, les plus chargées d'abord
       (au plus C.MAX_SPORTS) ;
    2. marchés vedettes en bulk (1 appel par ligue, au plus C.FEATURED_SHARE du budget) ;
    3. marchés détaillés, par ordre de priorité de C.PROP_MARKETS (corners, cartons... d'abord), sur les
       matchs les mieux cotés, tant que le budget le permet.
    `optional` : balayage facultatif (alertes, relevé du soir), sauté si crédits bas ; `after` : n'achète
    que les matchs à venir. Une ligue, un match ou un marché refusé ou en erreur est sauté (log stderr)."""
    events = []
    if budget.left(use) <= 0:
        why = f"allocation du jour : {budget.allowance()} crédits, déjà utilisée" if C.BUDGET_SHARES.get(use) else "aucune part dans BUDGET_SHARES"
        print(f"[budget] « {use} » n'a pas de budget aujourd'hui ({why}) : aucun appel payant.", file=sys.stderr)
        return events
    if optional and not credits_ok("balayage large"):
        return events
    window = day_window(day, after)
    try:
        sports = list_sports(limit=False)
    except OddsError as e:
        print(f"[odds] liste des sports indisponible : {e}", file=sys.stderr)
        return events
    leagues = []
    for i, s in enumerate(sports):
        try:
            n = len(league_events(s["key"], window))
        except Exception as e:                          # noqa: BLE001 - une ligue ne tue pas le run
            print(f"[{s['key']}] matchs du jour indisponibles : {e}", file=sys.stderr)
            continue
        if n:
            leagues.append((-n, i, s))
    leagues = [s for _, _, s in sorted(leagues, key=lambda t: t[:2])][:C.MAX_SPORTS]
    print(f"[odds] ligues avec matchs : {', '.join(s['key'] for s in leagues) or 'aucune'} "
          f"(budget « {use} » : {budget.left(use)} crédits)", file=sys.stderr)
    cap = int(budget.left(use) * C.FEATURED_SHARE + 1e-9) if props else budget.left(use)
    used = 0
    for s in leagues:
        markets = C.FEATURED.get(sport_group(s["key"]), "h2h")
        n = len(markets.split(","))
        if used and used + n > cap:                     # au moins une ligue ; le reste va aux marchés détaillés
            print(f"[budget] part des marchés vedettes atteinte ({used}/{cap}) : {s['key']} ignorée.", file=sys.stderr)
            break
        why = budget.why_not(n, use)
        if why:
            print(f"[budget] {s['key']} ignorée ({why}).", file=sys.stderr)
            break
        try:
            for ev in get(f"/sports/{s['key']}/odds", use=use, regions="eu", oddsFormat="decimal",
                          markets=markets, **window) or []:
                ev["sport_key"] = s["key"]
                ev.setdefault("bookmakers", [])
                events.append(ev)
            used += n
        except Exception as e:                          # noqa: BLE001
            print(f"[{s['key']}] ignoré : {e}", file=sys.stderr)
    if not props:
        return events
    events.sort(key=lambda e: -len(e["bookmakers"]))
    top = events[:C.PROP_EVENTS]
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
                "league": ev.get("sport_title", ""), "start": start.strftime("%H:%M"),
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


def snapshot(picks):
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    return {c["key"]: {"p": round(c["prob"], 4), "o": c["odds"], "t": now} for c in picks}
