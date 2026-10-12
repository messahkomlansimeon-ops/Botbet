"""Fenêtre des stats (kelly/statswindow.py, python bot.py --track) : 1xBet n'ouvre les paris tirs / fautes / touches / hors-jeu que quelques heures avant le match ;
le passage horaire relit ceux des matchs qui commencent dans [45 min ; 4 h], les remet à la cote 1xBet, envoie UN « 🧮 COMBINÉ STATS » (code coupon, mise, boutons, suivi)
et les nouveaux 💎 stats, sans jamais tenir le verrou des données pendant les requêtes 1xBet (--own-lock), dans le budget de données.
Hors ligne : aucun réseau (faux relevé 1xBet, faux Telegram, faux coupon). Lancer : python -m tests.test_stats_window"""
import copy
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlparse

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-statswin-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-statswin-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-statswin-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("KELLY_OWN_LOCK", None)
os.environ["XBET_PROXY_FILE"] = os.path.join(os.environ["DATA_DIR"], "pas-de-fichier.env")
os.environ["XBET_PROXY"] = "http://user:secret@proxy.test:80"

import requests  # noqa: E402

from kelly import app, calib, chat, combos as CB, config as C, custom, fair, fmt, money, played, statswindow, store, telegram, xprice  # noqa: E402,F401
from kelly.sources import fmmatch, http, state, xbet  # noqa: E402

# Ces tests vérifient la règle d'avant (meilleure ligne au prix 1xBet sur la seule proba de Kelly), toujours celle des touches et des hors-jeu : familles
# de l'ensemble d'avant le 12/10. Le mélange avec 1xBet des familles de stats est testé dans tests/test_stats_ensemble.py.
C.ENSEMBLE_ACTIVE_FAMILIES = frozenset(("goals", "h2h", "dc", "btts", "team_goals"))

# la mécanique (bornes, fraîcheur, plafonds, combinés) est testée sur une fenêtre de 4 h relue chaque heure ; la fenêtre large par défaut (24 h, relue
# toutes les 3 h de loin) est testée dans tests/test_pending_stats.py
C.STATS_WINDOW_MAX_H, C.STATS_WINDOW_NEAR_H = 4, 4

DAY = "2026-10-09"
NOW = datetime(2026, 10, 9, 17, 5, tzinfo=timezone.utc)
KO = {"M1": "18:00", "M2": "19:30", "M3": "21:00", "M4": "20:00", "M5": "22:30", "M6": "17:30", "M7": "16:00",
      "M8": "22:00", "M9": "22:00", "M10": "22:00", "Z1": "22:00"}      # fenêtre de 17:05 : 17:50 à 21:05 ; M5 trop tard, M6 trop près, M7 commencé
FOULS, SHOTS_H, SOT, THROWS_H, THROWS_T, OFFS, SHOTS_T = ("team_fouls_total", "team_shots_home", "team_sot_total", "team_throws_home",
                                                          "team_throws_total", "team_offsides_total", "team_shots_total")
STAT_MARKETS = (FOULS, SHOTS_H, SOT, THROWS_H, THROWS_T, OFFS, SHOTS_T)
# prix 1xBet des sous-matchs stats OUVERTS : (match, marché) -> cote du « Under 9.5 » (ou {ligne: cote}) ; ce qui n'y est pas est fermé
PRICES = {("M1", FOULS): 1.85, ("M1", SHOTS_H): 1.80, ("M1", SOT): 1.70, ("M2", SHOTS_H): 1.90, ("M3", THROWS_T): {9.5: 1.50, 10.5: 1.95},
          ("M4", OFFS): 1.85, ("M5", FOULS): 1.85, ("M5", SHOTS_T): 1.80, ("M8", SHOTS_H): 1.90, ("M9", OFFS): 1.80, ("M10", FOULS): 1.85}


MAIN_PRICES = {"totals": {("totals", "Over", 2.5): 1.9, ("totals", "Under", 2.5): 1.8}, "btts": {("btts", "Yes", None): 1.8, ("btts", "No", None): 1.95}}


def prono(mid, market, name="Under", point=9.5, p=0.66, odds_=None, **kw):
    c = {"key": f"{mid}|{market}||{point if point is not None else ''}|{name}", "id": mid, "sport_key": "soccer_epl", "home": f"H{mid}", "away": f"A{mid}",
         "event": f"H{mid} - A{mid}", "league": "Premier League", "commence": f"{DAY}T{KO[mid]}:00Z", "start": KO[mid], "market": market, "name": name,
         "point": point, "who": None, "group": "soccer", "pick": fmt.label(market, name, point, None, "soccer"), "prob": p, "p0": p, "fair": round(1 / p, 2),
         "min_odds": round(1 / p * 1.05, 2), "why": "x", "src": "football-data", "conf": 0.6, "n_min": 8, "xgid": 100 + int(mid[1:] or 0) if mid[1:].isdigit() else 999,
         "xnone": 1}
    if odds_:
        c.pop("xnone")
        c |= {"xodds": odds_, "xev": round(p * odds_ - 1, 4), "xodds0": odds_, "xat": "04:00", "xref": [700, 12, point]}
    return c | kw


def stat_pronos(p=0.66):
    ps = [prono("M1", FOULS, p=p), prono("M1", SHOTS_H, p=p), prono("M1", SOT, p=p), prono("M2", SHOTS_H, p=p), prono("M2", THROWS_H, p=p),
          prono("M3", THROWS_T, p=p, alts=[["Under", 9.5, p, None], ["Under", 10.5, min(0.9, p + 0.08), None]]), prono("M4", OFFS, p=p),
          prono("M5", FOULS, p=p), prono("M5", SHOTS_T, p=p), prono("M6", FOULS, p=p), prono("M7", FOULS, p=p)]
    return ps


def setup(p=0.66, extra=(), announced=True, combos=None):
    """Journée de départ : un pronostic non stats déjà chiffré (1xBet a lu la journée à 04:00), tous les pronostics stats « fermés » (xnone) à cause des sous-matchs
    pas encore ouverts à 04:00."""
    store.save(calib.FILE, {})
    calib._memo.clear()
    C.QUIET_HOURS = "off"
    for name in (statswindow.FILE, "bets.json", "tracked.json", fair.FILE, fair.COMBOS_FILE, app.COMBO_UPDATES, "xbet_usage.json", "ensemble_log.json", "outbox.json",
                 custom.FILE):
        store.save(name, [] if name in (fair.FILE, fair.COMBOS_FILE, "tracked.json", "outbox.json") else {})
    store.save("bets.json", {"picks": [], "combos": []})
    store.save(state.FILE, {})
    money.set_daily(10000)
    morning = [prono("M1", "totals", "Over", 2.5, 0.64, 1.9), prono("M2", "btts", "Yes", None, 0.6, 1.8)]
    today = {"day": DAY, "picks": [], "pronos": morning + stat_pronos(p) + list(extra), "xbet_at": "04:00"}
    if announced:
        today["announced"] = ["M1|totals", "M2|btts"]
    store.save("today.json", today)
    if combos:
        store.save(fair.COMBOS_FILE, combos)
    return today


class Fx:
    """Faux relevé 1xBet (xbet.fetch_day) : ne rend les cotes que des marchés DEMANDÉS (`wanted`) dont le sous-match est ouvert (PRICES), comme les vraies requêtes."""

    def __init__(self, prices=None):
        self.calls, self.prices = [], PRICES if prices is None else prices

    def __call__(self, events, deadline=None, wanted=None, **kw):
        self.calls.append({"ids": [e["id"] for _, e in events], "wanted": copy.deepcopy(wanted), "deadline": deadline, "kw": kw,
                           "events": [copy.deepcopy(e) for _, e in events]})
        out = {}
        for _, e in events:
            px, refs = {}, {}
            for m in (wanted or {}).get(e["id"], ()):
                if m in MAIN_PRICES:                                      # marchés du match (premier niveau de gameEvents) : toujours là
                    px |= MAIN_PRICES[m]
                    refs |= {k: [100 + int(e["id"][1:]), 1, k[2] or 0] for k in MAIN_PRICES[m]}
                got = self.prices.get((e["id"], m))
                for line, price in ((got if isinstance(got, dict) else {9.5: got}).items() if got else ()):
                    px[(m, "Under", line)], px[(m, "Over", line)] = price, 1.7
                    refs[(m, "Under", line)] = [900 + int(e["id"][1:]), 12, line]
            out[e["id"]] = {"gid": e["xgid"], "px": px, "refs": refs, "cid": 5}
        return out


def lock_free():
    try:
        with store.data_lock(wait=0):
            return True
    except store.LockTimeout:
        return False


class Net:
    calls = []

    @staticmethod
    def boom(*a, **k):
        Net.calls.append((a, k))
        raise AssertionError(f"accès réseau interdit : {a}")


def with_fakes(fn, coupon=True):
    """fn() avec faux Telegram, faux coupon et réseau interdit -> (résultat, messages envoyés, codes demandés [nombre de jambes])."""
    sent, coupons = [], []
    old = (telegram.send, xbet.save_coupon, requests.get, requests.post)
    telegram.send = lambda m, *a, **k: sent.append(m) or True
    xbet.save_coupon = lambda legs: coupons.append(len(legs)) or (f"CODE{len(coupons)}" if coupon else None)
    requests.get = requests.post = Net.boom
    try:
        return fn(), sent, coupons
    finally:
        telegram.send, xbet.save_coupon, requests.get, requests.post = old


def buttons(m):
    return [b.get("callback_data") or b.get("copy_text", {}).get("text") for row in m.markup["inline_keyboard"] for b in row]


def by_key():
    return {c["key"]: c for c in store.load("today.json", {})["pronos"]}


def snapshot():
    return {n: copy.deepcopy(store.load(n, None)) for n in ("bets.json", "tracked.json", fair.FILE)}


def window_msgs(sent):
    return [m for m in sent if "COMBINÉ STATS" in m], [m for m in sent if "Stats ouvertes chez 1xBet" in m]


# ---------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_selection_by_kickoff():
    """Fenêtre [45 min ; 4 h] autour du coup d'envoi ; matchs à pronostics stats sans cote fraîche, connus de 1xBet, pas jambes d'un combiné ; plus de pronostics d'abord."""
    setup()
    today = store.load("today.json", {})
    doc = statswindow.load(DAY)
    sel = statswindow.select(today["pronos"], NOW, doc)
    assert [m for m, _ in sel] == ["M1", "M2", "M4", "M3"], [m for m, _ in sel]       # 3 pronostics, 2, puis 1 + 1 (le plus proche d'abord : M4 à 20:00)
    assert {c["market"] for _, cs in sel for c in cs} <= set(STAT_MARKETS) and len(sel[0][1]) == 3
    assert statswindow.window_ids(today["pronos"], NOW) == {"M1", "M2", "M3", "M4"}
    # bornes exactes : 45 min incluses, 44 min exclues ; 4 h incluses, 4 h 01 exclues
    edge = lambda mid, ko: prono("M1", FOULS) | {"id": mid, "key": f"{mid}|e", "commence": ko}            # noqa: E731
    bounds = [edge("a", "2026-10-09T17:50:00Z"), edge("b", "2026-10-09T17:49:00Z"), edge("c", "2026-10-09T21:05:00Z"), edge("d", "2026-10-09T21:06:00Z")]
    assert [m for m, _ in statswindow.select(bounds, NOW, doc)] == ["a", "c"]
    # cote fraîche (lue il y a moins de 50 min) : pas relu ; plus vieille : relu ; la cote doit exister
    fresh = [c | {"xodds": 1.9, "xat": "16:30", "xev": 0.2} for c in today["pronos"] if c["id"] == "M1" and statswindow.is_stat(c)]
    assert statswindow.select(fresh, NOW, doc) == [], "M1 : toutes ses cotes sont fraîches (35 min)"
    stale = [c | {"xodds": 1.9, "xat": "15:55", "xev": 0.2} for c in today["pronos"] if c["id"] == "M1" and statswindow.is_stat(c)]
    assert [m for m, _ in statswindow.select(stale, NOW, doc)] == ["M1"], "75 min : à relire"
    assert [m for m, _ in statswindow.select([c | {"xat": "16:50", "xodds": None} for c in fresh], NOW, doc)] == ["M1"], "xat sans cote : pas une cote fraîche"
    # xat « demain » (24 h de décalage) : périmé
    assert statswindow.age_min({"xat": "18:00"}, NOW) == 23 * 60 + 5 and statswindow.age_min({"xat": "17:05"}, NOW) == 0 and statswindow.age_min({}, NOW) is None
    # sans id 1xBet, protégés (jambes d'un combiné), non stats : jamais
    nox = [c | {"xgid": None} for c in today["pronos"] if c["id"] == "M1"]
    assert statswindow.select(nox, NOW, doc) == []
    prot = {c["key"] for c in today["pronos"] if c["id"] == "M1"}
    assert [m for m, _ in statswindow.select(today["pronos"], NOW, doc, prot)] == ["M2", "M4", "M3"]
    assert [m for m, _ in statswindow.select([c for c in today["pronos"] if not statswindow.is_stat(c)], NOW, doc)] == []
    print("ok  sélection : fenêtre du coup d'envoi, cotes fraîches, matchs connus de 1xBet")


def test_selection_caps():
    """Plafonds de données : matchs par passage, relectures par jour et par match, passages sans réponse, délai entre deux relectures."""
    setup()
    pr = store.load("today.json", {})["pronos"]
    doc = statswindow.load(DAY)
    old = C.STATS_WINDOW_MAX_MATCHES
    try:
        C.STATS_WINDOW_MAX_MATCHES = 2
        assert [m for m, _ in statswindow.select(pr, NOW, doc)] == ["M1", "M2"], "au plus 2 matchs par passage : les plus proches du coup d'envoi (M1 18:00, M2 19:30)"
        C.STATS_WINDOW_MAX_MATCHES = 8
        d = copy.deepcopy(doc)
        d["reads"] = {"M1": {"n": C.STATS_WINDOW_TRIES}, "M2": {"miss": C.STATS_WINDOW_MISSES}, "M4": {"n": 1, "at": (NOW - timedelta(minutes=10)).isoformat()},
                      "M3": {"n": 1, "at": (NOW - timedelta(minutes=60)).isoformat()}}
        assert [m for m, _ in statswindow.select(pr, NOW, d)] == ["M3"], "M1 relu au maximum, M2 muet, M4 relu il y a 10 min"
        d["reads"] = {"M1": {"n": 20}, "M2": {"n": 20}}
        old_day = C.STATS_WINDOW_DAY_READS
        C.STATS_WINDOW_DAY_READS = 40
        d["reads"] = {"X": {"n": 38}}
        assert len(statswindow.select(pr, NOW, d)) == 2, "plafond du jour : 40 - 38 = 2 relectures possibles"
        d["reads"] = {"X": {"n": 30, "miss": 10}}
        assert statswindow.select(pr, NOW, d) == [], "plafond du jour atteint (relectures sans réponse comprises)"
        C.STATS_WINDOW_DAY_READS = old_day
    finally:
        C.STATS_WINDOW_MAX_MATCHES = old
    # estimation de données : (1 requête du match + 1 par famille ouverte) x 6,4 Ko
    assert round(statswindow.estimate_kb(8, 3)) == 205 and round(statswindow.estimate_kb(1, 0), 1) == 6.4
    print("ok  plafonds : matchs par passage, par jour, par match, délai")


def test_pass_reads_only_stat_subgames_and_reprices():
    """Un passage : relecture limitée aux marchés stats des matchs de la fenêtre, pronostics remis à la cote 1xBet (ligne choisie comprise), le reste de today.json intact."""
    setup()
    before = copy.deepcopy(store.load("today.json", {}))
    snap = snapshot()
    fx = Fx()
    ops = []
    old_log = app.log_opinions
    app.log_opinions = lambda rows, day, now=None: ops.append([c["key"] for c in rows]) or len(rows)
    try:
        rep, sent, coupons = with_fakes(lambda: app.run_stats_window(NOW, fetch=fx))
    finally:
        app.log_opinions = old_log
    assert Net.calls == [] and len(fx.calls) == 1, (Net.calls, len(fx.calls))
    call = fx.calls[0]
    assert sorted(call["ids"]) == ["M1", "M2", "M3", "M4"], "seuls les matchs de la fenêtre qui ont des pronostics stats"
    allm = {m for ms in call["wanted"].values() for m in ms}
    assert allm and allm <= set(STAT_MARKETS), "AUCUN marché non stats demandé (ni corners, ni buts) : seuls les sous-matchs stats sont relus"
    assert call["wanted"]["M1"] == {FOULS, SHOTS_H, SOT} and call["wanted"]["M2"] == {SHOTS_H, THROWS_H}
    assert 0 < call["deadline"] - time.monotonic() <= C.STATS_WINDOW_SECONDS + 1, "durée du passage plafonnée"
    now_by = by_key()
    for (mid, m), price in PRICES.items():
        if mid not in ("M1", "M2", "M3", "M4"):
            continue
        if mid == "M3":
            c = now_by["M3|team_throws_total||10.5|Under"]                  # la meilleure ligne à la cote 1xBet : 10,5 (1,95), pas 9,5 (1,50 : mal payé)
            assert c["xodds"] == 1.95 and c["point"] == 10.5 and "M3|team_throws_total||9.5|Under" not in now_by
        else:
            c = now_by[f"{mid}|{m}||9.5|Under"]
            assert c["xodds"] == price, (mid, m, c)
        assert c["xat"] == "17:05" and c["xref"][0] >= 900 and not c.get("xnone") and c["xodds0"] == c["xodds"], c
    assert now_by["M2|team_throws_home||9.5|Under"]["xnone"] == 1 and not now_by["M2|team_throws_home||9.5|Under"].get("xodds"), "sous-match fermé : toujours injouable"
    # tout le reste est identique : pronostics non stats, matchs hors fenêtre (M5 trop tard, M6 trop près, M7 commencé)
    moved = {"M3|team_throws_total||9.5|Under"}
    for c in before["pronos"]:
        if c["key"] in moved:
            continue
        if c["id"] in ("M1", "M2", "M3", "M4") and statswindow.is_stat(c):
            continue
        assert now_by[c["key"]] == c, c["key"]
    today = store.load("today.json", {})
    assert today["xbet_at"] == "17:05" and today["picks"] == before["picks"] and today["day"] == DAY
    assert len(today["pronos"]) == len(before["pronos"]) and [c["id"] for c in today["pronos"]] == [c["id"] for c in before["pronos"]], "même ordre, pas de doublon"
    after = snapshot()
    assert after["bets.json"] == snap["bets.json"] and after["tracked.json"] == snap["tracked.json"], "paris du jour, combinés et notes : jamais touchés par la relecture"
    assert after[fair.FILE][:len(snap[fair.FILE])] == snap[fair.FILE] and all(r["market"] in STAT_MARKETS for r in after[fair.FILE][len(snap[fair.FILE]):]), \
        "suivi gratuit : seulement de NOUVELLES lignes stats (les 💎 annoncés)"
    assert rep["selected"] == 4 and rep["read"] == 4 and rep["priced"] == 6, rep
    # suivi du jour : relectures faites par match
    doc = statswindow.load(DAY)
    assert doc["reads"]["M1"]["n"] == 1 and doc["reads"]["M1"]["priced"] is True and doc["reads"]["M4"]["n"] == 1 and doc["passes"] == 1
    assert ops and set(ops[0]) >= {c["key"] for c in now_by.values() if c.get("xat") == "17:05"}, "avis de l'ensemble journalisés pour les pronostics relus"
    print("ok  relecture : sous-matchs stats seulement, pronostics remis à la cote 1xBet, rien d'autre ne bouge")


def test_combo_sent_once_with_coupon_and_tracking():
    """UN « 🧮 COMBINÉ STATS » par passage : cote 5 à 7, un pari par match, code coupon, mise du budget, boutons, suivi ts5w1, et jamais envoyé deux fois."""
    setup()
    fx = Fx()
    rep, sent, coupons = with_fakes(lambda: app.run_stats_window(NOW, fetch=fx))
    cmb, lst = window_msgs(sent)
    assert len(cmb) == 1 and rep["combo"] == "ts5w1", (rep, [str(m)[:60] for m in sent])
    m = cmb[0]
    assert re.search(r"<b>🧮 COMBINÉ STATS — cote \d+,\d+</b>", m) and "CODE1" in m and "viennent de s'ouvrir" in m and "Cotes 1xBet de 17:05" in m, m
    assert coupons == [len(store.load(fair.COMBOS_FILE, [])[0]["legs"])] and coupons[0] >= 2
    assert "💰 Mise : <b>" in m and "FCFA" in m, "mise conseillée du budget du jour"
    rows = store.load(fair.COMBOS_FILE, [])
    assert len(rows) == 1
    row = rows[0]
    assert row["tier"] == "ts5w1" and row["date"] == DAY and 5.0 <= row["min_odds"] <= 7.0 and row["coupon"] == "CODE1" and row["at"] == "17:05"
    assert len({l["id"] for l in row["legs"]}) == len(row["legs"]) >= 2, "un pari par match"
    assert all(l["market"].startswith(C.STATS_WINDOW_PREFIX) and l["xodds"] and l["xref"] and l["result"] is None for l in row["legs"])
    assert row["stake"] > 0 and row["cur"] == "FCFA"
    prod = 1.0
    for l in row["legs"]:
        prod *= l["xodds"]
    assert abs(prod - row["min_odds"]) < 0.02, (prod, row["min_odds"])
    # boutons standard : « ✅ Je l'ai joué », « 🎟️ Vérifier ce combiné », « 📋 Copier le code »
    b = buttons(m)
    assert b == ["joue|vts5w1|2026-10-09", "xmaj|ts5w1|2026-10-09", "CODE1"], b
    assert "✅ Je l'ai joué — " in str(m.markup) and "Copier le code CODE1" in str(m.markup) and "Vérifier ce combiné" in str(m.markup)
    # suivi comme un combiné du matin : /joue, Mes paris, titre, palier
    assert CB.shots_title("ts5w1") == "🧮 COMBINÉ STATS" and CB.window_no("ts5w1") == 1 and CB.window_no("ts5") is None and CB.window_no("ts5wx") is None
    assert app._tier_range("ts5w1") == (5.0, 7.0, C.STATS_WINDOW_PREFIX) and app._tier_range("ts5")[:2] == (5.0, 7.0)
    t = played.find("vts5w1", DAY)
    assert t is not None and not isinstance(t, str) and t.ref == row["min_odds"]
    assert "Stats n°1 (17:05)" in str(played.todo(DAY)) and played._fair_name("ts5") == "Stats", played.todo(DAY)
    assert played.find("vts5w2", DAY).startswith("Pas de combiné « Stats n°2 »"), played.find("vts5w2", DAY)
    reply = played.command(["vts5w1", str(round(row["min_odds"], 2)).replace(".", ","), "1000"], DAY, NOW)
    assert str(reply).startswith("✅ Noté") and store.load(fair.COMBOS_FILE, [])[0]["played"]["stake"] == 1000.0, reply
    # 💎 stats annoncés dans UN message (un par match)
    assert len(lst) == 1 and rep["announced"] == len(lst[0].split("💎 ")) - 1 >= 3
    # deuxième passage, rien de neuf : ni relecture, ni combiné, ni message ; une seule ligne de suivi
    fx2 = Fx()
    rep2, sent2, coupons2 = with_fakes(lambda: app.run_stats_window(NOW + timedelta(minutes=1), fetch=fx2))
    assert fx2.calls == [] or all(not c["ids"] for c in fx2.calls), "toutes les cotes sont fraîches"
    assert sent2 == [] and coupons2 == [] and rep2["combo"] is None and len(store.load(fair.COMBOS_FILE, [])) == 1
    print("ok  un seul combiné stats : coupon, mise, boutons, suivi ts5w1, jamais renvoyé")


def test_dedup_same_legs_and_caps():
    """Jamais deux fois les mêmes jambes (jambes sûres : réutilisables, donc le même optimum reviendrait), au plus STATS_WINDOW_MAX_COMBOS par jour : ts5w1 puis ts5w2."""
    setup(p=0.74)                                    # jambes SÛRES (proba >= 0,68)
    rep, sent, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=Fx()))
    assert rep["combo"] == "ts5w1"
    row = store.load(fair.COMBOS_FILE, [])[0]
    t2 = NOW + timedelta(minutes=1)
    pool = app.upcoming(store.load("today.json", {})["pronos"], t2)
    usage = app._others_usage(store.load(fair.COMBOS_FILE, []), None, DAY, app._load_updates(DAY))
    cand = CB.fair_stats_window(pool, usage=usage, accept=lambda c: statswindow.in_window(c, t2) and statswindow.fresh(c, t2))
    assert cand and {l["key"] for l in cand[0]} == set(row["keys"]), "sans la garde, le même combiné serait renvoyé (jambes sûres réutilisables)"
    rep2, sent2, coupons2 = with_fakes(lambda: app.run_stats_window(t2, fetch=Fx()))
    assert rep2["combo"] is None and window_msgs(sent2)[0] == [] and coupons2 == [] and len(store.load(fair.COMBOS_FILE, [])) == 1
    # plafond du jour
    old = C.STATS_WINDOW_MAX_COMBOS
    try:
        setup(p=0.66, extra=[prono("M8", SHOTS_H), prono("M9", OFFS), prono("M10", FOULS)])
        C.STATS_WINDOW_MAX_COMBOS = 1
        r1, s1, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=Fx()))
        assert r1["combo"] == "ts5w1"
        # 18:05 : M8, M9, M10 (22:00) entrent dans la fenêtre, mais le plafond d'UN combiné par jour est atteint
        r2, s2, c2 = with_fakes(lambda: app.run_stats_window(NOW + timedelta(hours=1), fetch=Fx()))
        assert r2["read"] >= 3 and r2["combo"] is None and window_msgs(s2)[0] == [] and c2 == [] and len(store.load(fair.COMBOS_FILE, [])) == 1
        # plafond de 2 : le passage suivant envoie ts5w2 avec d'AUTRES matchs (les jambes risquées du 1er combiné ne servent pas deux fois)
        C.STATS_WINDOW_MAX_COMBOS = 2
        r3, s3, _ = with_fakes(lambda: app.run_stats_window(NOW + timedelta(hours=1, minutes=1), fetch=Fx()))
        assert r3["combo"] == "ts5w2", r3
        rows = store.load(fair.COMBOS_FILE, [])
        assert [r["tier"] for r in rows] == ["ts5w1", "ts5w2"]
        assert not {l["id"] for l in rows[0]["legs"]} & {l["id"] for l in rows[1]["legs"]}, "jambes risquées : un seul combiné"
        assert {l["id"] for l in rows[1]["legs"]} & {"M8", "M9", "M10"}
        assert "Stats n°1" in str(played.todo(DAY)) and "Stats n°2" in str(played.todo(DAY))
        r4, s4, _ = with_fakes(lambda: app.run_stats_window(NOW + timedelta(hours=1, minutes=2), fetch=Fx()))
        assert r4["combo"] is None and len(store.load(fair.COMBOS_FILE, [])) == 2, "plafond de 2 par jour"
        C.STATS_WINDOW_MAX_COMBOS = 0
        setup(p=0.66)
        r5, s5, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=Fx()))
        assert r5["combo"] is None and window_msgs(s5)[0] == [] and r5["read"] == 4, "0 : plus de combiné, mais les pronostics sont quand même remis à la cote"
    finally:
        C.STATS_WINDOW_MAX_COMBOS = old
    print("ok  jamais deux fois les mêmes jambes, plafond de combinés par jour, paliers ts5w1 / ts5w2")


def test_reuse_rules_with_morning_combos():
    """Même Usage que les combinés du matin : une jambe RISQUÉE déjà dans un combiné du jour (ici un « ts5 » du matin) n'est pas reprise ; la jambe du combiné envoyé n'est
    ni relue ni remise à la cote (le combiné du matin ne change jamais)."""
    leg_prono = prono("M1", FOULS, odds_=1.9, p=0.66)
    leg = {k: leg_prono.get(k) for k in ("key", "id", "market", "name", "point", "prob", "p0", "xodds", "xref", "min_odds", "commence", "home", "away")} | {"result": None}
    row = {"date": DAY, "tier": "ts5", "min_odds": 6.1, "prob": 0.3, "keys": [leg["key"]], "legs": [leg], "result": None}
    setup(combos=[row])
    today = store.load("today.json", {})
    strong = prono("M1", SHOTS_H, p=0.80)                         # le meilleur pari stats du jour (sûr, 80 %) : choisi sans règle de réutilisation
    today["pronos"] = [leg_prono if c["key"] == leg_prono["key"] else strong if c["key"] == strong["key"] else c for c in today["pronos"]]
    store.save("today.json", today)
    snap_row = copy.deepcopy(store.load(fair.COMBOS_FILE, []))
    fx = Fx()
    rep, sent, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=fx))
    assert fx.calls and "M1" in fx.calls[0]["ids"], "M1 a encore d'autres pronostics stats à relire"
    assert fx.calls[0]["wanted"]["M1"] == {SHOTS_H, SOT}, "la jambe du combiné du matin (protégée) n'est pas relue"
    assert by_key()[leg_prono["key"]] == leg_prono, "... ni remise à la cote : identique au caractère près"
    assert rep["combo"] == "ts5w1"
    new = store.load(fair.COMBOS_FILE, [])
    assert new[0] == snap_row[0], "le combiné du matin n'est pas modifié"
    assert "M1" not in {l["id"] for l in new[1]["legs"]}, "M1 : jambe risquée déjà prise ce matin, pas reprise"
    pool = app.upcoming(store.load("today.json", {})["pronos"], NOW)
    free = CB.fair_stats_window(pool, usage=CB.Usage(), accept=lambda c: statswindow.in_window(c, NOW) and statswindow.fresh(c, NOW))
    assert free and "M1" in {l["id"] for l in free[0]}, "sans la règle de réutilisation, M1 (80 %) serait choisi : le test distingue bien"
    print("ok  mêmes règles de réutilisation que le matin ; jambes d'un combiné envoyé jamais retouchées")


def test_no_read_when_fresh_combo_from_refresh_prices():
    """Cotes déjà fraîches (relevé manuel / de 15:00 il y a 10 min) : aucune requête, mais le combiné et les 💎 partent quand même avec ces cotes."""
    today = setup()
    for c in today["pronos"]:
        if statswindow.is_stat(c) and c["id"] in ("M1", "M2", "M3", "M4"):
            price = PRICES.get((c["id"], c["market"]))
            if price and not isinstance(price, dict):
                c.pop("xnone", None)
                c |= {"xodds": price, "xev": round(c["prob"] * price - 1, 4), "xat": "16:55", "xref": [901, 12, 9.5], "xodds0": price}
    store.save("today.json", today)
    fx = Fx()
    rep, sent, coupons = with_fakes(lambda: app.run_stats_window(NOW, fetch=fx))
    stale = [c for c in today["pronos"] if statswindow.is_stat(c) and c["id"] in ("M2", "M3") and not c.get("xodds")]
    assert [c["id"] for c in stale] == ["M2", "M3"], stale          # M2 (throws, fermé) et M3 (1xBet ne l'avait pas) : à relire
    assert fx.calls and sorted(fx.calls[0]["ids"]) == ["M2", "M3"], fx.calls
    assert rep["combo"] == "ts5w1" and "Cotes 1xBet de 16:55" in window_msgs(sent)[0][0], "l'heure honnête : celle de la cote la plus ancienne"
    print("ok  cotes fraîches reprises sans requête, heure du combiné = cote la plus ancienne")


def test_announce_dedup_and_caps():
    """Nouveaux 💎 stats : un message « 🧮 Stats ouvertes chez 1xBet », au plus N par message et par jour, une seule fois par match (le meilleur pari stats)."""
    setup(announced=False)
    old = (C.STATS_WINDOW_ANNOUNCE, C.STATS_WINDOW_DAY_ANNOUNCE, C.STATS_WINDOW_MAX_COMBOS)
    try:
        C.STATS_WINDOW_MAX_COMBOS = 0
        C.STATS_WINDOW_ANNOUNCE, C.STATS_WINDOW_DAY_ANNOUNCE = 3, 4
        rep, sent, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=Fx()))
        _, lst = window_msgs(sent)
        assert len(lst) == 1 and lst[0].count("💎 ") == 3 and rep["announced"] == 3, lst
        assert len(re.findall(r"\n💎 [^\n]+ · <b>\d+,\d+</b>\n", lst[0])) == 3 and "Cotes 1xBet de 17:05" in lst[0], lst[0]      # chaque pari à sa cote 1xBet
        slots = statswindow.load(DAY)["announced"]
        today = store.load("today.json", {})
        assert len(slots) == 3 and len({s.split("|")[0] for s in slots}) == 3, "un pari stats par match"
        assert set(slots) <= set(today["announced"]), "même registre que le relevé de 15:00 : il ne les ré-annonce pas"
        # deuxième passage : le 4e match (les 3 déjà annoncés ne reviennent pas, même avec un autre marché stats ouvert chez eux)
        rep2, sent2, _ = with_fakes(lambda: app.run_stats_window(NOW + timedelta(minutes=1), fetch=Fx()))
        _, lst2 = window_msgs(sent2)
        assert len(lst2) == 1 and lst2[0].count("💎 ") == 1 and rep2["announced"] == 1, lst2
        slots2 = statswindow.load(DAY)["announced"]
        assert len(slots2) == 4 and len({s.split("|")[0] for s in slots2}) == 4 and slots2[:3] == slots
        rep3, sent3, _ = with_fakes(lambda: app.run_stats_window(NOW + timedelta(minutes=2), fetch=Fx()))
        assert window_msgs(sent3)[1] == [] and rep3["announced"] == 0 and statswindow.load(DAY)["announced"] == slots2, "tout est annoncé : plus rien"
        # suivi gratuit des pronostics annoncés (fair.json) : une ligne par pari annoncé
        assert len([r for r in store.load(fair.FILE, []) if r["market"] in STAT_MARKETS]) == 4
        # plafond du jour : 3 par jour, alors qu'un 4e match attend
        setup(announced=False)
        C.STATS_WINDOW_DAY_ANNOUNCE = 3
        with_fakes(lambda: app.run_stats_window(NOW, fetch=Fx()))
        rep5, sent5, _ = with_fakes(lambda: app.run_stats_window(NOW + timedelta(minutes=1), fetch=Fx()))
        assert window_msgs(sent5)[1] == [] and len(statswindow.load(DAY)["announced"]) == 3, "plafond du jour atteint : rien de plus"
        # interrupteur : plus aucun message de 💎 (0 par message)
        setup(announced=False)
        C.STATS_WINDOW_ANNOUNCE = 0
        rep6, sent6, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=Fx()))
        assert window_msgs(sent6)[1] == [] and rep6["announced"] == 0
    finally:
        C.STATS_WINDOW_ANNOUNCE, C.STATS_WINDOW_DAY_ANNOUNCE, C.STATS_WINDOW_MAX_COMBOS = old
    # sans « announced » dans today.json : créé avec les 💎 DU MATIN (sinon le relevé de 15:00 les croirait nouveaux)
    setup(announced=False)
    with_fakes(lambda: app.run_stats_window(NOW, fetch=Fx()))
    ann = store.load("today.json", {})["announced"]
    assert "M1|totals" in ann and "M2|btts" in ann, ann
    print("ok  💎 stats : un message, plafonds par message et par jour, une seule fois par match")


def test_lock_not_held_during_network():
    """--own-lock : le verrou des données n'est tenu ni pendant la relecture 1xBet ni pendant la demande de code coupon (il l'est pour choisir, fusionner et envoyer)."""
    setup()
    states = {"sel": [], "send": [], "coupon": []}
    fx = Fx()

    def fetch(events, deadline=None, wanted=None, **kw):
        states["fetch"] = lock_free()
        return fx(events, deadline=deadline, wanted=wanted)
    old = (app._sent_keys, telegram.send, xbet.save_coupon, requests.get, requests.post)
    app._sent_keys = lambda day: states["sel"].append(not lock_free()) or old[0](day)
    telegram.send = lambda m, *a, **k: states["send"].append(not lock_free()) or True
    xbet.save_coupon = lambda legs: states["coupon"].append(lock_free()) or "CODE1"
    os.environ[fmmatch.OWN_LOCK_ENV] = "1"
    try:
        rep = app.run_stats_window(NOW, fetch=fetch)
    finally:
        os.environ.pop(fmmatch.OWN_LOCK_ENV, None)
        app._sent_keys, telegram.send, xbet.save_coupon, requests.get, requests.post = old
    assert states["fetch"] is True, "le verrou doit être LIBRE pendant la relecture 1xBet"
    assert states["coupon"] == [True], "... et pendant la demande du code coupon"
    assert states["sel"] and states["sel"][0] is True, "la sélection se fait sous le verrou"
    assert states["send"] and all(states["send"]), "les envois Telegram se font sous le verrou (outbox.json cohérent)"
    assert rep["combo"] == "ts5w1" and rep["read"] == 4 and lock_free(), "verrou rendu"
    # sans --own-lock (unité systemd sous flock) : AUCUN verrou repris ici (inter-blocage) : le passage va au bout même si le verrou est déjà tenu
    setup()
    old_wait = C.LOCK_WAIT_MIN
    C.LOCK_WAIT_MIN = 0.02
    try:
        with store.data_lock(wait=0):
            rep, sent, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=Fx()))
    finally:
        C.LOCK_WAIT_MIN = old_wait
    assert rep["combo"] == "ts5w1" and rep["read"] == 4, rep
    # --own-lock et verrou occupé trop longtemps : passage reporté, rien d'écrit, pas d'exception
    setup()
    before = copy.deepcopy(store.load("today.json", {}))
    C.LOCK_WAIT_MIN = 0.02
    os.environ[fmmatch.OWN_LOCK_ENV] = "1"
    try:
        with store.data_lock(wait=0):
            rep, sent, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=Fx()))
    finally:
        os.environ.pop(fmmatch.OWN_LOCK_ENV, None)
        C.LOCK_WAIT_MIN = old_wait
    assert rep["read"] == 0 and sent == [] and store.load("today.json", {}) == before
    print("ok  verrou libre pendant les requêtes 1xBet (--own-lock), jamais repris sous flock")


def test_morning_line():
    """Au matin, des pronostics stats sans cote (1xBet les ouvre tard) : une ligne l'explique ; rien si tout est chiffré ou si 1xBet n'a rien chiffré."""
    today = setup()
    pronos = today["pronos"]
    line = "🧮 Les paris tirs / fautes / touches arrivent quelques heures avant les matchs (1xBet les ouvre tard)."
    assert statswindow.morning_note(pronos) == line
    picks = [{"key": f"k{i}", "id": f"e{i}", "odds": 1.9, "prob": 0.6, "edge": 0.08, "n_books": 14, "weight": 1.0, "score": 0.08, "market": "h2h", "name": f"H{i}",
              "why": "", "pm": None, "commence": f"{DAY}T{12 + i}:00:00Z", "home": f"H{i}", "away": f"A{i}", "event": f"H{i} - A{i}", "league": "L",
              "start": f"{12 + i}:00", "pick": f"H{i} gagne", "group": "soccer", "sport_key": "soccer_epl"} for i in range(8)]
    msgs, _, _ = app.build_day(picks, date(2026, 10, 9), pronos=pronos)
    head = msgs[0]
    assert "<i>" + line + "</i>" in head and head.index(line) > head.index("Budget"), head
    # sans pari réel (pas d'en-tête) : la ligne passe sous la liste des pronostics
    msgs, _, _ = app.build_day([], date(2026, 10, 9), pronos=pronos)
    assert sum(line in m for m in msgs) == 1 and any(line in m and "Pronostics" in m for m in msgs)
    # tout est chiffré / rien n'est chiffré / interrupteurs
    priced = [c | {"xodds": 1.9, "xev": 0.2} if statswindow.is_stat(c) else c for c in pronos]
    assert statswindow.morning_note(priced) is None
    assert statswindow.morning_note([c for c in pronos if statswindow.is_stat(c)]) is None, "1xBet n'a rien chiffré : la raison n'est pas là"
    assert statswindow.morning_note([c | {"xgid": None} if statswindow.is_stat(c) else c for c in pronos]) is None, "match inconnu de 1xBet"
    assert statswindow.morning_note([]) is None and statswindow.morning_note(None) is None
    for name, val in (("STATS_WINDOW_NOTE", False), ("STATS_WINDOW_ENABLED", False)):
        old = getattr(C, name)
        setattr(C, name, val)
        try:
            assert statswindow.morning_note(pronos) is None, name
        finally:
            setattr(C, name, old)
    print("ok  ligne du matin : les paris stats arrivent quelques heures avant les matchs")


def test_combi_n_uses_window_priced_legs():
    """« combi N » : avant la fenêtre les pronostics stats sont écartés (la journée est chiffrée) ; après, leurs cotes servent ; la vérification en direct relit leurs
    sous-matchs ; keep_pronos garde ces jambes."""
    today = setup()
    pr0 = today["pronos"]
    assert not [l for l in CB.fair_legs(pr0) if statswindow.is_stat(l)], "avant : aucun pronostic stats n'est une jambe"
    fx = Fx()
    with_fakes(lambda: app.run_stats_window(NOW, fetch=fx))
    pr1 = store.load("today.json", {})["pronos"]
    legs = [l for l in CB.fair_legs(pr1) if statswindow.is_stat(l)]
    assert {l["id"] for l in legs} >= {"M1", "M2", "M3", "M4"}, [l["key"] for l in legs]
    assert all(l["odds"] == l["xodds"] and l["xref"] for l in legs)
    combo = CB.fair_custom(pr1, 5)
    assert combo and any(statswindow.is_stat(l) for l in combo[0]), "combi 5 : des jambes de stats au prix de la fenêtre"
    kept = {c["key"] for c in app.keep_pronos(pr1, [(5, combo)])}
    assert {c["key"] for c in combo[0]} <= kept and {l["key"] for l in legs} <= kept, "keep_pronos garde toutes les jambes de combiné"
    # vérification en direct d'un combiné bâti avec elles : les sous-matchs stats des jambes sont demandés (wanted), en direct
    sl = [l for l in legs if l["id"] in ("M1", "M2")]
    live = Fx()
    old = xbet.fetch_day
    xbet.fetch_day = live
    try:
        res, hhmm, ok = app._live_fair(sl, 5.0, 7.0, now=NOW + timedelta(minutes=2))
    finally:
        xbet.fetch_day = old
    assert ok and live.calls, live.calls
    c0 = live.calls[0]
    assert c0["kw"].get("live") is True and {"M1", "M2"} <= set(c0["ids"])
    wanted = c0["wanted"]
    assert wanted["M1"] & {FOULS, SHOTS_H, SOT} and wanted["M2"] & {SHOTS_H}, wanted
    assert all(set(ms) - set(STAT_MARKETS) <= {"totals", "btts"} for ms in wanted.values()), wanted
    # « combi 5 » de bout en bout : le combiné aux cotes 1xBet est relu en direct avec les sous-matchs stats de ses jambes
    live3 = Fx()
    real_check = xprice.live_check
    xprice.live_check = lambda ids, now=None, fetch=None, fresh_min=None: real_check(ids, NOW + timedelta(minutes=2), fetch, fresh_min)
    old_fd, old_send, old_cp, old_now = xbet.fetch_day, telegram.send, xbet.save_coupon, app.now_utc
    sent3 = []
    xbet.fetch_day, telegram.send, xbet.save_coupon = live3, lambda m, *a, **k: sent3.append(m) or True, lambda legs: "CODE9"
    app.now_utc = lambda: NOW + timedelta(minutes=2)                               # l'horloge de la commande (custom_combo relit les matchs « à venir »)
    try:
        reply = app.custom_combo([], app.upcoming(pr1, NOW + timedelta(minutes=2)), 5, DAY)
    finally:
        xprice.live_check, xbet.fetch_day, telegram.send, xbet.save_coupon, app.now_utc = real_check, old_fd, old_send, old_cp, old_now
    assert live3.calls and live3.calls[0]["kw"].get("live") is True, live3.calls
    stat_ids = {l["id"] for l in CB.fair_custom(pr1, 5)[0] if statswindow.is_stat(l)}
    assert stat_ids and all(live3.calls[0]["wanted"][i] & set(STAT_MARKETS) for i in stat_ids), (stat_ids, live3.calls[0]["wanted"])
    assert "CODE9" in str(reply) and "<b>💎 COMBINÉ — cote " in str(reply) and "🕒 Cotes 1xBet de " in str(reply), reply
    # xprice.live_check directement (boutons « Vérifier ce combiné » et « combi N »)
    live2 = Fx()
    xprice.live_check({"M1", "M4"}, NOW + timedelta(minutes=3), fetch=live2)
    assert live2.calls[0]["wanted"]["M4"] == {OFFS} and FOULS in live2.calls[0]["wanted"]["M1"]
    print("ok  « combi N » : jambes de stats aux cotes de la fenêtre, relues en direct avec leurs sous-matchs")


def test_refresh_includes_stat_subgames_window_first():
    """Relevé de 15:00 / manuel : les sous-matchs stats des matchs de la fenêtre sont demandés (wanted) et ces matchs sont relus avant les autres."""
    z1 = [prono("Z1", "totals", "Over", 2.5, 0.64)]                  # hors fenêtre (22:00), sans cote : ni prioritaire ni à pronostics stats
    setup(extra=z1)
    today = store.load("today.json", {})
    today["pronos"] = [c for c in today["pronos"] if c["id"] == "Z1"] + [c for c in today["pronos"] if c["id"] != "Z1"]       # Z1 (hors fenêtre) en tête
    store.save("today.json", today)
    fx = Fx()
    with_fakes(lambda: app.run_xbet_refresh(NOW, fetch=fx, announce=False))
    ids = fx.calls[0]["ids"]
    assert ids[:4] == ["M1", "M2", "M3", "M4"] and ids.index("Z1") == 4, ids          # M1, M2 : paris 💎 montrés ; puis les matchs de la fenêtre ; Z1 (en tête de today.json) après
    w = fx.calls[0]["wanted"]
    assert {FOULS, SHOTS_H, SOT} <= w["M1"] and OFFS in w["M4"], "sous-matchs stats relus par le relevé"
    now_by = by_key()
    assert now_by["M1|team_fouls_total||9.5|Under"]["xodds"] == 1.85 and now_by["M1|team_fouls_total||9.5|Under"]["xat"] == "17:05"
    print("ok  relevé de 15:00 / manuel : sous-matchs stats relus, matchs de la fenêtre d'abord")


def test_data_budget_and_real_requests():
    """Vrai chemin xbet.fetch_day (faux http) : seuls le match et les sous-matchs stats voulus sont requis (jamais les corners), pas de menu ; plafond mensuel."""
    setup()

    def cell(t, cf, p):
        return [{"type": t, "cf": cf, "cfView": str(cf), "parameter": p}]

    def main(gid):                                   # sous-matchs listés comme chez 1xBet : id = match * 1000 + type
        return {"constId": 7000 + gid, "eventGroups": [], "subGamesForMainGame": [{"id": gid * 1000 + t, "gameTypeId": t} for t in (2, 12, 95, 14, 55, 13)]}
    PRICE = {12: 1.7, 95: 1.8, 14: 1.85, 55: 1.9, 13: 1.85}
    ladder = lambda price: {"eventGroups": [{"groupId": 17, "events": [cell(9, 1.7, 9.5) + cell(10, price, 9.5), cell(11, 1.7, 9.5) + cell(12, price, 9.5),      # noqa: E731
                                                                      cell(13, 1.7, 9.5) + cell(14, price, 9.5)]}]}
    calls = []

    def fetch(url, source, ttl, headers=None, check_robots=True):
        calls.append(url)
        gid = int(dict(parse_qsl(urlparse(url).query))["gameId"])
        body = main(gid) if gid < 1000 else ladder(PRICE.get(gid % 1000, 1.8))
        return http.Result(url, json.dumps(body).encode(), 200)
    old = http.fetch
    http.fetch = fetch
    try:
        rep, sent, coupons = with_fakes(lambda: app.run_stats_window(NOW))
    finally:
        http.fetch = old
    eps = [(urlparse(u).path.rsplit("/", 1)[-1], int(dict(parse_qsl(urlparse(u).query))["gameId"])) for u in calls]
    assert eps and all(e == "gameEvents" for e, _ in eps), eps                  # ni menu ni liste de championnat : les matchs ont leur id 1xBet (xgid)
    ids = [g for _, g in eps]
    assert sorted(g for g in ids if g < 1000) == [101, 102, 103, 104], ids       # le match (pour trouver ses sous-matchs ouverts), M5 M6 M7 hors fenêtre : rien
    subs = {(g // 1000, g % 1000) for g in ids if g >= 1000}
    assert subs == {(101, 14), (101, 95), (101, 12), (102, 95), (102, 55), (103, 55), (104, 13)}, subs      # UNE requête par famille voulue, jamais les corners (type 2)
    assert len(ids) == 11
    kb = len(ids) * statswindow.KB_PER_REQUEST
    assert kb < 80, kb                                                           # ≈ 70 Ko pour 4 matchs
    doc = statswindow.load(DAY)
    assert doc["passes"] == 1 and doc["reads"]["M1"]["n"] == 1
    now_by = by_key()
    assert now_by["M4|team_offsides_total||9.5|Under"]["xodds"] == 1.85 and now_by["M1|team_shots_home||9.5|Under"]["xodds"] == 1.8
    assert now_by["M3|team_throws_total||9.5|Under"]["xodds"] == 1.9 and now_by["M2|team_throws_home||9.5|Under"]["xodds"] == 1.9
    assert now_by["M1|team_sot_total||9.5|Under"]["xref"][0] == 101012, now_by["M1|team_sot_total||9.5|Under"]["xref"]      # référence = le sous-match (coupon)
    assert rep["combo"] == "ts5w1" and coupons, rep
    # plafond mensuel : au-delà de STATS_WINDOW_MONTH_PCT % de XBET_MONTH_MB la fenêtre se tait (le matin passe avant)
    setup()
    limit = int(C.XBET_MONTH_MB * 1e6 * C.STATS_WINDOW_MONTH_PCT / 100)
    store.save(xbet.USAGE, xbet.usage() | {"bytes": limit + 1})
    fx = Fx()
    rep, sent, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=fx))
    assert fx.calls == [] and rep["read"] == 0, "plafond mensuel : aucune relecture"
    store.save(xbet.USAGE, xbet.usage() | {"bytes": limit - 10000})
    fx = Fx()
    with_fakes(lambda: app.run_stats_window(NOW, fetch=fx))
    assert fx.calls, "sous le seuil : relecture"
    # estimation du pire cas (README) : STATS_WINDOW_DAY_READS relectures par jour, chacune avec les 5 familles stats ouvertes
    worst_day = statswindow.estimate_kb(C.STATS_WINDOW_DAY_READS, 5) / 1000
    assert worst_day < 8.0 and worst_day * 31 < C.XBET_MONTH_MB * 0.85 * C.STATS_WINDOW_MONTH_PCT / 100, (worst_day, worst_day * 31)
    print(f"ok  requêtes réelles limitées (≈ {kb:.0f} Ko ce passage), plafond mensuel ; pire cas {worst_day:.2f} Mo/jour = {worst_day * 31:.0f} Mo/mois")


def test_usage_not_lost_when_another_reader_writes():
    """La relecture sans verrou ne doit pas effacer la consommation d'un autre relevé (compteur du mois relu à la fermeture de la session)."""
    store.save(xbet.USAGE, {})
    store.save(state.FILE, {})
    ses = xbet._Session()
    w = http.WIRE.setdefault(xbet.DOMAIN, {"bytes": 0, "requests": 0})
    w["bytes"] += 100_000
    w["requests"] += 5                                                           # ce relevé a consommé 100 Ko
    store.save(xbet.USAGE, xbet.usage() | {"bytes": 3_000_000, "requests": 40, "days": {str(state.now().date()): 3_000_000}})      # un autre relevé a écrit entre-temps
    ses.close("essai")
    u = xbet.usage()
    assert u["bytes"] == 3_100_000 and u["requests"] == 45 and u["days"][str(state.now().date())] == 3_100_000, u
    # la session Webshare adoptée est retenue ; sinon celle déjà enregistrée reste
    store.save(xbet.USAGE, xbet.usage() | {"session": 7})
    ses2 = xbet._Session()
    ses2.close("rien")
    assert xbet.usage()["session"] == 7
    ses3 = xbet._Session()
    ses3.switched = 9
    ses3.close("autre session")
    assert xbet.usage()["session"] == 9
    print("ok  compteur de données relu à la fermeture : rien d'effacé par un relevé concurrent")


def test_never_raises_and_changes_nothing_on_failure():
    setup()
    before = copy.deepcopy(store.load("today.json", {}))
    snap = snapshot()

    def boom(events, deadline=None, wanted=None, **kw):
        raise RuntimeError("1xBet en panne")
    rep, sent, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=boom))
    assert rep["read"] == 0 and sent == [] and store.load("today.json", {}) == before and snapshot() == snap
    assert statswindow.load(DAY)["reads"]["M1"].get("miss") == 1 and not statswindow.load(DAY)["reads"]["M1"].get("n"), "panne : essai noté sans relecture réussie"
    # 1xBet ne relit aucun match : idem (les essais ne sont pas des relectures)
    setup()
    before = copy.deepcopy(store.load("today.json", {}))
    rep, sent, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=lambda events, deadline=None, wanted=None, **kw: {}))
    assert rep["read"] == 0 and sent == [] and store.load("today.json", {}) == before
    # passages sans réponse : on y renonce pour la journée après STATS_WINDOW_MISSES essais (délai entre deux essais coupé pour le test)
    setup()
    old_retry = C.STATS_WINDOW_RETRY_MIN
    C.STATS_WINDOW_RETRY_MIN = 0
    silent = Fx(prices={})
    try:
        for k in range(C.STATS_WINDOW_MISSES + 2):
            with_fakes(lambda: app.run_stats_window(NOW + timedelta(seconds=k), fetch=lambda events, deadline=None, wanted=None, **kw: {}))
        assert statswindow.load(DAY)["reads"]["M1"]["miss"] == C.STATS_WINDOW_MISSES, statswindow.load(DAY)["reads"]["M1"]
        with_fakes(lambda: app.run_stats_window(NOW + timedelta(seconds=30), fetch=silent))
        assert silent.calls == [], "M1, M2, M3, M4 : abandonnés pour la journée"
    finally:
        C.STATS_WINDOW_RETRY_MIN = old_retry
    # une exception interne ne sort jamais
    old = statswindow.select
    statswindow.select = lambda *a, **k: (_ for _ in ()).throw(ValueError("bug"))
    try:
        rep, _, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=Fx()))
    finally:
        statswindow.select = old
    assert rep["read"] == 0
    # pas de today.json du jour / document illisible / jour passé / interrupteurs
    setup()
    t = store.load("today.json", {})
    store.save("today.json", t | {"day": "2026-10-01"})
    fx = Fx()
    rep, sent, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=fx))
    assert fx.calls == [] and sent == []
    store.save("today.json", None)
    assert with_fakes(lambda: app.run_stats_window(NOW, fetch=fx))[0]["read"] == 0
    store.save("today.json", t)
    late = datetime(2026, 10, 10, 0, 30, tzinfo=timezone.utc)
    assert with_fakes(lambda: app.run_stats_window(late, fetch=fx))[0]["read"] == 0 and fx.calls == []
    for name in ("STATS_WINDOW_ENABLED", "XBET_ENABLED"):
        old = getattr(C, name)
        setattr(C, name, False)
        try:
            assert with_fakes(lambda: app.run_stats_window(NOW, fetch=fx))[0]["read"] == 0 and fx.calls == [], name
        finally:
            setattr(C, name, old)
    print("ok  jamais d'exception, rien ne change en cas de panne ; interrupteurs")


def test_concurrent_writes_during_network():
    """Pendant la relecture (sans verrou), d'autres processus écrivent : today.json est rechargé avant la fusion (rien de perdu) et un pronostic devenu jambe d'un combiné
    envoyé entre-temps n'est pas remplacé."""
    setup()
    fx = Fx()
    late_leg = prono("M1", FOULS)

    def fetch(events, deadline=None, wanted=None, **kw):
        out = fx(events, deadline=deadline, wanted=wanted)
        t = store.load("today.json", {})                                                    # le bot Telegram / un autre passage écrit pendant la relecture
        t["marker"] = 42
        t["pronos"].append(prono("M7", SOT))
        store.save("today.json", t)
        leg = {k: late_leg.get(k) for k in ("key", "id", "market", "name", "point", "prob", "commence")} | {"result": None}
        store.save(fair.COMBOS_FILE, [{"date": DAY, "tier": "vm1", "custom": True, "min_odds": 5.0, "prob": 0.3, "keys": [late_leg["key"]], "legs": [leg],
                                       "result": None}])
        return out
    rep, sent, _ = with_fakes(lambda: app.run_stats_window(NOW, fetch=fetch))
    today = store.load("today.json", {})
    assert today["marker"] == 42 and any(c["key"] == "M7|team_sot_total||9.5|Under" for c in today["pronos"]), "écritures concurrentes conservées"
    now_by = by_key()
    assert now_by[late_leg["key"]] == late_leg, "jambe d'un combiné créé pendant la relecture : jamais remplacée"
    assert now_by["M1|team_shots_home||9.5|Under"]["xodds"] == 1.8 and now_by["M4|team_offsides_total||9.5|Under"]["xodds"] == 1.85
    print("ok  écritures concurrentes pendant la relecture : rien de perdu, jambes d'un combiné intactes")


def test_tier_helpers_and_wording():
    assert CB.window_tier(3) == "ts5w3" and C.STATS_WINDOW_TIER == "ts5w"
    assert played._fair_name("ts5w2", True, {"at": "18:05"}) == "Stats n°2 (18:05)" and played._fair_name("ts5w1") == "Stats n°1"
    assert played._fair_name("tc5") == "Tirs cadrés" and played._fair_name("tv5") == "Tirs vers le but" and played._fair_name(12).startswith("À vérifier")
    assert app._of_day("ts5w1") == "stats" and app._of_day("ts5") == "du matin" and app._of_day(5) == "du matin"
    assert app._tier_range("tc5")[2] == "team_sot_" and app._tier_range("zz") is None
    print("ok  paliers ts5w<n> : titre, nom dans « Mes paris », fourchette, mots des mises à jour")


def test_refresh_button_on_window_combo():
    """« 🎟️ Vérifier ce combiné » sous un combiné de la fenêtre : relu en direct, même fourchette (5 à 7), familles stats seulement."""
    setup()
    with_fakes(lambda: app.run_stats_window(NOW, fetch=Fx()))
    row = store.load(fair.COMBOS_FILE, [])[0]
    pool = store.load("today.json", {})["pronos"]
    check = lambda ids, now=None, fresh_min=None: (pool, set(ids), "17:20")                                  # noqa: E731
    reply, sent, coupons = with_fakes(lambda: app.refresh_combo("ts5w1", DAY, NOW + timedelta(minutes=15), check=check))
    assert "Combiné stats remis aux cotes 1xBet de 17:20" in str(reply) and "aucun pari à changer" in str(reply), reply
    assert "joue|vts5w1" in str(reply.markup) and coupons == [len(row["legs"])]
    assert chat.refresh_button("vts5w1", DAY) == ("🎟️ Vérifier ce combiné", "xmaj|ts5w1|2026-10-09")
    print("ok  bouton « Vérifier ce combiné » sur un combiné de la fenêtre")


def test_window_combo_followed_like_morning_combos():
    """Un combiné de la fenêtre est suivi comme ceux du matin : le relevé manuel / de 15:00 (_update_combos) le renvoie à jour si sa cote a bougé de plus de 5 %, avec les
    mots « combiné stats » ; noté joué, il n'est jamais remplacé (Kelly prévient seulement)."""
    setup()
    with_fakes(lambda: app.run_stats_window(NOW, fetch=Fx()))
    row = store.load(fair.COMBOS_FILE, [])[0]
    pronos = copy.deepcopy(store.load("today.json", {})["pronos"])
    for c in pronos:
        if c["key"] == row["keys"][0]:
            c["xodds"] = round(c["xodds"] * 0.92, 2)                                 # la cote d'une jambe baisse de 8 % (relevé suivant), reste jouable (> cote mini)
    ids = {l["id"] for l in row["legs"]}
    t = NOW + timedelta(minutes=20)
    n, sent, coupons = with_fakes(lambda: app._update_combos(DAY, pronos, t, ids))
    assert n == 1 and any("Un combiné stats a changé chez 1xBet" in str(m) for m in sent) and coupons, (n, [str(m)[:90] for m in sent])
    new = store.load(fair.COMBOS_FILE, [])[0]
    assert new["tier"] == "ts5w1" and new["updated"] == t.strftime("%H:%M") and new["min_odds"] < row["min_odds"] and len(store.load(fair.COMBOS_FILE, [])) == 1
    # noté joué : le ticket est placé, jamais remplacé
    played.command(["vts5w1", "5,5", "1000"], DAY, NOW)
    frozen = copy.deepcopy(store.load(fair.COMBOS_FILE, []))
    for c in pronos:
        if c["key"] == row["keys"][0]:
            c["xodds"] = round(c["xodds"] * 0.9, 2)
    n, sent, _ = with_fakes(lambda: app._update_combos(DAY, pronos, t + timedelta(minutes=5), ids))
    assert n == 0 and store.load(fair.COMBOS_FILE, []) == frozen, "combiné noté : ses paris et sa note ne changent pas"
    print("ok  combiné de la fenêtre suivi comme un combiné du matin (mise à jour, ticket noté intact)")


def test_bot_track_order():
    """bot.py --track : clôture, puis fenêtre des stats, puis stats de match FotMob ; --own-lock : la fenêtre prend son verrou elle-même (pas tenu par le processus)."""
    sys.argv.append("--sample")
    try:
        import bot
    finally:
        sys.argv.remove("--sample")
    seen = []
    old = (app.run_track, app.run_stats_window, app.fill_match_stats)
    own = lambda: "own" if os.environ.get(fmmatch.OWN_LOCK_ENV) else "plain"        # noqa: E731
    app.run_track = lambda *a, **k: seen.append(("track", own(), not lock_free()))
    app.run_stats_window = lambda *a, **k: seen.append(("window", own(), not lock_free()))
    app.fill_match_stats = lambda *a, **k: seen.append(("fill", own(), not lock_free()))
    try:
        bot.main(["--track", "--own-lock"])
        assert seen == [("track", "own", True), ("window", "own", False), ("fill", "own", False)], seen
        os.environ.pop(fmmatch.OWN_LOCK_ENV, None)
        del seen[:]
        bot.main(["--track"])
        assert seen == [("track", "plain", False), ("window", "plain", False), ("fill", "plain", False)], seen
    finally:
        app.run_track, app.run_stats_window, app.fill_match_stats = old
        os.environ.pop(fmmatch.OWN_LOCK_ENV, None)
    print("ok  python bot.py --track : clôture, fenêtre des stats, FotMob")


def test_bot_stats_window_cli():
    """python bot.py --stats-window : même passage à la main ; prend le verrou lui-même (mode --own-lock) sauf --no-lock."""
    sys.argv.append("--sample")
    try:
        import bot
    finally:
        sys.argv.remove("--sample")
    seen = []
    old = app.run_stats_window
    app.run_stats_window = lambda *a, **k: seen.append("own" if os.environ.get(fmmatch.OWN_LOCK_ENV) else "plain") or {}
    try:
        bot.main(["--stats-window"])
        os.environ.pop(fmmatch.OWN_LOCK_ENV, None)
        bot.main(["--stats-window", "--no-lock"])
    finally:
        app.run_stats_window = old
        os.environ.pop(fmmatch.OWN_LOCK_ENV, None)
    assert seen == ["own", "plain"], seen
    print("ok  python bot.py --stats-window")


def test_stale_prices_never_make_a_combo():
    """Des cotes périmées (lues le matin) ne font jamais un combiné : si 1xBet ne répond pas, rien n'est envoyé ; relues, elles servent."""
    today = setup()
    for c in today["pronos"]:
        price = PRICES.get((c["id"], c["market"]))
        if statswindow.is_stat(c) and c["id"] in ("M1", "M2", "M3", "M4") and price and not isinstance(price, dict):
            c.pop("xnone", None)
            c |= {"xodds": price, "xev": round(c["prob"] * price - 1, 4), "xat": "10:00", "xref": [901, 12, 9.5], "xodds0": price}
    store.save("today.json", today)
    rep, sent, coupons = with_fakes(lambda: app.run_stats_window(NOW, fetch=lambda events, deadline=None, wanted=None, **kw: {}))
    assert rep["combo"] is None and sent == [] and coupons == [] and store.load(fair.COMBOS_FILE, []) == [], (rep, sent)
    rep, sent, _ = with_fakes(lambda: app.run_stats_window(NOW + timedelta(seconds=1), fetch=Fx()))
    assert rep["read"] == 0 and rep["combo"] is None, "le même match n'est pas relu avant STATS_WINDOW_RETRY_MIN minutes"
    old_retry = C.STATS_WINDOW_RETRY_MIN
    C.STATS_WINDOW_RETRY_MIN = 0
    try:
        rep, sent, coupons = with_fakes(lambda: app.run_stats_window(NOW + timedelta(seconds=2), fetch=Fx()))
    finally:
        C.STATS_WINDOW_RETRY_MIN = old_retry
    assert rep["combo"] == "ts5w1" and "Cotes 1xBet de 17:05" in window_msgs(sent)[0][0], "relues à 17:05 : le combiné part avec ces cotes"
    print("ok  cotes périmées : jamais dans un combiné")


def test_readme_documents_window():
    readme = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md"), encoding="utf-8").read()
    for word in ("Fenêtre des stats", "STATS_WINDOW_ENABLED", "STATS_WINDOW_MAX_COMBOS", "STATS_WINDOW_MONTH_PCT", "ts5w1", "stats_window.json", "vts5w1"):
        assert word in readme, word
    print("ok  README")


if __name__ == "__main__":
    test_selection_by_kickoff()
    test_selection_caps()
    test_pass_reads_only_stat_subgames_and_reprices()
    test_combo_sent_once_with_coupon_and_tracking()
    test_dedup_same_legs_and_caps()
    test_reuse_rules_with_morning_combos()
    test_no_read_when_fresh_combo_from_refresh_prices()
    test_announce_dedup_and_caps()
    test_lock_not_held_during_network()
    test_morning_line()
    test_combi_n_uses_window_priced_legs()
    test_refresh_includes_stat_subgames_window_first()
    test_data_budget_and_real_requests()
    test_usage_not_lost_when_another_reader_writes()
    test_never_raises_and_changes_nothing_on_failure()
    test_concurrent_writes_during_network()
    test_tier_helpers_and_wording()
    test_refresh_button_on_window_combo()
    test_window_combo_followed_like_morning_combos()
    test_bot_track_order()
    test_bot_stats_window_cli()
    test_stale_prices_never_make_a_combo()
    test_readme_documents_window()
    print("tests fenêtre des stats : OK")
