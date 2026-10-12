"""Règlement des paris (résultats réels) et bilan."""
import sys
from datetime import datetime, timedelta, timezone

from . import calib, config as C, ensemble, fair, fmt, learn, odds, played, players, sharpgap, stats, store, track
from .sources import fmmatch, footballdata as fd, fotmob, names, s365

GOAL_MARKETS = ("h2h", "double_chance", "btts", "spreads", "totals", "team_totals_home", "team_totals_away")
FD_MARKETS = ("alternate_totals_corners", "alternate_totals_cards")     # réglables avec les CSV football-data
UNKNOWN = "unknown"                                        # match joué mais jambe invérifiable (« non vérifiable ») : ni gagné ni perdu
# Marchés réglés avec les statistiques d'une équipe (fstats["side"]) : tirs, tirs cadrés, fautes, touches, hors-jeu, corners d'UNE équipe (le total du match :
# alternate_totals_corners) et cartons JAUNES du match (1xBet ne compte que les jaunes, jamais les rouges : stat « yellow » ; « yel » chez API-Football).
SHOT_MARKETS = tuple(f"team_{k}_{s}" for k in ("shots", "sot", "fouls", "throws", "offsides") for s in ("total", "home", "away")) \
    + ("team_corners_home", "team_corners_away", "team_yellow_total")
ALIAS = {"yellow": "yel"}          # nom de la stat chez API-Football (stats._team_values)


def _side_value(sd, fld):
    """Tirs (« shots »), tirs cadrés (« sot »), corners, jaunes... d'une équipe d'après fstats["side"][côté] ; None si la source ne les donne pas (null chez API-Football)."""
    if not sd:
        return None
    for k in dict.fromkeys((fld, ALIAS.get(fld))):
        if k and k not in (sd.get("nul") or []) and sd.get(k) is not None:
            return sd[k]
    return None


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


def _player_team(b):
    """Équipe du joueur d'un pari player_* (identifiant API-Football ou nom) quand le pari la porte (b["team_id"], b["team"]), sinon None : players.stat ne règle alors
    un joueur que s'il est le seul de ce nom dans le match (jamais au hasard entre deux homonymes)."""
    return b.get("team_id") if b.get("team_id") is not None else b.get("team")


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
    totals = {"totals": h + a, "team_totals_home": h, "team_totals_away": a}
    if fstats:
        for mk, k in (("alternate_totals_corners", "corners"), ("alternate_totals_cards", "cards")):
            if fstats.get(k) is not None:
                totals[mk] = fstats[k]
        field = {"player_shots": "shots", "player_shots_on_target": "shots_on_target",
                 "player_fouls": "fouls"}.get(m)
        if field:
            v = players.stat(fstats, b["who"] or "", field, team=_player_team(b))
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
        v = players.stat(fstats, b.get("who") or "", field, team=_player_team(b)) if field else None
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


def fd_stats(m):
    """Stats d'un match des CSV football-data, au format du règlement (settle_pick) : totaux, et par équipe tirs, tirs cadrés, corners, fautes,
    jaunes SEULS (fd.match_stats : jaunes + rouges pour les cartons)."""
    out = fd.match_stats(m)
    side = out.setdefault("side", {"home": {}, "away": {}})
    for key, h, a in (("corners", "hc", "ac"), ("yellow", "hy", "ay"), ("fouls", "hf", "af")):
        if m.get(h) is not None and m.get(a) is not None:
            side["home"][key], side["away"][key] = m[h], m[a]
            if key == "yellow":
                out["yellow"] = m[h] + m[a]
    return out


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
        return fd_stats(m) if m else None

    def fotmob_score(self, b):
        """Score FotMob (fotmob_score) ; le fichier de profils n'est lu qu'une fois par exécution."""
        if self._fm is None:
            try:
                self._fm = fotmob.profiles().get("leagues") or {}
            except Exception as e:                    # noqa: BLE001 - facultatif
                print(f"[settle] FotMob (scores) ignoré : {e}", file=sys.stderr)
                self._fm = {}
        return fotmob_score(b, self._fm)


def _has_stat(b, fst):
    """`fst` donne-t-il la valeur dont le marché de `b` a besoin (corners / cartons du match, ou la stat d'une équipe) ?"""
    if b["market"] in FD_MARKETS:
        return (fst or {}).get("corners" if "corners" in b["market"] else "cards") is not None
    if b["market"] in SHOT_MARKETS:
        return shot_value(b["market"], fst or {}) is not None
    return bool(fst)


def match_stats(b, fdata, stats_for=None):
    """Totaux du match d'un pari hors buts (corners, cartons, tirs, stats joueurs) : API-Football d'abord, puis les CSV
    football-data si API-Football est absent ou son quota épuisé. None pour un marché de buts. fstats["side"] : tirs et
    tirs cadrés de chaque équipe (marchés par équipe)."""
    if b["market"] in GOAL_MARKETS:
        return None
    soccer, fstats = b["group"] == "soccer", None
    if b.get("free"):                                  # suivi en silence pour la fiabilité (fair.record) : jamais de requête
        if not soccer:
            return None
        if stats_for:
            return stats_for(b)
        first = None                                   # mémoires des passages horaires (365scores, FotMob : frais, sans requête), puis les CSV
        for name, read in (("365scores", lambda: s365._match_stats(b, net=False)), ("FotMob", lambda: fmmatch.memory_stats(b)),
                           ("football-data", lambda: fdata.stats(b))):
            try:
                got = read()
            except Exception as e:                     # noqa: BLE001 - facultatif
                print(f"[settle] {name} (mémoire) ignoré : {type(e).__name__}: {e}", file=sys.stderr)
                got = None
            if got and _has_stat(b, got):
                return got
            first = first or got
        return first
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
    if soccer and (b["market"] in SHOT_MARKETS and shot_value(b["market"], fstats or {}) is None
                   or b["market"] in FD_MARKETS and (fstats or {}).get(need) is None):
        try:                                           # ligues sans FotMob détaillé : stats du match chez 365scores
            fstats = s365.match_stats(b) or fstats
        except Exception as e:                         # noqa: BLE001 - facultatif
            print(f"[settle] 365scores ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    return fstats


def settle_pending(now=None, scores_by_sport=None, stats_for=None):
    """Règle les paris suivis passés (envoyés d'abord). Le foot se règle gratuitement : API-Football d'abord,
    puis les CSV football-data en cache (buts, corners, cartons, quand API-Football est absent ou son quota
    épuisé) ; /scores de The Odds API (2 crédits par ligue, budgétés) ne sert que de dernier repli pour les
    marchés de buts, sans STATS_API_KEY, ou pour un sport hors foot. Un pari sans score après WAIT_DAYS jours (buts),
    WAIT_DAYS_STATS pour corners et cartons (football-data se met à jour ~2 fois par semaine), ou dont le match a un score mais dont les stats
    restent introuvables, devient « unknown » (non vérifiable : jamais compté gagné ni perdu, jamais « annulé », et un combiné qui le contient
    devient « unknown » sans toucher la bankroll). `scores_by_sport` / `stats_for` : injectables (tests)."""
    now = now or datetime.now(timezone.utc)
    rows = track.load()
    bets = store.load_doc("bets.json", {"picks": [], "combos": []})
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
                b["result"] = UNKNOWN              # jamais de score après WAIT_DAYS : non vérifiable (comme settle_fair_combos), JAMAIS « void » qui
                settled.append(b)                  # rendrait un combiné « gagné » à une cote recalculée sans cette jambe
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
            combo["odds_final"] = 1.0              # jambes annulées / remboursées : cote recalculée, sur la MÊME base de prix que la cote du combiné
            xl = {l.get("key"): l.get("xodds") for l in combo.get("xlegs") or []}           # combiné aux cotes 1xBet : celles de l'envoi
            for k in combo["keys"]:
                if by_key[k]["result"] == "win":
                    # sinon la cote de référence de la jambe (row["odds"]) : la ligne de suivi peut avoir pris une cote 1xBet depuis (_upgrade_single)
                    combo["odds_final"] *= (xl.get(k) if xl else None) or by_key[k]["odds"]
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
    calib.add(rows, now.date())                     # cumul par famille, sens et tranche, vieilli avec le temps (fair.json : 10 jours)
    store.save(fair.FILE, rows)
    return rows, settled


def _cached_fstats(b):
    """Statistiques d'un match d'après le cache d'API-Football seulement (stats.cached_stats), au format de match_stats ; None sinon. Aucune requête de
    statistiques de plus : celles déjà payées en quota par le règlement des paris et des pronostics servent aussi au journal de l'ensemble."""
    if not stats.enabled():
        return None
    fx = stats.find_fixture(b["commence"][:10], b["home"], b["away"], kickoff=b["commence"])
    cur = stats.cached_stats(fx["id"]) if fx else None
    if not cur:
        return None
    teams = cur.get("teams") or {}
    return dict(cur, side={"home": teams.get(str(fx["home_id"])), "away": teams.get(str(fx["away_id"]))})


def settle_ensemble(now=None, stats_for=None):
    """Règle le journal des avis de l'ensemble (data/ensemble_log.json) avec le règlement gratuit des pronostics (_settle_fair_row : API-Football, football-data,
    FotMob), puis apprend des lignes réglées (ensemble.learn : poids appris par famille, idempotent par clé). Les statistiques d'un match (corners, cartons,
    tirs...) viennent des caches seulement (API-Football jamais redemandé ici : _cached_fstats, football-data, FotMob) ; une ligne dont le résultat reste introuvable
    devient « unknown » ou « void » comme un pronostic et n'apprend rien. -> (lignes réglées, lignes apprises) ; ([], 0) quand le journal est éteint."""
    if not ensemble.logging_on():
        return [], 0
    now = now or datetime.now(timezone.utc)
    fdata = FootballData()
    return ensemble.settle_log(lambda r: _settle_fair_row(r, now, fdata, stats_for or _cached_fstats))


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
    calib.add_combo_legs([leg for c in rows for leg in c.get("legs") or []], now.date())     # prudence par jambe des combinés (combos.leg_discount)
    store.save(fair.COMBOS_FILE, rows)
    return rows, settled


def _locked_in(c):
    """Combiné que la suite ne doit plus changer : noté joué (le ticket est placé) ou déjà réglé."""
    return bool(c.get("played")) or c.get("result") is not None


def record_combos(combos_sent, day, replace=False):
    """Enregistre les combinés réels envoyés (bets.json), un seul par palier et par jour. Relance du matin (`replace`, run_daily) : un palier déjà
    enregistré dont les paris sont les mêmes (renvoyé tel quel, voir app._resend_real) garde sa ligne, avec la mise du nouvel envoi si le combiné n'est pas noté ;
    un palier reconstruit avec d'autres paris REMPLACE la ligne seulement si elle n'est ni notée ni réglée (le suivi, les notes et le règlement restent ceux du
    combiné effectivement envoyé en dernier). Sans `replace`, le premier envoyé reste."""
    bets = store.load_doc("bets.json", {"picks": [], "combos": []})
    for combo in combos_sent:
        old = next((c for c in bets["combos"] if c["date"] == str(day) and c["tier"] == combo["tier"]), None)
        if old is None:
            bets["combos"].append(combo | {"date": str(day), "result": None})
        elif replace and not _locked_in(old):
            if set(old.get("keys") or []) == set(combo["keys"]):
                for k in ("stake", "cur"):
                    old.pop(k, None)
                old.update({k: combo[k] for k in ("stake", "cur") if k in combo})
            else:
                bets["combos"][bets["combos"].index(old)] = combo | {"date": str(day), "result": None}
    store.save("bets.json", bets)


def _rate(rows):
    n = sum(1 for r in rows if r["result"] in ("win", "loss"))
    w = sum(1 for r in rows if r["result"] == "win")
    ret = sum(((r.get("xodds") or r["odds"]) - 1) if r["result"] == "win" else (-1 if r["result"] == "loss" else 0) for r in rows)
    return n, w, (ret / n * 100 if n else 0.0)


def _block(name, rows):
    """« Envoyés · 11/23 (48 %) · rendement −14 % · CLV +1,6 % » (texte brut, virgules françaises)."""
    n, w, roi = _rate(rows)
    if not n:
        return f"{name} · aucun réglé"
    txt = f"{name} · {w}/{n} ({fmt.pct(w / n)}) · rendement {fmt.signed(round(roi), ' %')}"
    clv = [r["clv"] for r in rows if r.get("clv") is not None]
    if clv:
        txt += f" · CLV {fmt.signed(sum(clv) / len(clv) * 100, ' %')}"
    return txt


def bilan(bets, days=None, rows=None, detail=False):
    """Bilan. Par défaut en mots simples (combinés gagnés, résultat en FCFA de tes paris notés, paris envoyés) ; `detail` : les chiffres
    techniques (rendement par mise, CLV, Brier, par marché)."""
    rows = track.load() if rows is None else rows
    picks = [r for r in rows if r.get("result") in ("win", "loss")]
    combos = [c for c in bets["combos"] if c.get("result") in ("win", "loss")]
    if days:
        cutoff = str((datetime.now(timezone.utc) - timedelta(days=days)).date())
        picks = [b for b in picks if b["date"] >= cutoff]
        combos = [c for c in combos if c["date"] >= cutoff]
    if not picks and not combos and not fair_combo_line(fair.load_combos(), days) and not _custom_done(fair.load_combos(), days):
        return fmt.Html("🔬 <b>Bilan détaillé</b>\n\nAucun pari réglé pour l'instant.") if detail else fmt.Html(
            "📊 <b>Bilan</b>\n\nAucun pari réglé pour l'instant : les paris du jour sont réglés le lendemain matin.")
    return _bilan_detail(bets, days, rows, picks, combos) if detail else _bilan_simple(days, rows, picks, combos)


def fair_line(rows, days=None, today=None, short=False):
    """Pronostics sans cote réglés, en mots simples : « Pronostics sans cote : 5 gagnés sur 8 (62 %), Kelly en annonçait 64 % en
    moyenne. » ('' si rien de réglé). `short` (bilan) : « Pronostics sans cote : 5 gagnés sur 8 (62 % réalisés, 64 % annoncés) »."""
    done = [r for r in rows if r.get("result") in ("win", "loss")]
    if days:
        cut = str((datetime.fromisoformat(str(today)) if today else datetime.now(timezone.utc)).date() - timedelta(days=days))
        done = [r for r in done if r["date"] >= cut]
    if not done:
        return ""
    w = sum(1 for r in done if r["result"] == "win")
    said = fmt.pct(sum(r["prob"] for r in done) / len(done))
    if short:
        return f"Pronostics sans cote : {w} gagné{'s' if w > 1 else ''} sur {len(done)} ({fmt.pct(w / len(done))} réalisés, {said} annoncés)"
    return (f"Pronostics sans cote : {w} gagné{'s' if w > 1 else ''} sur {len(done)} ({fmt.pct(w / len(done))}), Kelly en "
            f"annonçait {said} en moyenne.")


def fair_combo_line(rows, days=None, today=None, short=False):
    """Combinés à cote juste réglés : « Combinés à vérifier : 2 gagnés sur 5 (sans mise : à comparer aux cotes de 1xBet) » ; '' si
    rien de réglé. Les combinés sur mesure que l'utilisateur joue (« ✅ Je le joue », marque « custom ») n'y sont pas : il les choisit lui-même,
    ils ne mesurent pas les combinés du matin de Kelly (ils sont dans le bilan en argent et celui des paris joués)."""
    done = [r for r in rows if r.get("result") in ("win", "loss") and not r.get("custom")]
    if days:
        cut = str((datetime.fromisoformat(str(today)) if today else datetime.now(timezone.utc)).date() - timedelta(days=days))
        done = [r for r in done if r["date"] >= cut]
    if not done:
        return ""
    w = sum(1 for r in done if r["result"] == "win")
    if short:                                       # bilan : « Combinés à vérifier : 6 gagnés sur 20 »
        return f"Combinés à vérifier : {w} gagné{'s' if w > 1 else ''} sur {len(done)}"
    return (f"Combinés à vérifier chez 1xBet : {w} gagné{'s' if w > 1 else ''} sur {len(done)} "
            "(le résultat en argent ne compte que ceux que tu as notés).")


def _custom_done(rows, days=None):
    """Combinés sur mesure joués (notés, pas retirés par l'ancien « ❌ Pas joué ») et réglés : ils comptent dans le bilan en argent et celui
    des paris joués, pas dans fair_combo_line."""
    done = [r for r in rows if r.get("custom") and r.get("result") in ("win", "loss") and not r.get("skipped") and r.get("played")]
    if days:
        cut = str((datetime.now(timezone.utc) - timedelta(days=days)).date())
        done = [r for r in done if r["date"] >= cut]
    return done


def _gain(c):
    """Gain d'un combiné de bets.json : à la cote et à la mise notées quand il y en a (comme bankroll.played_gain), sinon à la mise de
    sa ligne (ancienne mise en unités : calcul interne de la bankroll, jamais affiché)."""
    if c.get("played") and c["result"] in ("win", "loss"):
        from .bankroll import played_gain
        return played_gain(c)
    return c["stake"] * (c.get("odds_final", c.get("xodds") or c["odds"]) - 1) if c["result"] == "win" else -c["stake"]


def _sum_txt(cs, html=True):
    """Résultat en argent des paris NOTÉS et réglés parmi `cs` (money.gain : cote et mise notées) : « +2 000 FCFA » ; '' si aucun n'est noté
    et réglé (un combiné que Kelly a seulement conseillé ne compte pas). `html` : devise échappée."""
    from . import money
    by = {}
    for c in cs:
        g = money.gain(c)
        if g is not None:
            by.setdefault(c.get("cur") or money.cur(), []).append(g)
    txt = " et ".join(money.signed(round(sum(g)), cur) for cur, g in by.items())      # arrondi au franc
    return fmt.esc(txt) if html else txt


def _reliability_lines():
    """Fiabilité de Kelly par famille (pastilles du status : 🟢 / 🔴 / ⏳, réalisé / annoncé) et par championnat (les 2 plus fiables et le moins
    fiable, au plus 3), en texte brut : ([familles], [championnats])."""
    from . import status
    d = calib.load()
    fams = sorted(((f, a) for f, a in d.items() if not f.startswith("_") and isinstance(a, list) and len(a) == 3 and a[0] >= calib.SHOW_MIN),
                  key=lambda x: -x[1][0])
    out = []
    for f, (n, w, s_) in fams:
        dot, txt = status._state(n, w, s_, calib.min_need(f), calib.family_k(f, d, real_only=True))
        out.append(f"{dot} {calib.LABELS.get(f, f).capitalize()} · {txt}")
    rows = calib.league_rows(d)
    pick = rows if len(rows) <= 3 else rows[:2] + rows[-1:]
    lg = []
    for name, n, won, said in pick:
        mood = status._mood(won, said)
        lg.append(f"{'🔴' if mood == 'trop confiante' else '🟢'} {name} · {calib.count(n)} · {mood} ({fmt.pct(won)} / {fmt.pct(said)})")
    return out, lg


def _bilan_simple(days, rows, picks, combos):
    """Bilan en carte (HTML), par blocs séparés d'une ligne vide : ton argent (paris notés, arrondis, jauge de la mise récupérée), les combinés du
    matin par palier, paris envoyés / pronostics / combinés à vérifier / paris 1xBet en retard, tes cotes chez 1xBet, la fiabilité de Kelly (pastilles)
    et par championnat, puis les paris en attente."""
    from . import money
    sent = [r for r in picks if r.get("sent")]
    blocks = [[f"📊 <b>Bilan</b>{f' — {days} derniers jours' if days else ''}"]]
    bets_ = store.load_doc("bets.json", {"combos": []})
    ml = money.summary_lines(bets_, rows, fair.load_combos())
    if ml:
        blocks.append(["💰 <b>Ton argent</b> " + fmt.i("(paris notés)")] + [fmt.esc(x) for x in ml])
    if combos:
        w = sum(1 for c in combos if c["result"] == "win")
        b = [f"🎯 <b>Combinés du matin</b> : {w} gagné{'s' if w > 1 else ''} sur {len(combos)}"]
        tiers = {}
        for c in combos:
            tiers.setdefault(fmt.tier_name(c["tier"]), []).append(c)
        for tier, cs in tiers.items():
            mine = _sum_txt(cs)
            b.append(f"{fmt.esc(tier)} · {sum(1 for c in cs if c['result'] == 'win')}/{len(cs)}" + (f" · tes paris {mine}" if mine else ""))
        blocks.append(b)
    counts = []
    if sent:
        w = sum(1 for r in sent if r["result"] == "win")
        counts.append(f"✅ Paris envoyés : {w} gagné{'s' if w > 1 else ''} sur {len(sent)} ({fmt.pct(w / len(sent))})")
    counts += [f"🎲 {fmt.esc(x)}" for x in (fair_line(fair.load(), days, short=True), fair_combo_line(fair.load_combos(), days, short=True)) if x]
    gl = sharpgap.summary_line(rows, days)
    if gl:
        counts.append(f"💰 {fmt.esc(gl)}")
    if counts:
        blocks.append(counts)
    pl = played.summary_lines(bets_, rows, fair.load_combos(), days)
    if ml:                                          # le résultat en argent est au-dessus : ne garder que l'écart de cote 1xBet
        pl = [x for x in pl if not x.startswith("Joués chez 1xBet")]
    if pl:
        blocks.append(["💵 <b>Tes cotes chez 1xBet</b>"] + [fmt.i(fmt.esc(x.removeprefix("💵 "))) for x in pl])
    fam, lg = _reliability_lines()
    if fam:
        blocks.append(["📏 <b>Fiabilité de Kelly</b> " + fmt.i("(réalisé / annoncé)")] + [fmt.esc(x) for x in fam])
    if lg:
        blocks.append(["🏟 <b>Par championnat</b>"] + [fmt.esc(x) for x in lg])
    pending = sum(1 for r in rows if r.get("result") is None)
    blocks.append([(f"⏳ {pending} pari{'s' if pending > 1 else ''} en attente · " if pending else "")
                   + fmt.i("écris « bilan détail » pour les chiffres techniques")])
    return fmt.Html("\n\n".join("\n".join(b) for b in blocks))


def _bilan_detail(bets, days, rows, picks, combos):
    """Bilan détaillé en carte (HTML) : ton argent (paris notés, écarts de cote 1xBet), la qualité des choix de Kelly (rendement, CLV, Brier, expliqués),
    les combinés du matin, par marché, le suivi gratuit, qui Kelly écoute (ensemble), sa fiabilité (pastilles, facteur de correction, sens et niveau de
    chances) et par championnat, puis les paris en attente."""
    from . import status
    e = fmt.esc
    sent = [r for r in picks if r.get("sent")]
    ghost = [r for r in picks if not r.get("sent")]
    blocks = [[f"🔬 <b>Bilan détaillé</b>{f' — {days} derniers jours' if days else ''}"]]
    pl = played.summary_lines(bets, rows, fair.load_combos(), days, detail=True)
    if pl:
        blocks.append(["💰 <b>Ton argent</b> " + fmt.i("(paris notés)")] + [e(x) for x in pl])
    q = [e("✅ " + _block("Envoyés", sent)), e("👻 " + _block("Suivis en silence", ghost))]
    if picks:
        brier = sum((r["prob"] - (1.0 if r["result"] == "win" else 0.0)) ** 2 for r in picks) / len(picks)
        q.append(f"🎯 Brier {fmt.num(brier, 3)} " + fmt.i("(0,25 = hasard, plus bas = mieux)"))
    q.append(fmt.i("ℹ️ CLV positif : Kelly prend de meilleures cotes que la cote finale, le meilleur signe sur la durée. Rendement : gain moyen par mise."))
    blocks.append(["🎯 <b>Qualité des choix de Kelly</b>"] + q)
    if combos:
        tiers = {}
        for c in combos:
            tiers.setdefault(fmt.tier_name(c["tier"]), []).append(c)
        b = ["🧩 <b>Combinés du matin</b>"]
        for tier, rows_ in tiers.items():
            mine = _sum_txt(rows_)
            b.append(f"{e(tier)} · {sum(1 for r in rows_ if r['result'] == 'win')}/{len(rows_)}" + (f" · tes paris {mine}" if mine else ""))
        blocks.append(b)
    if sent:
        markets = {}
        for r in sent:
            markets.setdefault(r["market"], []).append(r)
        b = ["📈 <b>Par marché</b> " + fmt.i("(paris envoyés)")]
        for m, rows_ in sorted(markets.items(), key=lambda kv: -len(kv[1])):
            nn, ww, rr = _rate(rows_)
            if nn:
                b.append(e(f"{C.MARKET_LABELS.get(m, m).capitalize()} · {ww}/{nn} ({fmt.pct(ww / nn)}) · rendement {fmt.signed(round(rr), ' %')}"))
        blocks.append(b)
    free = [x for x in (fair_line(fair.load(), days, short=True), fair_combo_line(fair.load_combos(), days, short=True), sharpgap.summary_line(rows, days)) if x]
    if free:
        blocks.append(["🎲 <b>Suivi gratuit</b>"] + [e(x) for x in free])
    el = ensemble.lines(short=True)
    if el:
        blocks.append(["⚖️ <b>Qui Kelly écoute</b> " + fmt.i("(avis de Kelly · part de Pinnacle · résultats)")] + [e(x) for x in el]
                      + [fmt.i("ℹ️ ↑ / ↓ : Kelly pèse plus ou moins que prévu au départ.")])
    fam, _ = _reliability_lines()
    if fam:
        d = calib.load()
        fams = [f for f, a in sorted(((f, a) for f, a in d.items() if not f.startswith("_") and isinstance(a, list) and len(a) == 3
                                      and a[0] >= calib.SHOW_MIN), key=lambda x: -x[1][0])]
        b = ["📏 <b>Fiabilité de Kelly</b> " + fmt.i("(réalisé / annoncé · correction)")]
        for line, f in zip(fam, fams):
            k = calib.family_k(f, d)
            head, sep, tail = line.partition(" → ")
            if line[0] == "⏳":                    # pas encore assez de vrais résultats : la correction est celle du rejeu, s'il y en a un
                tag = f" · ×{fmt.num(k)} (rejeu)" if f in calib.boot_acc() and abs(k - 1) >= 0.005 else ""
            else:
                tag = f" · ×{fmt.num(k)}" if k is not None else ""
            b.append(e(head + tag + (sep + tail if sep else "")))
        b += [e(f"• {x}") for x in calib.cell_lines()]
        blocks.append(b)
    from . import replay
    rl = replay.status_line()
    if rl:
        blocks.append(["📚 <b>Rejeu de la saison passée</b> " + fmt.i("(point de départ des corrections, tes vrais résultats passent devant)"),
                       e(rl.removeprefix("📚 Rejeu de la saison passée : "))] + [e(x) for x in replay.family_lines()])
    lg = [f"{'🔴' if status._mood(won, said) == 'trop confiante' else '🟢'} {name} · {calib.count(n)} · {status._mood(won, said)} "
          f"({fmt.pct(won)} / {fmt.pct(said)})" for name, n, won, said in calib.league_rows()]
    if lg:
        blocks.append(["🏟 <b>Par championnat</b> " + fmt.i("(du plus fiable au moins fiable)")] + [e(x) for x in lg])
    pending = sum(1 for r in rows if r.get("result") is None)
    if pending:
        blocks.append([f"⏳ {pending} pari{'s' if pending > 1 else ''} en attente de résultat"])
    return fmt.Html("\n\n".join("\n".join(b) for b in blocks))

