"""Règlement des paris (résultats réels) et bilan."""
import sys
from datetime import datetime, timedelta, timezone

from . import config as C, fair, fmt, learn, odds, stats, store, track
from .sources import footballdata as fd

GOAL_MARKETS = ("h2h", "double_chance", "btts", "spreads", "totals")
FD_MARKETS = ("alternate_totals_corners", "alternate_totals_cards")     # réglables avec les CSV football-data


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
        for mk, k in (("alternate_totals_corners", "corners"), ("alternate_totals_cards", "cards")):
            if fstats.get(k) is not None:
                totals[mk] = fstats[k]
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


def final_text(b, ev, fstats=None):
    """Score ou total final du match tel qu'on l'affiche : « 2-0 » (buts), « 11 corners », « 4 cartons », « 2 tirs »."""
    sc = _score_map(ev)
    m = b["market"]
    if m in GOAL_MARKETS and b["home"] in sc and b["away"] in sc:
        return f"{sc[b['home']]:g}-{sc[b['away']]:g}"
    fstats = fstats or {}
    if m in FD_MARKETS and fstats.get("corners" if "corners" in m else "cards") is not None:
        return f"{fstats['corners' if 'corners' in m else 'cards']:g} {'corners' if 'corners' in m else 'cartons'}"
    if m.startswith("player_"):
        field = {"player_shots": "shots", "player_shots_on_target": "shots_on_target", "player_fouls": "fouls"}.get(m)
        v = stats.player_stat(fstats, b.get("who") or "", field) if field else None
        unit = C.UNITS.get(m)
        return f"{v:g} {unit}" if v is not None and isinstance(unit, str) else ""
    return f"{sc[b['home']]:g}-{sc[b['away']]:g}" if b["home"] in sc and b["away"] in sc else ""


def football_score(b):
    """Score d'un match de foot terminé via API-Football (GRATUIT : 1 requête par jour réglé, mise en
    cache), au format attendu par settle_pick ; None si introuvable ou pas terminé."""
    r = stats.fixture_result(b["commence"][:10], b["home"], b["away"], kickoff=b["commence"])
    if not r or not r["finished"]:
        return None
    return {"id": b["id"], "completed": True,
            "scores": [{"name": b["home"], "score": str(r["home"])}, {"name": b["away"], "score": str(r["away"])}]}


class FootballData:
    """Règlement gratuit avec les CSV football-data déjà en cache (aucune requête : refresh_all les actualise le
    matin). Le site ne se met à jour que ~2 fois par semaine : un match récent peut manquer."""

    def __init__(self):
        self._by_div, self._memo = {}, {}

    def match(self, b):
        div = fd.SPORT_TO_DIV.get(b["sport_key"])
        if div not in self._by_div:
            try:
                self._by_div[div] = fd.cached_history([div] if div else None)
            except Exception as e:                    # noqa: BLE001 - facultatif
                print(f"[settle] football-data ignoré : {e}", file=sys.stderr)
                self._by_div[div] = []
        k = (b["home"], b["away"], b["commence"][:10])
        if k not in self._memo:
            self._memo[k] = fd.find_match(self._by_div[div], b["home"], b["away"], b["commence"][:10])
        return self._memo[k]

    def score(self, b):
        m = self.match(b)
        return m and {"id": b["id"], "completed": True,
                      "scores": [{"name": b["home"], "score": str(m["hg"])}, {"name": b["away"], "score": str(m["ag"])}]}

    def stats(self, b):
        m = self.match(b)
        return fd.match_stats(m) if m else None


def match_stats(b, fdata, stats_for=None):
    """Totaux du match d'un pari hors buts (corners, cartons, stats joueurs) : API-Football d'abord, puis les CSV
    football-data si API-Football est absent ou son quota épuisé. None pour un marché de buts."""
    if b["market"] in GOAL_MARKETS:
        return None
    soccer, fstats = b["group"] == "soccer", None
    if stats_for:
        fstats = stats_for(b)
    elif stats.enabled() and soccer:
        fx = stats.find_fixture(b["commence"][:10], b["home"], b["away"], kickoff=b["commence"])
        fstats = stats.fixture_stats(fx["id"]) if fx else None
    need = "corners" if "corners" in b["market"] else "cards"
    if soccer and b["market"] in FD_MARKETS and (fstats or {}).get(need) is None:
        fstats = fdata.stats(b) or fstats              # API-Football absent ou quota épuisé : football-data
    return fstats


def settle_pending(now=None, scores_by_sport=None, stats_for=None):
    """Règle les paris suivis passés (envoyés d'abord). Le foot se règle gratuitement : API-Football d'abord,
    puis les CSV football-data en cache (buts, corners, cartons, quand API-Football est absent ou son quota
    épuisé) ; /scores de The Odds API (2 crédits par ligue, budgétés) ne sert que de dernier repli pour les
    marchés de buts, sans STATS_API_KEY, ou pour un sport hors foot. Un pari sans résultat est annulé après
    WAIT_DAYS jours (buts), WAIT_DAYS_STATS pour corners et cartons (football-data se met à jour ~2 fois par
    semaine). `scores_by_sport` / `stats_for` : injectables (tests)."""
    now = now or datetime.now(timezone.utc)
    rows = track.load()
    bets = store.load("bets.json", {"picks": [], "combos": []})
    pending = [r for r in rows if r.get("result") is None
               and datetime.fromisoformat(r["commence"].replace("Z", "+00:00")) < now - timedelta(hours=3)]
    pending.sort(key=lambda r: not r["sent"])
    scores_by_sport = scores_by_sport or {}
    fdata = FootballData()
    settled = []
    for b in pending:
        wait = C.WAIT_DAYS_STATS if b["market"] in FD_MARKETS else C.WAIT_DAYS
        old = datetime.fromisoformat(b["commence"].replace("Z", "+00:00")) < now - timedelta(days=wait)
        soccer = b["group"] == "soccer"
        ev = next((e for e in scores_by_sport.get(b["sport_key"], []) if e["id"] == b["id"] and e.get("completed")), None)
        if not ev and soccer:
            ev = (football_score(b) if stats.enabled() else None) or fdata.score(b)
        free = soccer and stats.enabled()
        if (not ev and not free and (not soccer or b["market"] in GOAL_MARKETS)
                and b["sport_key"] not in scores_by_sport):
            try:                                   # repli payant, une fois par sport et par exécution
                scores_by_sport[b["sport_key"]] = odds.fetch_scores(b["sport_key"])
            except Exception as e:                 # noqa: BLE001 - budget refusé, réseau... : on réessaiera
                print(f"[settle] scores {b['sport_key']} ignorés : {e}", file=sys.stderr)
                scores_by_sport[b["sport_key"]] = []
            ev = next((e for e in scores_by_sport[b["sport_key"]] if e["id"] == b["id"] and e.get("completed")), None)
        if not ev:
            if old:
                b["result"] = "void"               # annulé / reporté / jamais de score
            continue
        fst = match_stats(b, fdata, stats_for)
        b["result"] = settle_pick(b, ev, fst)
        if b["result"] in ("win", "loss", "push"):
            b["final"] = final_text(b, ev, fst)       # affiché dans le message de résultats : « 2-0 », « 11 corners »
        if b["result"] is None and old:
            b["result"] = "void"                   # pas de stats disponibles
        if b["result"]:
            settled.append(b)
    by_key = {r["key"]: r for r in rows}
    for combo in bets["combos"]:
        if combo.get("result") is not None:
            continue
        results = [by_key.get(k, {}).get("result") for k in combo["keys"]]
        if any(r == "loss" for r in results):
            combo["result"] = "loss"
        elif all(r in ("win", "push", "void") for r in results):
            combo["result"] = "win"
            combo["odds_final"] = 1.0              # jambes annulées : cote recalculée
            for k in combo["keys"]:
                if by_key[k]["result"] == "win":
                    combo["odds_final"] *= by_key[k]["odds"]
            if combo["odds_final"] == 1.0:
                combo["result"] = "void"
    track.save(rows)
    store.save("bets.json", bets)
    return bets, settled


def settle_fair(now=None, stats_for=None):
    """Règle les pronostics sans cote (data/fair.json) avec les mêmes sources gratuites que les paris (API-Football,
    puis CSV football-data) : jamais de crédit, jamais d'annulation payante. Un pronostic sans résultat est annulé après
    WAIT_DAYS (buts) ou WAIT_DAYS_STATS (corners, cartons). -> (toutes les lignes, lignes réglées cette fois)."""
    now = now or datetime.now(timezone.utc)
    rows = fair.load()
    fdata, settled = FootballData(), []
    for b in rows:
        ko = datetime.fromisoformat(b["commence"].replace("Z", "+00:00"))
        if b.get("result") is not None or ko >= now - timedelta(hours=3):
            continue
        full = {"group": "soccer", "who": None} | b            # lignes de fair.json allégées : champs de settle_pick ajoutés
        old = ko < now - timedelta(days=C.WAIT_DAYS_STATS if b["market"] in FD_MARKETS else C.WAIT_DAYS)
        ev = (football_score(full) if stats.enabled() else None) or fdata.score(full)
        if ev:
            b["result"] = settle_pick(full, ev, match_stats(full, fdata, stats_for))
        if b.get("result") is None and old:
            b["result"] = "void"
        if b.get("result"):
            settled.append(b)
    store.save(fair.FILE, rows)
    return rows, settled


def record_combos(combos_sent, day):
    bets = store.load("bets.json", {"picks": [], "combos": []})
    for combo in combos_sent:
        if any(c["date"] == str(day) and c["tier"] == combo["tier"] and c["keys"] == combo["keys"] for c in bets["combos"]):
            continue                                # relance du matin avec les mêmes cotes (cache) : pas de doublon
        bets["combos"].append(combo | {"date": str(day), "result": None})
    store.save("bets.json", bets)


def _rate(rows):
    n = sum(1 for r in rows if r["result"] in ("win", "loss"))
    w = sum(1 for r in rows if r["result"] == "win")
    ret = sum((r["odds"] - 1) if r["result"] == "win" else (-1 if r["result"] == "loss" else 0) for r in rows)
    return n, w, (ret / n * 100 if n else 0.0)


def _block(name, rows):
    n, w, roi = _rate(rows)
    if not n:
        return f"{name} : aucun réglé"
    txt = f"{name} : {w}/{n} gagnés ({w/n*100:.0f}%) | rendement {roi:+.0f}% par mise"
    clv = [r["clv"] for r in rows if r.get("clv") is not None]
    if clv:
        txt += f" | CLV {sum(clv)/len(clv)*100:+.1f}%"
    return txt


def bilan(bets, days=None, rows=None, detail=False):
    """Bilan. Par défaut en mots simples (combinés gagnés, résultat en unités, paris envoyés) ; `detail` : les chiffres
    techniques (rendement par mise, CLV, Brier, par marché)."""
    rows = track.load() if rows is None else rows
    picks = [r for r in rows if r.get("result") in ("win", "loss")]
    combos = [c for c in bets["combos"] if c.get("result") in ("win", "loss")]
    if days:
        cutoff = str((datetime.now(timezone.utc) - timedelta(days=days)).date())
        picks = [b for b in picks if b["date"] >= cutoff]
        combos = [c for c in combos if c["date"] >= cutoff]
    if not picks and not combos:
        return "📊 BILAN\nAucun pari réglé pour l'instant." if detail else fmt.Html("📊 <b>Bilan</b>\nAucun pari réglé pour l'instant.")
    return _bilan_detail(bets, days, rows, picks, combos) if detail else _bilan_simple(days, rows, picks, combos)


def fair_line(rows, days=None, today=None):
    """Pronostics sans cote réglés, en mots simples : « Pronostics sans cote : 5 gagnés sur 8 (62 %), Kelly en annonçait 64 % en
    moyenne. » ('' si rien de réglé)."""
    done = [r for r in rows if r.get("result") in ("win", "loss")]
    if days:
        cut = str((datetime.fromisoformat(str(today)) if today else datetime.now(timezone.utc)).date() - timedelta(days=days))
        done = [r for r in done if r["date"] >= cut]
    if not done:
        return ""
    w = sum(1 for r in done if r["result"] == "win")
    return (f"Pronostics sans cote : {w} gagné{'s' if w > 1 else ''} sur {len(done)} ({fmt.pct(w / len(done))}), Kelly en "
            f"annonçait {fmt.pct(sum(r['prob'] for r in done) / len(done))} en moyenne.")


def _gain(c):
    return c["stake"] * (c.get("odds_final", c["odds"]) - 1) if c["result"] == "win" else -c["stake"]


def _bilan_simple(days, rows, picks, combos):
    sent = [r for r in picks if r.get("sent")]
    lines = [f"📊 <b>Bilan</b>{f' — {days} derniers jours' if days else ''}"]
    if combos:
        w = sum(1 for c in combos if c["result"] == "win")
        lines.append(f"Combinés : {w} gagné{'s' if w > 1 else ''} sur {len(combos)} · résultat {fmt.signed(sum(map(_gain, combos)))}")
        tiers = {}
        for c in combos:
            tiers.setdefault(fmt.tier_name(c["tier"]), []).append(c)
        for tier, cs in tiers.items():
            lines.append(f"   {fmt.esc(tier)} : {sum(1 for c in cs if c['result'] == 'win')} sur {len(cs)} "
                         f"({fmt.signed(sum(map(_gain, cs)))})")
    if sent:
        w = sum(1 for r in sent if r["result"] == "win")
        lines.append(f"Paris envoyés : {w} gagnés sur {len(sent)} ({fmt.pct(w / len(sent))})")
    fl = fair_line(fair.load(), days)
    if fl:
        lines.append(fmt.esc(fl))
    pending = sum(1 for r in rows if r.get("result") is None)
    if pending:
        lines.append(fmt.i(f"{pending} pari{'s' if pending > 1 else ''} en attente de résultat"))
    lines.append(fmt.i("Chiffres techniques : /bilan détail"))
    return fmt.Html("\n".join(lines))


def _bilan_detail(bets, days, rows, picks, combos):
    sent = [r for r in picks if r.get("sent")]
    ghost = [r for r in picks if not r.get("sent")]
    lines = [f"📊 BILAN{f' ({days} derniers jours)' if days else ''}",
             _block("Envoyés", sent), _block("Suivis en silence", ghost)]
    tiers = {}
    for c in combos:
        tiers.setdefault(c["tier"].split(" (")[0], []).append(c)
    for tier, rows_ in tiers.items():
        nn = len(rows_); ww = sum(1 for r in rows_ if r["result"] == "win")
        gain = sum(r["stake"] * (r.get("odds_final", r["odds"]) - 1) if r["result"] == "win" else -r["stake"] for r in rows_)
        lines.append(f"{tier} : {ww}/{nn} | {gain:+.1f} unités")
    if sent:
        lines.append("Par marché (envoyés) :")
        markets = {}
        for b in sent:
            markets.setdefault(b["market"], []).append(b)
        for m, rows_ in sorted(markets.items(), key=lambda kv: -len(kv[1])):
            nn, ww, rr = _rate(rows_)
            if nn:
                lines.append(f"• {C.MARKET_LABELS.get(m, m)} : {ww}/{nn} ({ww/nn*100:.0f}%) | {rr:+.0f}%")
    if picks:
        brier = sum((r["prob"] - (1.0 if r["result"] == "win" else 0.0)) ** 2 for r in picks) / len(picks)
        lines.append(f"Brier (justesse des probas, plus bas = mieux, 0.25 = hasard) : {brier:.3f}")
    pending = sum(1 for r in rows if r.get("result") is None)
    if pending:
        lines.append(f"({pending} paris en attente de résultat)")
    fl = fair.summary(fair.load(), days)
    if fl:
        lines.append(fl)
    return "\n".join(lines)
