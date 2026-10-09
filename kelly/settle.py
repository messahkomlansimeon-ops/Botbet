"""Règlement des paris (résultats réels) et bilan."""
import sys
from datetime import datetime, timedelta, timezone

from . import calib, config as C, fair, fmt, learn, odds, played, stats, store, track
from .sources import fmmatch, footballdata as fd, fotmob, names

GOAL_MARKETS = ("h2h", "double_chance", "btts", "spreads", "totals")
FD_MARKETS = ("alternate_totals_corners", "alternate_totals_cards")     # réglables avec les CSV football-data
UNKNOWN = "unknown"                                        # match joué mais jambe invérifiable (« non vérifiable ») : ni gagné ni perdu
SHOT_MARKETS = tuple(f"team_{k}_{s}" for k in ("shots", "sot", "fouls", "throws", "offsides") for s in ("total", "home", "away"))


def _side_value(sd, fld):
    """Tirs (« shots ») ou tirs cadrés (« sot ») d'une équipe d'après fstats["side"][côté] ; None si l'API les a laissés à null."""
    if not sd or fld in (sd.get("nul") or []) or sd.get(fld) is None:
        return None
    return sd[fld]


def shot_value(market, fstats):
    """Valeur réelle d'un marché de tirs / tirs cadrés (total du match ou une équipe) d'après fstats, ou None."""
    fld = fmt.TEAM_MARKET.match(market)[1]
    side = fstats.get("side") or {}
    if market.endswith("_total"):
        vals = [_side_value(side.get(s), fld) for s in ("home", "away")]
        if side and all(v is not None for v in vals):
            return sum(vals)
        # par équipe, la source n'a que tirs / tirs cadrés (football-data) : le total du match (fautes...) fait foi ; une stat laissée à
        # null par API-Football (« nul ») reste inconnue, son total compterait les null comme 0
        absent = all(fld not in (side.get(s) or {}) and fld not in ((side.get(s) or {}).get("nul") or []) for s in ("home", "away"))
        return fstats.get(fld) if not side or absent else None
    return _side_value(side.get("home" if market.endswith("_home") else "away"), fld)


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
        if m in SHOT_MARKETS and shot_value(m, fstats) is not None:
            totals[m] = shot_value(m, fstats)
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
    if m in SHOT_MARKETS:
        v = shot_value(m, fstats)
        return f"{v:g} {fmt.TEAM_UNITS[fmt.TEAM_MARKET.match(m)[1]]}" if v is not None else ""
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


FM_DAYS = 1                                                # écart de date toléré entre le coup d'envoi 1xBet et le match FotMob
FM_MIN_SIM = 0.6                                           # ressemblance minimale des noms : seuls les matchs de la ligue à ±1 jour sont candidats (« Slavia Praha » ~ « Slavia Prague » : 0,7)


def fotmob_score(b, leagues=None):
    """Score d'un match de championnat vu par 1xBet d'après les derniers résultats FotMob de la ligue (src_fotmob.json, matches[sport_key]) :
    repli quand API-Football (noms trop différents : « Slavia Prague » / « Slavia Praha ») et football-data (pas de division) n'ont rien.
    Date à FM_DAYS jour près, les deux équipes reconnues (names.best_team, seuil FM_MIN_SIM : la fenêtre ne compte qu'une dizaine de
    matchs) parmi les matchs de cette fenêtre. Au format de settle_pick ; None sinon.
    `leagues` : profils de ligues déjà lus (src_fotmob.json["leagues"])."""
    if leagues is None:
        leagues = fotmob.profiles().get("leagues") or {}
    try:
        day = datetime.fromisoformat(b["commence"][:10])
        win = [m for m in (leagues.get(b.get("sport_key")) or {}).get("matches") or []
               if m.get("d") and abs((datetime.fromisoformat(m["d"]) - day).days) <= FM_DAYS
               and m.get("hg") is not None and m.get("ag") is not None]
    except (ValueError, TypeError, AttributeError):
        return None
    h = names.best_team(b["home"], list(dict.fromkeys(m["home"] for m in win)), min_sim=FM_MIN_SIM)
    a = names.best_team(b["away"], list(dict.fromkeys(m["away"] for m in win)), min_sim=FM_MIN_SIM)
    m = next((m for m in win if m["home"] == h and m["away"] == a), None) if h and a else None
    return m and {"id": b["id"], "completed": True,
                  "scores": [{"name": b["home"], "score": str(m["hg"])}, {"name": b["away"], "score": str(m["ag"])}]}


class FootballData:
    """Règlement gratuit avec les CSV football-data déjà en cache (aucune requête : refresh_all les actualise le
    matin). Le site ne se met à jour que ~2 fois par semaine : un match récent peut manquer."""

    def __init__(self):
        self._by_div, self._memo, self._fm = {}, {}, None

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

    def fotmob_score(self, b):
        """Score FotMob (fotmob_score) ; le fichier de profils n'est lu qu'une fois par exécution."""
        if self._fm is None:
            try:
                self._fm = fotmob.profiles().get("leagues") or {}
            except Exception as e:                    # noqa: BLE001 - facultatif
                print(f"[settle] FotMob (scores) ignoré : {e}", file=sys.stderr)
                self._fm = {}
        return fotmob_score(b, self._fm)


def match_stats(b, fdata, stats_for=None):
    """Totaux du match d'un pari hors buts (corners, cartons, tirs, stats joueurs) : API-Football d'abord, puis les CSV
    football-data si API-Football est absent ou son quota épuisé. None pour un marché de buts. fstats["side"] : tirs et
    tirs cadrés de chaque équipe (marchés par équipe)."""
    if b["market"] in GOAL_MARKETS:
        return None
    soccer, fstats = b["group"] == "soccer", None
    if stats_for:
        fstats = stats_for(b)
    elif stats.enabled() and soccer:
        fx = stats.find_fixture(b["commence"][:10], b["home"], b["away"], kickoff=b["commence"])
        fstats = stats.fixture_stats(fx["id"], players=b["market"].startswith("player_")) if fx else None
        if fstats and fx:                              # tirs de chaque équipe : les marchés « par équipe » en ont besoin
            teams = fstats.get("teams") or {}
            fstats = dict(fstats, side={"home": teams.get(str(fx["home_id"])), "away": teams.get(str(fx["away_id"]))})
    need = "corners" if "corners" in b["market"] else "cards"
    if soccer and b["market"] in FD_MARKETS and (fstats or {}).get(need) is None:
        fstats = fdata.stats(b) or fstats              # API-Football absent ou quota épuisé : football-data
    elif soccer and b["market"] in SHOT_MARKETS and shot_value(b["market"], fstats or {}) is None:
        fstats = fdata.stats(b) or fstats
    if soccer and (b["market"] in SHOT_MARKETS and shot_value(b["market"], fstats or {}) is None
                   or b["market"] in FD_MARKETS and (fstats or {}).get(need) is None):
        try:                                           # touches, hors-jeu (FotMob seul les a), ou rien ailleurs : page du match FotMob
            fstats = fmmatch.match_stats(b) or fstats
        except Exception as e:                         # noqa: BLE001 - facultatif
            print(f"[settle] FotMob ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    return fstats


def settle_pending(now=None, scores_by_sport=None, stats_for=None):
    """Règle les paris suivis passés (envoyés d'abord). Le foot se règle gratuitement : API-Football d'abord,
    puis les CSV football-data en cache (buts, corners, cartons, quand API-Football est absent ou son quota
    épuisé) ; /scores de The Odds API (2 crédits par ligue, budgétés) ne sert que de dernier repli pour les
    marchés de buts, sans STATS_API_KEY, ou pour un sport hors foot. Un pari sans score est annulé (« void ») après
    WAIT_DAYS jours (buts), WAIT_DAYS_STATS pour corners et cartons (football-data se met à jour ~2 fois par
    semaine) ; un pari dont le match a un score mais dont les stats restent introuvables devient « unknown » (non
    vérifiable : jamais compté gagné ni perdu, et un combiné qui le contient devient « unknown » sans toucher la bankroll). `scores_by_sport` / `stats_for` : injectables (tests)."""
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
            b["result"] = UNKNOWN                  # match joué mais stats introuvables : ni gagné ni annulé chez le bookmaker
        if b["result"]:
            settled.append(b)
    by_key = {r["key"]: r for r in rows}
    for combo in bets["combos"]:
        if combo.get("result") is not None:
            continue
        results = [by_key.get(k, {}).get("result") for k in combo["keys"]]
        if any(r == "loss" for r in results):
            combo["result"] = "loss"
        elif all(r in ("win", "push", "void", UNKNOWN) for r in results) and UNKNOWN in results:
            combo["result"] = UNKNOWN              # une jambe jouée mais invérifiable : on ne sait pas si le combiné a passé
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


def _settle_fair_row(b, now, fdata, stats_for=None):
    """Règle une ligne de pronostic (data/fair.json ou jambe d'un combiné à cote juste) avec les sources gratuites : API-Football,
    puis CSV football-data. Sans résultat, la ligne est annulée après WAIT_DAYS (buts) ou WAIT_DAYS_STATS (corners, cartons,
    tirs). -> True si elle vient d'être réglée."""
    ko = datetime.fromisoformat(b["commence"].replace("Z", "+00:00"))
    if b.get("result") is not None or ko >= now - timedelta(hours=3):
        return False
    full = {"group": "soccer", "who": None} | b            # lignes de fair.json allégées : champs de settle_pick ajoutés
    stat_market = b["market"] in FD_MARKETS or b["market"] in SHOT_MARKETS
    old = ko < now - timedelta(days=C.WAIT_DAYS_STATS if stat_market else C.WAIT_DAYS)
    ev = (football_score(full) if stats.enabled() else None) or fdata.score(full) or fdata.fotmob_score(full)
    if ev:
        fst = match_stats(full, fdata, stats_for)
        b["result"] = settle_pick(full, ev, fst)
        if b["result"] in ("win", "loss", "push"):
            b["final"] = final_text(full, ev, fst)
    if b.get("result") is None and old:
        b["result"] = UNKNOWN if ev else "void"       # score connu mais stats introuvables : non vérifiable ; pas de score : annulé
    return bool(b.get("result"))


def settle_fair(now=None, stats_for=None):
    """Règle les pronostics sans cote (data/fair.json) avec les mêmes sources gratuites que les paris (API-Football,
    puis CSV football-data) : jamais de crédit, jamais d'annulation payante. -> (toutes les lignes, lignes réglées cette fois)."""
    now = now or datetime.now(timezone.utc)
    rows = fair.load()
    fdata = FootballData()
    settled = [b for b in rows if _settle_fair_row(b, now, fdata, stats_for)]
    calib.add(rows)                                 # cumul par famille, gardé pour toujours (fair.json : 10 jours)
    store.save(fair.FILE, rows)
    return rows, settled


def settle_fair_combos(now=None, stats_for=None):
    """Règle les combinés à cote juste (data/fair_combos.json) : perdu dès qu'une jambe est perdue, gagné quand toutes sont
    gagnées (jambes remboursées ignorées). Une jambe sans score trouvé est « unknown », pas « void » : le combiné est alors « unknown »,
    jamais gagné. Gratuit, comme les pronostics. -> (tous, réglés cette fois)."""
    now = now or datetime.now(timezone.utc)
    rows, fdata, settled = fair.load_combos(), FootballData(), []
    for c in rows:
        if c.get("result") is not None:
            continue
        for leg in c["legs"]:
            _settle_fair_row(leg, now, fdata, stats_for)
            if leg.get("result") == "void":
                leg["result"] = UNKNOWN                # aucun score trouvé : on ne sait pas si la jambe a passé, le combiné ne peut pas être « gagné »
        results = [leg.get("result") for leg in c["legs"]]
        if "loss" in results:
            c["result"] = "loss"
        elif all(r in ("win", "push", "void", UNKNOWN) for r in results):
            c["result"] = UNKNOWN if UNKNOWN in results else ("win" if "win" in results else "void")   # sans jambe gagnée : rien à payer
        if c.get("result"):
            settled.append(c)
    store.save(fair.COMBOS_FILE, rows)
    return rows, settled


def record_combos(combos_sent, day):
    bets = store.load("bets.json", {"picks": [], "combos": []})
    for combo in combos_sent:
        if any(c["date"] == str(day) and c["tier"] == combo["tier"] for c in bets["combos"]):
            continue                                # relance du matin : un seul combiné par palier et par jour (le premier envoyé)
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
    if not picks and not combos and not fair_combo_line(fair.load_combos(), days):
        return "📊 BILAN\nAucun pari réglé pour l'instant." if detail else fmt.Html(
            "📊 <b>Bilan</b>\n\nAucun pari réglé pour l'instant : les paris du jour sont réglés le lendemain matin.")
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


def fair_combo_line(rows, days=None, today=None):
    """Combinés à cote juste réglés : « Combinés à vérifier : 2 gagnés sur 5 (sans mise : à comparer aux cotes de 1xBet) » ; '' si
    rien de réglé."""
    done = [r for r in rows if r.get("result") in ("win", "loss")]
    if days:
        cut = str((datetime.fromisoformat(str(today)) if today else datetime.now(timezone.utc)).date() - timedelta(days=days))
        done = [r for r in done if r["date"] >= cut]
    if not done:
        return ""
    w = sum(1 for r in done if r["result"] == "win")
    return f"Combinés à vérifier chez 1xBet : {w} gagné{'s' if w > 1 else ''} sur {len(done)} (sans mise, hors bankroll)."


def _gain(c):
    return c["stake"] * (c.get("odds_final", c["odds"]) - 1) if c["result"] == "win" else -c["stake"]


def _sum_txt(cs, html=True):
    """Résultat d'un groupe de combinés : en unités pour ceux de la bankroll, en devise pour ceux misés sur le budget du jour (« pas
    joué » et cote corrigée comptés) ; jamais mélangés : « +12 u et −1 000 FCFA ». `html` : devise échappée."""
    from . import money
    parts = []
    units = [c for c in cs if not c.get("cur")]
    if units or not cs:
        parts.append(fmt.signed(sum(map(_gain, units))))
    for cur in dict.fromkeys(c["cur"] for c in cs if c.get("cur")):
        parts.append(money.signed(sum(money.gain(c) or 0 for c in cs if c.get("cur") == cur), cur))
    txt = " et ".join(parts)
    return fmt.esc(txt) if html else txt


def _bilan_simple(days, rows, picks, combos):
    """Bilan en mots simples, par blocs séparés d'une ligne vide : combinés, paris envoyés, paris joués chez 1xBet, pronostics sans
    cote, fiabilité par marché."""
    sent = [r for r in picks if r.get("sent")]
    blocks = [[f"📊 <b>Bilan</b>{f' — {days} derniers jours' if days else ''}"]]
    if combos:
        w = sum(1 for c in combos if c["result"] == "win")
        b = [f"🎯 <b>Combinés</b> : {w} gagné{'s' if w > 1 else ''} sur {len(combos)} · résultat {fmt.b(_sum_txt(combos))}"]
        tiers = {}
        for c in combos:
            tiers.setdefault(fmt.tier_name(c["tier"]), []).append(c)
        b += [f"      {fmt.esc(tier)} : {sum(1 for c in cs if c['result'] == 'win')} sur {len(cs)} ({_sum_txt(cs)})"
              for tier, cs in tiers.items()]
        blocks.append(b)
    if sent:
        w = sum(1 for r in sent if r["result"] == "win")
        blocks.append([f"✅ <b>Paris envoyés</b> : {w} gagné{'s' if w > 1 else ''} sur {len(sent)} ({fmt.pct(w / len(sent))})"])
    from . import money
    bets_ = store.load("bets.json", {"combos": []})
    ml = money.summary_lines(bets_, rows, fair.load_combos())
    if ml:
        blocks.append(["💰 <b>Tes mises</b> " + fmt.i("(tout ce que Kelly a proposé, sauf « pas joué »)")] + [f"• {fmt.esc(x)}" for x in ml])
    pl = played.summary_lines(bets_, rows, fair.load_combos(), days)
    if money.active():                              # le résultat en argent est au-dessus : ne garder que l'écart de cote 1xBet
        pl = [x for x in pl if not x.startswith("Joués chez 1xBet")]
        if pl:
            blocks.append([f"💵 {fmt.esc(x)}" for x in pl])
    elif pl:
        blocks.append([f"💵 <b>{fmt.esc(pl[0])}</b>"] + [f"      {fmt.esc(x)}" for x in pl[1:]])
    free = [x for x in (fair_line(fair.load(), days), fair_combo_line(fair.load_combos(), days)) if x]
    if free:
        blocks.append([f"🎲 {fmt.esc(x)}" for x in free])
    cl, ll = calib.lines(), calib.league_lines(limit=3)
    if cl:
        blocks.append(["📏 <b>Fiabilité de Kelly</b> " + fmt.i("(pronostics réglés, depuis le début)")] + [f"• {fmt.esc(x)}" for x in cl])
    if ll:
        blocks.append(["🏟 <b>Ses meilleurs championnats</b>"] + [f"• {fmt.esc(x)}" for x in ll])
    pending = sum(1 for r in rows if r.get("result") is None)
    foot = ([f"⏳ {pending} pari{'s' if pending > 1 else ''} en attente de résultat"] if pending else [])
    blocks.append(foot + [fmt.i("Chiffres techniques : bouton « Plus de détails », ou écris « bilan détail »")])
    return fmt.Html("\n\n".join("\n".join(b) for b in blocks))


def _bilan_detail(bets, days, rows, picks, combos):
    sent = [r for r in picks if r.get("sent")]
    ghost = [r for r in picks if not r.get("sent")]
    lines = [f"📊 BILAN{f' ({days} derniers jours)' if days else ''}",
             _block("Envoyés", sent), _block("Suivis en silence", ghost)]
    lines += played.summary_lines(bets, rows, fair.load_combos(), days, detail=True)
    tiers = {}
    for c in combos:
        tiers.setdefault(c["tier"].split(" (")[0], []).append(c)
    for tier, rows_ in tiers.items():
        nn = len(rows_); ww = sum(1 for r in rows_ if r["result"] == "win")
        lines.append(f"{tier} : {ww}/{nn} | {_sum_txt(rows_, html=False)}")      # unités ou devise (cote corrigée, « pas joué »)
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
    fc = fair_combo_line(fair.load_combos(), days)
    if fc:
        lines.append(fc)
    cl = calib.lines(detail=True)
    if cl:
        lines.append("Fiabilité par marché (pronostics sans cote, depuis le début) :")
        lines += [f"• {x}" for x in cl]
    ll = calib.league_lines()
    if ll:
        lines.append("Fiabilité par championnat (du plus fiable au moins fiable) :")
        lines += [f"• {x}" for x in ll]
    return "\n".join(lines)
