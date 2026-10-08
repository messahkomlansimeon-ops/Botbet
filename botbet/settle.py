"""Règlement des paris (résultats réels) et bilan."""
from datetime import datetime, timedelta, timezone

from . import config as C, odds, stats, store


def _score_map(ev):
    return {s["name"]: float(s["score"]) for s in (ev.get("scores") or [])}


def settle_pick(b, ev, fstats=None):
    """Résultat d'un pari : 'win', 'loss', 'push', ou None si non réglable."""
    sc = _score_map(ev)
    if b["home"] not in sc or b["away"] not in sc:
        return None
    h, a = sc[b["home"]], sc[b["away"]]
    m, name, point = b["market"], b["name"], b["point"]
    winner = "Draw" if h == a else (b["home"] if h > a else b["away"])
    if m == "h2h":
        return "win" if name == winner else "loss"
    if m == "double_chance":
        parts = [p.strip() for p in name.split(" or ")]
        return "win" if winner in parts else "loss"
    if m == "btts":
        both = h > 0 and a > 0
        return "win" if (name == "Yes") == both else "loss"
    if m == "spreads":
        mine, other = (h, a) if name == b["home"] else (a, h)
        diff = mine + point - other
        return "push" if diff == 0 else ("win" if diff > 0 else "loss")
    totals = {"totals": h + a}
    if fstats:
        totals["alternate_totals_corners"] = fstats["corners"]
        totals["alternate_totals_cards"] = fstats["cards"]
        field = {"player_shots": "shots", "player_shots_on_target": "shots_on_target",
                 "player_fouls": "fouls"}.get(m)
        if field:
            v = stats.player_stat(fstats, b["who"] or "", field)
            if v is not None:
                totals[m] = v
    if m not in totals:
        return None
    v = totals[m]
    if v == point:
        return "push"
    return "win" if (name == "Over") == (v > point) else "loss"


def settle_pending(now=None, scores_by_sport=None, stats_for=None):
    """Règle les paris passés. `scores_by_sport` / `stats_for` injectables (tests)."""
    now = now or datetime.now(timezone.utc)
    bets = store.load("bets.json", {"picks": [], "combos": []})
    pending = [b for b in bets["picks"] if b.get("result") is None
               and datetime.fromisoformat(b["commence"].replace("Z", "+00:00")) < now - timedelta(hours=3)]
    if not pending:
        return bets, []
    scores_by_sport = scores_by_sport or {}
    settled = []
    for b in pending:
        if b["sport_key"] not in scores_by_sport:
            try:
                scores_by_sport[b["sport_key"]] = odds.fetch_scores(b["sport_key"])
            except Exception as e:                 # noqa: BLE001
                print(f"scores {b['sport_key']} : {e}")
                scores_by_sport[b["sport_key"]] = []
        ev = next((e for e in scores_by_sport[b["sport_key"]] if e["id"] == b["id"] and e.get("completed")), None)
        if not ev:
            # Match > 3 jours sans score : on l'abandonne (annulé/reporté).
            if datetime.fromisoformat(b["commence"].replace("Z", "+00:00")) < now - timedelta(days=3):
                b["result"] = "void"
            continue
        fstats = None
        if b["market"] not in ("h2h", "double_chance", "btts", "spreads", "totals"):
            day = b["commence"][:10]
            if stats_for:
                fstats = stats_for(b)
            elif stats.enabled() and b["group"] == "soccer":
                fx = stats.find_fixture(day, b["home"], b["away"])
                fstats = stats.fixture_stats(fx["id"]) if fx else None
        b["result"] = settle_pick(b, ev, fstats)
        if b["result"] is None and datetime.fromisoformat(b["commence"].replace("Z", "+00:00")) < now - timedelta(days=3):
            b["result"] = "void"                   # pas de stats disponibles
        if b["result"]:
            settled.append(b)
    by_key = {b["key"]: b for b in bets["picks"]}
    for combo in bets["combos"]:
        if combo.get("result") is not None:
            continue
        results = [by_key.get(k, {}).get("result") for k in combo["keys"]]
        if any(r == "loss" for r in results):
            combo["result"] = "loss"
        elif all(r in ("win", "push", "void") for r in results):
            combo["result"] = "win"
            # jambes annulées : cote recalculée
            combo["odds_final"] = 1.0
            for k in combo["keys"]:
                if by_key[k]["result"] == "win":
                    combo["odds_final"] *= by_key[k]["odds"]
            if combo["odds_final"] == 1.0:
                combo["result"] = "void"
    store.save("bets.json", bets)
    return bets, settled


def record(picks_sent, combos_sent, day):
    """Enregistre les paris envoyés (sans doublon de clé pour un même jour)."""
    bets = store.load("bets.json", {"picks": [], "combos": []})
    known = {b["key"] for b in bets["picks"]}
    for c in picks_sent:
        if c["key"] in known:
            continue
        known.add(c["key"])
        bets["picks"].append({k: c[k] for k in (
            "key", "id", "sport_key", "group", "event", "home", "away", "league", "commence",
            "market", "who", "point", "name", "pick", "odds", "prob", "why")} | {"date": str(day), "result": None})
    for combo in combos_sent:
        bets["combos"].append(combo | {"date": str(day), "result": None})
    store.save("bets.json", bets)


def _rate(rows):
    n = sum(1 for r in rows if r["result"] in ("win", "loss"))
    w = sum(1 for r in rows if r["result"] == "win")
    ret = sum((r["odds"] - 1) if r["result"] == "win" else (-1 if r["result"] == "loss" else 0) for r in rows)
    return n, w, (ret / n * 100 if n else 0.0)


def bilan(bets, days=None):
    """Texte du bilan : global, par palier, par marché."""
    picks = [b for b in bets["picks"] if b.get("result") in ("win", "loss")]
    combos = [c for c in bets["combos"] if c.get("result") in ("win", "loss")]
    if days:
        cutoff = str((datetime.now(timezone.utc) - timedelta(days=days)).date())
        picks = [b for b in picks if b["date"] >= cutoff]
        combos = [c for c in combos if c["date"] >= cutoff]
    if not picks and not combos:
        return "📊 BILAN\nAucun pari réglé pour l'instant."
    n, w, roi = _rate(picks)
    lines = [f"📊 BILAN{f' ({days} derniers jours)' if days else ''}",
             f"Paris simples : {w}/{n} gagnés ({w/n*100:.0f}%) | rendement {roi:+.0f}% par mise" if n else "Paris simples : aucun réglé"]
    tiers = {}
    for c in combos:
        tiers.setdefault(c["tier"].split(" (")[0], []).append(c)
    for tier, rows in tiers.items():
        nn = len(rows); ww = sum(1 for r in rows if r["result"] == "win")
        gain = sum(r["stake"] * (r.get("odds_final", r["odds"]) - 1) if r["result"] == "win" else -r["stake"] for r in rows)
        lines.append(f"{tier} : {ww}/{nn} | {gain:+.1f} unités")
    lines.append("Par marché :")
    markets = {}
    for b in picks:
        markets.setdefault(b["market"], []).append(b)
    for m, rows in sorted(markets.items(), key=lambda kv: -len(kv[1])):
        nn, ww, rr = _rate(rows)
        if nn:
            lines.append(f"• {C.MARKET_LABELS.get(m, m)} : {ww}/{nn} ({ww/nn*100:.0f}%) | {rr:+.0f}%")
    pending = sum(1 for b in bets["picks"] if b.get("result") is None)
    if pending:
        lines.append(f"({pending} paris en attente de résultat)")
    return "\n".join(lines)
