"""Corrections de l'audit (lot I), un test par point : argent et suivi (combiné noté jamais remplacé, arrondis, rapport des combinés réglés,
total du matin, archives, règlement « non vérifiable », base de prix des cotes, réutilisation des matchs), saisies et Telegram (cotes à milliers,
cotes absurdes, /combi, liste « Mes paris », attentes, texte libre, textes et devise, longues listes), 1xBet (réponses inattendues, disjoncteur,
marqueurs jeunes / femmes / réserves, sens domicile-extérieur, coupon, heure d'un relevé partiel) et robustesse (matin isolé, relevé manuel,
sauvegarde du décalage Telegram). Hors ligne (1xBet, Telegram et coupons simulés).
Lancer : python -m tests.test_audit_fix"""
import copy
import json
import os
import random
import sys
import traceback
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlparse

from tests import test_notes_fcfa as N      # fixe DATA_DIR (dossier temporaire), proxy simulé, monde de test (pari, pronostic 💎, Fakes...)
from kelly import (app, bankroll, chat, combos as CB, config as C, custom, fair, fmt, learn, money, monde, played, settle, store, telegram,
                   track, xprice)  # noqa: E402
from kelly.sources import http, state, xbet  # noqa: E402

DAY, KO, FCFA = N.DAY, N.KO, N.FCFA
REAL_CHECK = xprice.live_check                 # le vrai relevé en direct (N.Fakes le remplace pendant un `with`)
GOT9 = {f"m{i}" for i in range(9)}


# ---------- outils ----------

class Fakes(N.Fakes):
    """N.Fakes + le remplacement de texte + boutons d'un message (telegram.edit_message) capturé dans `self.rewrites`."""

    def __enter__(self):
        self.rewrites = []
        self.old_edit = getattr(telegram, "edit_message", None)
        telegram.edit_message = lambda mid, text, markup=None, chat_id=None: (self.rewrites.append((mid, text, markup)), True)[1]
        return super().__enter__()

    def __exit__(self, *a):
        if self.old_edit is None:
            del telegram.edit_message
        else:
            telegram.edit_message = self.old_edit
        return super().__exit__(*a)


def px_world(n=9, gone=(), drift=None):
    """Relevé 1xBet simulé : « moins de 3,5 buts » à 1,55 (ou `drift`[id]) ; les matchs de `gone` sont lus mais le pari n'y est plus."""
    drift = drift or {}
    return {f"m{i}": ({("btts", "Yes", None): 1.8} if f"m{i}" in gone else {("totals", "Under", 3.5): drift.get(f"m{i}", 1.55)})
            for i in range(n)}


def check_with(priced):
    return lambda ids, now=None, fresh_min=None: REAL_CHECK(ids, now, fetch=N.fake_fetch(priced), fresh_min=fresh_min)


def gone_pronos(gone=("m1",), n=9):
    """Pronostics du relevé où le pari des matchs de `gone` n'est plus chez 1xBet (xnone : match relu, pari absent)."""
    return [N.prono(i, xodds=None, xodds0=None, xev=None, xref=None, xnone=1) if f"m{i}" in gone else N.prono(i) for i in range(n)]


def noted_morning(stake=500, n=4, odds=5.4):
    """Combiné du matin de palier 5 aux cotes 1xBet (m0..m<n-1>, code MORN1) NOTÉ joué (le ticket est placé)."""
    N.reset()
    money.set_daily(5000)
    legs = [N.prono(i) for i in range(n)]
    fair.record_combos([(5, (legs, round(1.55 ** n, 3), 0.72 ** n))], DAY, {5: stake}, FCFA, coupons={5: "MORN1"}, at="09:00")
    store.save("today.json", {"day": DAY, "picks": [], "pronos": N.pronos(9)})
    store.save(app.COMBO_UPDATES, {})
    assert "✅ Noté" in played.command(["v5", str(odds).replace(".", ",")], DAY)
    return legs


def world_known(budget=5000):
    """Monde du matin : Sûr, pari du jour et v5 aux cotes 1xBet connues, avec une mise conseillée (comme test_known_odds_never_asked)."""
    N.reset()
    if budget:
        money.set_daily(budget)
    picks, priced = N.world()
    xprice.price_picks(picks, fetch=N.fake_fetch(priced))
    legs = [N.prono(i) for i in range(4)]
    with N.Fakes(code="KN01"):
        msgs, sent_picks, sent_combos = app.build_day(picks, DAY, odds_time="09:30")
        fair.record_combos([(5, (legs, 1.55 ** 4, 0.72 ** 4))], DAY, {5: 500}, FCFA, coupons={5: "FAIRC"}, at="04:12")
    track.record(picks, {c["key"] for c in sent_picks}, DAY, N.at(4), single_key=app.single_key(sent_picks))
    settle.record_combos(sent_combos, DAY)
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": N.pronos(7), "picks_at": "09:30"})
    return msgs, picks


def kb(markup):
    """[(texte, donnée)] à plat d'un clavier inline."""
    out = []
    for row in (markup or {"inline_keyboard": []})["inline_keyboard"]:
        for b in row:
            out.append((b["text"], b.get("callback_data") or ("copy:" + b["copy_text"]["text"] if "copy_text" in b else "?")))
    return out


def datas(markup):
    return [d for _, d in kb(markup)]


# ---------- 1. un combiné noté n'est jamais remplacé ni tué ----------

def test_noted_combo_never_replaced():
    noted_morning()
    row0 = copy.deepcopy(fair.load_combos()[0])
    assert row0["played"]["odds"] == 5.4
    # un pari du combiné n'est plus chez 1xBet et un remplaçant existe : AVANT, nouvelle version + note retirée
    with Fakes(code="NEW01") as f:
        n = app._update_combos(DAY, gone_pronos(), N.at(13), GOT9)
    row = fair.load_combos()[0]
    assert n == 0 and not f.coupons, (n, f.coupons)
    assert row["keys"] == row0["keys"] and row["legs"] == row0["legs"] and row["min_odds"] == row0["min_odds"], row
    assert row["played"] == row0["played"] and row["result"] is None and "dead" not in row and "orig_keys" not in row, row
    warns = [str(m) for m in f.sent if "⚠️" in str(m)]
    assert len(warns) == 1 and "ton ticket" in warns[0] and "H1 – A1" in warns[0] and "n'est plus chez 1xBet" in warns[0], f.sent
    assert "COMBINÉ À JOUR" not in "".join(map(str, f.sent)) and "NEW01" not in "".join(map(str, f.sent))
    # le relevé suivant, même situation : pas deux fois la même alerte
    with Fakes() as f:
        assert app._update_combos(DAY, gone_pronos(), N.at(15), GOT9) == 0 and not f.sent, f.sent
    # plus aucun remplaçant : AVANT le ticket noté était déclaré mort, annulé et sorti du bilan
    noted_morning()
    only4 = [N.prono(0), N.prono(1, xodds=None, xodds0=None, xev=None, xref=None, xnone=1), N.prono(2), N.prono(3)]
    store.save("today.json", {"day": DAY, "picks": [], "pronos": only4})
    with Fakes() as f:
        app._update_combos(DAY, only4, N.at(13), {"m0", "m1", "m2", "m3"})
    row = fair.load_combos()[0]
    assert "dead" not in row and "skipped" not in row and "auto_skipped" not in row and row["result"] is None, row
    assert row["played"]["odds"] == 5.4 and any("⚠️" in str(m) and "ton ticket" in str(m) for m in f.sent), f.sent
    assert ": plus jouable chez 1xBet" not in str(played.todo(DAY)) and "🚫" not in str(played.todo(DAY))
    # il se règle comme le ticket placé et compte dans l'argent
    rows = fair.load_combos()
    for l in rows[0]["legs"]:
        l["result"] = "win"
    store.save(fair.COMBOS_FILE, rows)
    done_rows, done = settle.settle_fair_combos(N.at(8, day=5))
    assert done and done_rows[0]["result"] == "win" and money.gain(done_rows[0]) is not None
    # garde-fous de la couche fair : jamais remplacé ni tué
    noted_morning()
    r = fair.load_combos()[0]
    snap = copy.deepcopy(r)
    new_legs = [N.prono(i) for i in (0, 1, 2, 5)]
    assert fair.update_combo(r, (new_legs, 1.55 ** 4 * 1.3, 0.5), "14:00", "NEW") == snap and r == snap
    assert fair.kill_combo(r) == snap and r == snap and not fair.is_ticket({"played": {"odds": 2, "stake": 1}, "custom": True})
    # « 🎟️ Vérifier ce combiné » sur le combiné noté : relevé et rapport, jamais de nouvelle version
    noted_morning()
    snap = copy.deepcopy(fair.load_combos()[0])
    with Fakes(code="NEW02") as f:
        out = app.refresh_combo(5, DAY, now=N.at(13), check=check_with(px_world(gone=("m1",))))
    row = fair.load_combos()[0]
    assert row == snap and not f.coupons, (row, snap)
    assert store.load(app.COMBO_UPDATES, {}).get("tiers", {}).get("5", {}).get("keys", snap["keys"]) == snap["keys"]
    assert "H1 – A1" in out and "ton ticket" in out and "COMBINÉ À JOUR" not in out and "retirée" not in out, out
    assert f"annule|v5|{DAY}" in datas(out.markup) and f"xmaj|5|{DAY}" in datas(out.markup), datas(out.markup)
    # dérive minuscule d'une cote (+0,01), tous les paris encore chez 1xBet : la note reste, rien n'est remplacé
    noted_morning()
    snap = copy.deepcopy(fair.load_combos()[0])
    with Fakes() as f:
        out = app.refresh_combo(5, DAY, now=N.at(13), check=check_with(px_world(drift={"m0": 1.56})))
    row = fair.load_combos()[0]
    assert row["played"] == snap["played"] and row["keys"] == snap["keys"] and row["min_odds"] == snap["min_odds"], row
    assert "toujours chez 1xBet" in out and "ton ticket" not in out, out
    print("ok  1 combiné noté : jamais remplacé ni tué ni retiré du bilan (relevé, bouton Vérifier, couche fair) ; seulement un rapport ⚠️ une fois")


# ---------- 2. la note ne tombe pas pour un arrondi ----------

def test_update_keeps_note_on_rounding():
    N.reset()
    money.set_daily(5000)
    odds_list = [1.48, 2.25, 2.36, 2.3, 1.93]
    total = 1.0
    for o in odds_list:
        total *= o
    assert round(round(total, 3), 2) != round(total, 2), "le cas de l'audit : l'arrondi double diffère"
    legs = [N.prono(i, min_odds=o, xodds=o, odds=o, xref=[900 + i, 10, 3.5]) for i, o in enumerate(odds_list)]
    cid = custom.remember(DAY, 50.0, 50.0, 62.5, (legs, total, 0.05), stake=500, cur=FCFA, coupon="ABC", at="15:00", src="pronos")
    fair.record_custom(custom._row(cid, DAY, custom._load(DAY)["items"][cid]), DAY)
    rows = fair.load_combos()
    rows[0]["played"] = {"odds": 50.0, "stake": 500, "at": "x"}
    fair.update_combo(rows[0], (legs, total, 0.05), "15:30", "ABC")
    assert rows[0].get("played"), "mêmes paris, mêmes cotes : la note reste (arrondi double 3 puis 2 décimales)"
    # une vraie différence de cote fait toujours tomber la note d'un combiné pas ticket
    fair.update_combo(rows[0], (legs, total * 1.2, 0.05), "15:45", "ABC")
    assert "played" not in rows[0]
    print("ok  2 update_combo : tolérance d'arrondi (3 puis 2 décimales), la note ne tombe que pour un vrai changement")


# ---------- 3. rapport des combinés réglés ----------

def settled_world(n=8, noted_last=3):
    """`n` combinés d'hier dont toutes les jambes sont gagnantes (pas encore réglés ni annoncés) ; les `noted_last` derniers sont des combis sur
    mesure NOTÉS (mise 1 000 FCFA)."""
    N.reset()
    money.set_daily(5000)
    rows = []
    for k in range(n):
        legs = [N.unpriced(i, key=f"k{k}x{i}") for i in range(2)]
        tier = [5, 12, 25, "tc5", "tv5", "m1", "m2", "m8"][k] if k < 8 else f"m{20 + k}"
        total = 5.01 + k
        r = fair.combo_row(tier, (legs, total, 0.2), DAY, 500, FCFA)
        for l in r["legs"]:
            l["result"] = "win"
        if k >= n - noted_last:
            r |= {"custom": True, "target": 10000 + k, "played": {"odds": total, "stake": 1000, "at": "x"}}
        rows.append(r)
    store.save(fair.COMBOS_FILE, rows)
    return rows


def test_report_announces_every_settled_combo():
    rows = settled_world()
    with Fakes():
        out = app.fair_combos_report(N.at(8, day=5))
    txt = fmt.plain(str(out[0]))
    after = fair.load_combos()
    assert all(r["reported"] for r in after), [(r["tier"], r.get("reported")) for r in after]
    for r in after:                                     # jamais « reported » sans avoir été montré
        label = played.custom_name(r) if r.get("custom") else f"Combiné à cote mini {fmt.odds_txt(r['min_odds'])}"
        assert label in txt, (label, txt)
    assert txt.count("FCFA") >= 3 and len(str(out[0])) < 4000
    # les combinés notés d'abord
    idx_noted = [txt.index(played.custom_name(r)) for r in after if r.get("played")]
    idx_plain = [txt.index(f"Combiné à cote mini {fmt.odds_txt(r['min_odds'])}") for r in after if not r.get("custom")]
    assert max(idx_noted) < min(idx_plain), (idx_noted, idx_plain)
    # beaucoup de combinés : tous annoncés (les derniers en une ligne), les notés jamais perdus
    rows = settled_world(n=14, noted_last=4)
    with Fakes():
        out = app.fair_combos_report(N.at(8, day=5))
    txt = fmt.plain("\n".join(str(m) for m in out))
    after = fair.load_combos()
    assert all(r["reported"] for r in after)
    for r in after:
        label = played.custom_name(r) if r.get("custom") else f"Combiné à cote mini {fmt.odds_txt(r['min_odds'])}"
        assert label in txt, (label, txt[:300])
    assert txt.count("H0 – A0") == app.FAIR_REPORT_DETAIL, txt.count("H0 – A0")       # 6 combinés détaillés (jambe par jambe), les 8 autres en une ligne
    print("ok  3 rapport des combinés réglés : tous annoncés (notés d'abord), rien marqué « reported » sans être montré")


# ---------- 4. le total du matin compte les combinés réglés ce matin-là ----------

def test_morning_total_includes_fair_combos():
    N.reset()
    money.set_daily(5000)
    D, ko = DAY, KO

    def row(key, ev, home, away, **kw):
        return {"key": key, "id": ev, "sport_key": "soccer_epl", "group": "soccer", "event": f"{home} - {away}", "home": home, "away": away,
                "league": "L", "commence": ko, "market": "h2h", "who": None, "point": None, "name": home, "pick": home, "odds": 2.0,
                "prob": .5, "sent": True, "date": D, "result": None} | kw
    track.save([row("s1", "m1", "Alpha", "Bravo", single=True, played={"odds": 2.0, "stake": 1000, "at": "x"}, cur=FCFA, stake=None)])
    store.save("bets.json", {"picks": [], "combos": [{"tier": N.SUR, "keys": ["s1"], "odds": 2.0, "prob": 0.5, "stake": None, "date": D, "result": None}]})
    legs = [{"key": "k1", "id": "m2", "sport_key": "soccer_epl", "home": "Charlie", "away": "Delta", "commence": ko, "market": "h2h", "name": "Charlie",
             "point": None, "prob": .5, "p0": .5, "pick": "Charlie", "min_odds": 2.0, "xodds": 2.0, "result": "win"}]
    store.save(fair.COMBOS_FILE, [{"date": D, "tier": "m1", "min_odds": 2.0, "prob": .5, "keys": ["k1"], "legs": legs, "result": None, "custom": True,
                                   "cur": FCFA, "stake": None, "played": {"odds": 2.0, "stake": 2000, "at": "x"}}])
    sc = {"soccer_epl": [{"id": "m1", "completed": True, "scores": [{"name": "Alpha", "score": "2"}, {"name": "Bravo", "score": "0"}]}]}
    saved, old_fill = settle.settle_pending, app.freeclose.fill
    settle.settle_pending = lambda n=None, **k: saved(n, scores_by_sport=sc, stats_for=lambda b: {})
    app.freeclose.fill = lambda rows, now: 0
    try:
        with Fakes():
            msgs, model = app.morning_report(N.at(8, day=2))
    finally:
        settle.settle_pending, app.freeclose.fill = saved, old_fill
    first = fmt.plain(str(msgs[0]))
    assert "résultat de tes paris notés : +3 000 FCFA" in first, first
    assert money.result(store.load("bets.json", {}), track.load(), fair.load_combos())[1] == 3000
    print("ok  4 résultat du matin : le total des paris notés compte aussi les combinés (mesure, à vérifier) réglés ce matin-là")


# ---------- 5. rien de noté ne disparaît de « depuis le début » ----------

def test_noted_rows_survive_retention():
    N.reset()
    store.save("bets.json", {"picks": [], "combos": []})
    today = date(2026, 10, 9)
    old_day = str(today - timedelta(days=13))

    def mk(tier, day, win, stake=1000, odds=5.0):
        legs = [{"key": f"k{tier}{i}", "id": f"e{i}", "min_odds": 2.0, "xodds": 2.0, "odds": 2.0, "result": "win", "commence": f"{day}T10:00:00Z",
                 "home": "A", "away": "B", "market": "h2h", "name": "A", "point": None} for i in range(2)]
        return {"date": day, "tier": tier, "min_odds": 4.0, "prob": 0.2, "keys": [l["key"] for l in legs], "legs": legs,
                "result": "win" if win else "loss", "custom": True, "played": {"odds": odds, "stake": stake, "at": day + "T09:00+00:00"},
                "cur": FCFA, "stake": None}
    store.save(fair.COMBOS_FILE, [mk("m1", old_day, True, 1000, 4.0), mk("m2", str(today - timedelta(days=2)), False, 500),
                                  mk("m3", old_day, True, 1000, 4.0) | {"played": None} | {"cur": None}])
    rows = fair.load_combos()
    rows[2].pop("played")
    store.save(fair.COMBOS_FILE, rows)
    before = money.result({"combos": []}, [], fair.load_combos())
    fair.record_combos([], str(today + timedelta(days=2)))
    kept = fair.load_combos()
    assert {r["tier"] for r in kept} == {"m1", "m2"}, [(r["tier"], r["date"]) for r in kept]          # le non noté sort, le noté reste
    assert money.result({"combos": []}, [], kept) == before, (money.result({"combos": []}, [], kept), before)
    old_row = next(r for r in kept if r["tier"] == "m1")
    assert old_row.get("slim") and all(set(l) <= set(fair.SLIM_LEG) for l in old_row["legs"]) and old_row["played"], old_row      # jambes allégées, l'argent intact
    assert money.summary_lines({"combos": []}, [], kept)[-1].startswith("depuis le début : +2 500 FCFA")
    fair.record_custom(mk("m9", str(today + timedelta(days=2)), True), str(today + timedelta(days=2)))
    assert {"m1", "m2", "m9"} <= {r["tier"] for r in fair.load_combos()}
    # tracked.json : une ligne notée réglée plus vieille que TRACK_KEEP_DAYS reste, les autres vont à l'archive
    last = date(2026, 10, 9)
    old = str(last - timedelta(days=C.TRACK_KEEP_DAYS + 30))

    def srow(key, day, **kw):
        return {"key": key, "id": key, "sport_key": "soccer_epl", "group": "soccer", "event": "A - B", "home": "A", "away": "B", "league": "L",
                "commence": f"{day}T19:00:00Z", "market": "h2h", "who": None, "point": None, "name": "A", "pick": "A", "odds": 2.0, "prob": .5,
                "sent": True, "date": day, "result": "win", "single": True} | kw
    track.save([srow("old-noted", old, played={"odds": 2.0, "stake": 1000, "at": "x"}, cur=FCFA), srow("old-plain", old), srow("recent", str(last))])
    keys = {r["key"] for r in track.load()}
    assert keys == {"old-noted", "recent"}, keys
    print("ok  5 rétention : un combiné / un pari du jour noté n'est jamais supprimé (fair_combos.json, tracked.json) : « depuis le début » reste vrai")


# ---------- 6. règlement : jamais « annulé » sans score ----------

def test_no_score_is_unknown_not_void():
    N.reset()
    D, ko = "2030-01-01", "2030-01-01T19:00:00Z"

    def row(key, name, odds, home, away, ev):
        return {"key": key, "id": ev, "sport_key": "soccer_epl", "group": "soccer", "event": f"{home} - {away}", "home": home, "away": away,
                "league": "L", "commence": ko, "market": "h2h", "who": None, "point": None, "name": name, "pick": name, "odds": odds, "prob": .5,
                "sent": True, "date": D, "result": None}
    track.save([row("a", "Alpha", 1.5, "Alpha", "Bravo", "m1"), row("b", "Gamma", 2.0, "Gamma", "Delta", "m2")])
    combo = {"tier": "T", "keys": ["a", "b"], "odds": 3.0, "prob": .3, "stake": None, "date": D, "result": None, "cur": FCFA,
             "played": {"odds": 3.0, "stake": 1000, "at": "x"}}
    store.save("bets.json", {"picks": [], "combos": [combo]})
    saved = (settle.FootballData.match, settle.fmmatch.match_stats)
    settle.FootballData.match = lambda self, b: None
    settle.fmmatch.match_stats = lambda b: None
    sc = {"soccer_epl": [{"id": "m1", "completed": True, "scores": [{"name": "Alpha", "score": "2"}, {"name": "Bravo", "score": "0"}]}]}
    try:
        bets, done = settle.settle_pending(N.at(12, day=12), scores_by_sport=sc, stats_for=lambda b: {})
    finally:
        settle.FootballData.match, settle.fmmatch.match_stats = saved
    c = bets["combos"][0]
    legs = {r["key"]: r["result"] for r in track.load()}
    assert legs == {"a": "win", "b": "unknown"}, legs
    assert c["result"] == "unknown" and money.gain(c) is None and "odds_final" not in c, c
    print("ok  6 jambe sans score après WAIT_DAYS : « unknown » (jamais « void » qui rendait le combiné gagné)")


# ---------- 7. odds_final sur la même base de prix que le combiné ----------

def test_odds_final_same_price_basis():
    N.reset()
    D, ko = "2030-01-01", "2030-01-01T19:00:00Z"

    def row(key, name, home, away, ev, typical, **kw):
        return {"key": key, "id": ev, "sport_key": "soccer_epl", "group": "soccer", "event": f"{home} - {away}", "home": home, "away": away,
                "league": "L", "commence": ko, "market": "h2h", "who": None, "point": None, "name": name, "pick": name, "odds": typical,
                "prob": .5, "sent": True, "date": D, "result": None} | kw

    def scores(*ids):
        return {"soccer_epl": [{"id": i, "completed": True, "scores": [{"name": n, "score": "2"}, {"name": m, "score": "0"}]}
                               for i, n, m in ids]}
    sc = scores(("m1", "Alpha", "Bravo"), ("m2", "Gamma", "Delta"))
    # combiné bâti au matin à la cote de référence (typique 1,75 x 1,60) ; le pari du jour passe ensuite aux cotes 1xBet (_upgrade_single : xodds)
    track.save([row("a", "Alpha", "Alpha", "Bravo", "m1", 1.75, xodds=2.1, single=True), row("b", "Gamma", "Gamma", "Delta", "m2", 1.6)])
    store.save("bets.json", {"picks": [], "combos": [{"tier": "T", "keys": ["a", "b"], "odds": 2.8, "prob": .3, "stake": None, "date": D,
                                                      "result": None, "cur": FCFA, "played": {"odds": 2.8, "stake": 1000, "at": "x"}}]})
    bets, _ = settle.settle_pending(N.at(8, day=3), scores_by_sport=sc, stats_for=lambda b: {})
    c = bets["combos"][0]
    assert c["result"] == "win" and abs(c["odds_final"] - 2.8) < 1e-9, c
    assert abs(bankroll.played_gain(c) - 1000 * (2.8 - 1)) < 1e-6, bankroll.played_gain(c)          # ratio 1 : aucune jambe annulée
    # combiné aux cotes 1xBet (xlegs) : les cotes 1xBet de l'envoi, pas la typique
    N.reset()
    track.save([row("a", "Alpha", "Alpha", "Bravo", "m1", 1.75, xodds=1.7), row("b", "Gamma", "Gamma", "Delta", "m2", 1.6, xodds=1.5)])
    store.save("bets.json", {"picks": [], "combos": [{"tier": "T", "keys": ["a", "b"], "odds": 1.7 * 1.5, "prob": .3, "stake": None, "date": D,
                                                      "result": None, "xlegs": [{"key": "a", "xodds": 1.7, "xref": None},
                                                                                {"key": "b", "xodds": 1.5, "xref": None}]}]})
    bets, _ = settle.settle_pending(N.at(8, day=3), scores_by_sport=sc, stats_for=lambda b: {})
    c = bets["combos"][0]
    assert c["result"] == "win" and abs(c["odds_final"] - 2.55) < 1e-9, c
    print("ok  7 odds_final : même base de prix que la cote du combiné (référence ou 1xBet), ratio 1 sans jambe annulée")


# ---------- 8. réutilisation des matchs entre combinés du matin ----------

MARKETS = [("totals", "Under", 3.5), ("totals", "Over", 1.5), ("double_chance", "H or Draw", None), ("team_sot_home", "Under", 4.5),
           ("team_shots_total", "Under", 24.5), ("team_shots_home", "Over", 12.5), ("team_sot_total", "Over", 7.5),
           ("alternate_totals_corners", "Over", 9.5)]


def gen_pronos(n=16, seed=2):
    r = random.Random(seed)
    out = []
    for i in range(n):
        for mk, nm, pt in r.sample(MARKETS, 4):
            p = round(r.uniform(0.60, 0.80), 4)
            x = round(1 / p * r.uniform(1.07, 1.2), 2)
            out.append(N.prono(i, market=mk, name=nm, point=pt, prob=p, p0=p, min_odds=round(1 / p * (1 + C.FAIR_MARGIN), 2), xodds=x,
                               xodds0=x, xev=round(p * x - 1, 4), key=f"m{i}|{mk}||{pt if pt is not None else ''}|{nm}",
                               pick=f"{mk} {nm} {pt}", score=p - 0.5, xref=[900 + i, 10, pt or 0]))
    return sorted(out, key=lambda c: -c["score"])


def reuse_violations(plan):
    """Matchs qui servent dans plusieurs combinés du matin contre les règles de CB.Usage (une jambe risquée une fois, sûre LEG_REUSE_MAX fois)."""
    u, bad = CB.Usage(), 0
    for _, combo in plan:
        if not combo:
            continue
        for c in combo[0]:
            if not u.allowed(c):
                bad += 1
        u.add(combo[0])
    return bad


def test_morning_combos_share_usage():
    N.reset()
    legacy = shared = 0
    for seed in range(1, 9):
        p = gen_pronos(18, seed)
        legacy += reuse_violations(CB.fair_plan(p) + CB.fair_shots_plan(p))                  # l'ancienne composition : deux Usage séparés
        shared += reuse_violations(app.fair_plan_all(p))
    assert legacy > 0, "le scénario de l'audit existe : les combinés « tirs » reprennent des jambes risquées déjà prises"
    assert shared == 0, shared
    # patch_combo : un remplaçant déjà pris (jambe risquée : 0,66 < LEG_SAFE_PROB) dans un autre combiné du jour n'est pas repris
    assert 0.66 < C.LEG_SAFE_PROB
    N.reset()
    cur = [N.prono(i, prob=0.66) for i in range(9)]
    cur[1] = cur[1] | {"xodds": None, "xodds0": None, "xev": None, "xref": None, "xnone": 1}
    base = [N.prono(i, prob=0.66) for i in range(4)]
    free = CB.patch_combo(base, cur, 5.0, 6.5, fresh_ids=GOT9)
    assert free is not None and free[3] and free[4], free
    taken = {c["id"] for c in free[4]}
    u = CB.Usage()
    u.add([c for c in cur if c["id"] in taken])
    again = CB.patch_combo(base, cur, 5.0, 6.5, fresh_ids=GOT9, usage=u)
    assert again is not None and again[4] and not ({c["id"] for c in again[4]} & taken), again
    u.add([c for c in cur if c["id"] not in {"m0", "m1", "m2", "m3"}])
    assert CB.patch_combo(base, cur, 5.0, 6.5, fresh_ids=GOT9, usage=u) is None
    # _update_combos : les autres combinés du jour comptent (leur dernière version envoyée)
    N.reset()
    money.set_daily(5000)
    risky = [N.prono(i, prob=0.66) for i in range(10)]
    fair.record_combos([(5, ([risky[i] for i in range(4)], 1.55 ** 4, 0.66 ** 4)), (12, ([risky[i] for i in range(4, 10)], 1.55 ** 6, 0.66 ** 6))],
                       DAY, {5: 500, 12: 300}, FCFA, at="09:00")
    cur = [risky[0], risky[1] | {"xodds": None, "xodds0": None, "xev": None, "xref": None, "xnone": 1}] + risky[2:]
    store.save("today.json", {"day": DAY, "picks": [], "pronos": cur})
    store.save(app.COMBO_UPDATES, {})
    other_ids = {l["id"] for r in fair.load_combos() if r["tier"] == 12 for l in r["legs"]}
    with Fakes(code="NEW03"):
        app._update_combos(DAY, cur, N.at(13), {f"m{i}" for i in range(10)})
    r5 = next(r for r in fair.load_combos() if r["tier"] == 5)
    assert not ({l["id"] for l in r5["legs"]} & other_ids), r5["keys"]            # AVANT : un remplaçant déjà dans le combiné 12
    assert r5.get("dead"), "aucun remplaçant libre : le combiné pas noté est déclaré injouable"
    print("ok  8 combinés du matin : un seul Usage pour les paliers et les « tirs » ; les remplaçants respectent les autres combinés du jour")


# ---------- 9. une cote écrite avec une espace des milliers ----------

def big_custom(total=11061.9):
    """Combi sur mesure « m1 » suivi, aux cotes 1xBet, de cote totale `total` (« combi 10000 »), pas de budget du jour."""
    N.reset()
    legs = [N.prono(i, xodds=total ** 0.5, odds=total ** 0.5, min_odds=total ** 0.5, xref=[900 + i, 10, 3.5]) for i in range(2)]
    row = fair.combo_row("m1", (legs, total, 0.0001), DAY) | {"custom": True, "target": 10000, "lo": 7500, "hi": 12500, "src": "pronos"}
    fair.record_custom(row, DAY)
    store.save("today.json", {"day": DAY, "picks": [], "pronos": N.pronos(7)})
    return legs


def test_thousands_space_in_odds():
    for args in (["11 061,90"], ["11", "061,90"], ["11 061,90"]):
        for strict in (False, True):
            assert played.parse_odds_stake(args, strict=strict) == (11061.9, None, None), (args, strict)
        assert played.parse_odds_stake(args + ["100"]) == (11061.9, 100, None), args
    assert played.parse_odds_stake(["2 500"], strict=True)[2] == "ambigu" and played.parse_odds_stake(["2 500"]) == (2.0, 500, None)
    assert played.parse_odds_stake(["5,40", "2 000"]) == (5.4, 2000, None) and played.parse_odds_stake(["5,40", "100,5"]) == (5.4, 100.5, None)
    # réponse « 11 061,90 » à la question de la mise (cote connue) : la cote entière est retenue, la mise est demandée
    big_custom()
    with Fakes():
        ask = chat.on_callback(f"joue|vm1|{DAY}", 777)
        assert ask.startswith(played.NEED_STAKE) and chat.get_pending()["odds"] == 11061.9, ask
        reply = chat.route("11 061,90")[1]
    assert reply.startswith(played.NEED_STAKE) and chat.get_pending()["odds"] == 11061.9, reply
    assert fair.load_combos()[0].get("played") is None, fair.load_combos()[0]
    with Fakes():
        assert chat.route("500")[1].startswith("✅ Noté")
    assert fair.load_combos()[0]["played"] == fair.load_combos()[0]["played"] | {"odds": 11061.9, "stake": 500}
    # la commande tapée : pareil
    big_custom()
    with Fakes():
        reply = app.handle_command("/joue vm1 11 061,90")
    assert reply.startswith(played.NEED_STAKE) and chat.get_pending()["odds"] == 11061.9 and fair.load_combos()[0].get("played") is None, reply
    with Fakes():
        assert chat.route("1000")[1].startswith("✅ Noté")
    assert fair.load_combos()[0]["played"]["odds"] == 11061.9
    print("ok  9 « 11 061,90 » = UNE cote de 11 061,9 (jamais cote 11 + mise 61,9), en réponse comme avec /joue")


# ---------- 10. cote absurde : confirmation avant de noter ----------

def test_absurd_odds_ask_confirmation():
    world_known()
    for cmd in ("joué sûr 1000", "/joue sur 1,5", "joué pari 2000", "/joue sur 5000", "joué sûr 1000000"):
        chat.clear_pending()
        played.command(["sur", "0"], DAY)
        played.command(["pari", "0"], DAY)
        with Fakes():
            r = str(app.handle_command(cmd))
        assert r.startswith(played.CONFIRM) and "oui" in r, (cmd, r)
        assert played.find("sur", DAY).item.get("played") is None and played.find("pari", DAY).item.get("played") is None, cmd
        assert chat.get_pending()["confirm"], chat.get_pending()
    # « oui » note la cote écrite, « non » abandonne
    chat.clear_pending()
    with Fakes():
        app.handle_command("joué sûr 1000")
        out = chat.route("oui")[1]
    assert out.startswith("✅ Noté") and played.find("sur", DAY).item["played"]["odds"] == 1000.0 and chat.get_pending() is None, out
    played.command(["sur", "0"], DAY)
    with Fakes():
        app.handle_command("joué sûr 1000")
        out = chat.route("non")[1]
    assert "pas noté" in out.lower() and played.find("sur", DAY).item.get("played") is None and chat.get_pending() is None, out
    # une autre cote à la place de la confirmation : elle est lue normalement
    with Fakes():
        app.handle_command("joué sûr 1000")
        out = chat.route("5,40")[1]
    assert out.startswith("✅ Noté") and played.find("sur", DAY).item["played"]["odds"] == 5.4
    # une cote plausible est notée tout de suite (comme avant)
    played.command(["sur", "0"], DAY)
    with Fakes():
        assert app.handle_command("joué sûr 5,40").startswith("✅ Noté") and app.handle_command("/joue pari 1,45 800").startswith("✅ Noté")
    # réponse libre à la question « à quelle cote ? » : la même garde
    played.command(["sur", "0"], DAY)
    chat.clear_pending()
    with Fakes():
        store.save("bets.json", {"picks": [], "combos": [N.combo(N.SUR, 5.0, 1000, FCFA)]})
        chat.on_callback(f"joue|sur|{DAY}", 5)
        out = chat.route("500")[1]
    assert out.startswith(played.CONFIRM) and played.find("sur", DAY).item.get("played") is None, out
    print("ok  10 cote > 3x ou < 1/3 de la cote du pari (ou > 1000 pour un pari simple) : confirmation « oui » avant de noter ; cote plausible : noté")


# ---------- 11. /combi et /bilan : saisies limites ----------

def test_combi_and_bilan_inputs():
    world_known()
    seen = []
    old = app.custom_combo
    app.custom_combo = lambda live, pronos_, target, day, **kw: (seen.append(target), "combi ok")[1]
    try:
        with Fakes():
            for t in ("/combi 0.01", "/combi 0,5", "/combi 0.99", "/combi 1", "/combi 1.0", "/combi 1,00"):
                r = app.handle_command(t)
                assert isinstance(r, str) and "1,01" in r and "Erreur" not in r and "combi ok" not in r, (t, r)
            for t, want in (("combi 10 000", 10000.0), ("/combi 3 000", 3000.0), ("/combi 20", 20.0), ("/combi 2,5", 2.5), ("/combi 1.01", 1.01),
                            ("combi 10000", 10000.0), ("/combi 10 000 000", 1e7)):
                assert app.handle_command(t) == "combi ok" and seen[-1] == want, (t, seen[-1:])
            for t in ("/combi", "/combi abc", "/combi -5", "/combi inf", "/combi nan"):
                assert app.handle_command(t) == "Exemple : /combi 30, ou /combi 20 tirs fautes", t
    finally:
        app.custom_combo = old
    # best_combo ne lève plus pour une fourchette sous 1
    assert CB.best_combo([N.pick(1) | {"odds": 1.5}, N.pick(2) | {"odds": 1.5}], 0.5, 0.9) is None
    assert CB.best_combo([N.pick(1) | {"odds": 1.5}, N.pick(2) | {"odds": 1.5}], 0.0) is None
    with Fakes():
        for t in ("/bilan 800000", "/bilan 9999999999", "/bilan 99999999999999999999999"):
            r = app.handle_command(t)
            assert isinstance(r, str) and "jours" in r and "Erreur" not in r, (t, r)
        assert "Bilan" in str(app.handle_command("/bilan 7")) or "BILAN" in str(app.handle_command("/bilan 7"))
    print("ok  11 /combi < 1,01 : message clair ; « combi 10 000 » / « /combi 3 000 » lus ; /bilan avec un nombre absurde : message clair")


# ---------- 12. « Mes paris » : la liste entière est rafraîchie ----------

def test_list_keeps_whole_keyboard():
    world_known()
    with Fakes():
        lst = played.todo(DAY)
    joue = [d for d in datas(lst.markup) if d.startswith("joue|")]
    assert joue == [f"joue|sur|{DAY}|l0", f"joue|pari|{DAY}|l0", f"joue|v5|{DAY}|l0"], joue
    with Fakes() as f:
        reply = chat.on_callback(joue[0], 500)                  # Sûr : cote 1xBet connue + mise conseillée : noté tout de suite
    assert reply.startswith("✅ Noté")
    mid, text, markup = f.rewrites[-1]
    d = datas(markup)
    assert mid == 500 and "✅ Sûr · joué à 5,06" in text and "▫️ Pari du jour ·" in text, text
    assert f"annule|sur|{DAY}|l0" in d and f"joue|sur|{DAY}|l0" in d and f"joue|pari|{DAY}|l0" in d and f"joue|v5|{DAY}|l0" in d, d
    assert f"xmajr|pari|{DAY}" in d and f"xmaj|5|{DAY}" in d and not f.edits, (d, f.edits)
    with Fakes() as f:                                          # ↩️ Annuler depuis la liste
        chat.on_callback(f"annule|sur|{DAY}|l0", 500)
    mid, text, markup = f.rewrites[-1]
    d = datas(markup)
    assert "joué à" not in text and f"annule|sur|{DAY}|l0" not in d and f"joue|sur|{DAY}|l0" in d and f"joue|pari|{DAY}|l0" in d, (text, d)
    # ✅ puis la cote tapée (pari sans cote 1xBet) : la réponse rafraîchit toute la liste
    N.reset()
    money.set_daily(5000)
    store.save("bets.json", {"picks": [], "combos": [N.combo(N.SUR, 5.0, 1000, FCFA), N.combo(N.COSTAUD, 13.0, 500, FCFA)]})
    with Fakes() as f:
        lst = played.todo(DAY)
        assert datas(lst.markup) == [f"joue|sur|{DAY}|l0", f"joue|costaud|{DAY}|l0"]
        assert chat.on_callback(f"joue|sur|{DAY}|l0", 600).startswith("À quelle cote")
        assert chat.route("5,40")[1].startswith("✅ Noté")
    mid, text, markup = f.rewrites[-1]
    d = datas(markup)
    assert mid == 600 and "✅ Sûr · joué à 5,40" in text and f"joue|costaud|{DAY}|l0" in d and f"annule|sur|{DAY}|l0" in d, (text, d)
    # le message d'un pari seul garde son clavier d'un pari
    N.reset()
    money.set_daily(5000)
    store.save("bets.json", {"picks": [], "combos": [N.combo(N.SUR, 5.0, 1000, FCFA)]})
    with Fakes() as f:
        chat.on_callback(f"joue|sur|{DAY}", 601)
        chat.route("5,40")
    assert f.edits and f.edits[-1][0] == 601 and not f.rewrites, (f.edits, f.rewrites)
    print("ok  12 Mes paris : noter / annuler / « Autre cote » depuis la liste refait toute la liste (texte et clavier), pas le clavier d'un seul pari")


# ---------- 13. attentes ----------

def test_pending_cleared_and_double_tap():
    world_known()
    chat.clear_pending()
    with Fakes():
        assert chat.on_callback(f"joue|sur|{DAY}", 10).startswith("✅ Noté")
        chat.forget_taps()                                      # l'utilisateur attend avant de toucher « ✏️ Autre cote »
        assert "À quelle cote as-tu finalement joué" in chat.on_callback(f"joue|sur|{DAY}", 10)
        assert chat.get_pending()["what"] == "sur"
        assert "Note annulée" in chat.on_callback(f"annule|sur|{DAY}", 10)
        assert chat.get_pending() is None, chat.get_pending()
        r = chat.route("5,40")[1]
    assert r.startswith("Appuie d'abord") and played.find("sur", DAY).item.get("played") is None, r
    # une note réussie du même pari referme sa question
    chat.set_pending("pari", DAY, 11)
    with Fakes():
        assert app.handle_command("/joue pari 1,45 800").startswith("✅ Noté")
    assert chat.get_pending() is None
    played.command(["pari", "0"], DAY)
    chat.set_pending("pari", DAY, 11)
    with Fakes():
        assert chat.on_callback(f"joue|pari|{DAY}", 12).startswith("✅ Noté")           # l'attente de ce pari tombe
    assert chat.get_pending() is None, chat.get_pending()
    # double appui sur « ✅ Je l'ai joué » : le 2e (même message, aussitôt) n'ouvre aucune attente
    played.command(["sur", "0"], DAY)
    chat.clear_pending()
    chat.forget_taps()
    with Fakes() as f:
        r1 = chat.on_callback(f"joue|sur|{DAY}", 21)
        r2 = chat.on_callback(f"joue|sur|{DAY}", 21)
    assert r1.startswith("✅ Noté") and r2 == "" and chat.get_pending() is None, (r1, r2, chat.get_pending())          # réponse vide : rien n'est envoyé
    assert played.find("sur", DAY).item["played"]["odds"] == 5.06
    print("ok  13 ↩️ Annuler et une note réussie referment la question ; un double appui n'ouvre pas d'attente")


# ---------- 14. texte libre ----------

def test_route_casual_texts():
    world_known()
    today = store.load("today.json", {})
    extra = N.pick(8, home="Lens Okoye", away="Nulville Gozo Deportes", event="Lens Okoye - Nulville Gozo Deportes", name="Lens Okoye",
                   key="e8|h2h||Lens Okoye") | {"id": "e8"}
    top = N.pick(9, home="Top Oss", away="Start Kristiansand", event="Top Oss - Start Kristiansand", name="Top Oss", key="e9|h2h||Top Oss") | {"id": "e9"}
    mainz = N.pick(10, home="05 Mainz", away="1860 Munich", event="05 Mainz - 1860 Munich", name="05 Mainz", key="e10|h2h||05 Mainz") | {"id": "e10"}
    store.save("today.json", today | {"picks": today["picks"] + [extra, top, mainz]})
    chat.set_pending("sur", DAY, 55)
    for text in ("ok", "le", "nul", "go", "a", "de", "yo", "fc", "un", "🙂", "👍", "oui", "lol"):
        cmd, reply = chat.route(text)
        assert cmd is None and str(reply).startswith("Je n'ai pas compris"), (text, cmd, reply)
        assert chat.get_pending() is not None, f"{text!r} ne referme pas la question ouverte"
    # un vrai nom d'équipe garde son analyse, et referme la question
    assert chat.route("Lens") == ("/analyse Lens", None) and chat.get_pending() is None
    assert chat.route("Home1") == ("/analyse Home1", None) and chat.route("Lens Okoy") == ("/analyse Lens Okoy", None)      # début d'un mot (4 lettres)
    # « mes paris » : la liste ; « paris » : les paris du jour
    assert chat.route("mes paris") == ("/joue", None) and chat.route("Mes Paris") == ("/joue", None) and chat.route("mes paris du jour") == ("/joue", None)
    assert chat.route("paris") == ("/top", None) and chat.route("paris du jour") == ("/top", None) and chat.route("top") == ("/top", None)
    # une équipe qui commence par un mot de commande
    assert chat.route("Top Oss") == ("/analyse Top Oss", None) and chat.route("Start Kristiansand") == ("/analyse Start Kristiansand", None)
    assert chat.route("Start") == ("/aide", None) and chat.route("bonjour") == ("/aide", None)
    cmd, reply = chat.route("Menu Futbol")
    assert cmd is None and str(reply).startswith("Je n'ai pas compris"), (cmd, reply)
    assert chat.route("bilan 7") == ("/bilan 7", None) and chat.route("combi 20") == ("/combi 20", None) and chat.route("budget 5000") == ("/budget 5000", None)
    assert chat.route("joué sûr 5,40 1000") == ("/joue sûr 5,40 1000", None) and chat.route("stats PSG") == ("/stats PSG", None)
    # des noms qui commencent par un chiffre ne sont pas des montants
    for text in ("05 Mainz", "1860 Munich", "1xbet", "96 Hannover", "1. FC Köln"):
        assert not chat.NUM.match(text), text
    for text in ("5,40", "5,40 2 000", "5,40 2.000 FCFA", "5,40 2000F", "5,40 2k", "5k", "1 000 f", "50 €", "11 061,90", "2000 francs"):
        assert chat.NUM.match(text), text
    chat.set_pending("sur", DAY, 55)
    cmd, reply = chat.route("05 Mainz")
    assert cmd == "/analyse 05 Mainz" and reply is None, (cmd, reply)
    print("ok  14 texte libre : « ok », « le », « nul »... ne lancent aucune analyse ni ne ferment la question ; « mes paris » = la liste ; équipes à nom de commande ou de chiffre")


# ---------- 15. textes à jour et devise FCFA ----------

def test_texts_and_currency():
    rows = [{"date": DAY, "result": "win", "tier": 5}, {"date": DAY, "result": "loss", "tier": 12}]
    line = settle.fair_combo_line(rows)
    assert line and "hors bankroll" not in line and "sans mise" not in line, line
    # « Mes paris » : le pied ne demande la cote que s'il y a une cote à demander
    world_known()
    with Fakes():
        known = str(played.todo(DAY))
    assert "envoie la cote obtenue" not in known and "Appuie sur ce que tu as joué (« ✏️ Autre cote » corrige la cote)" in known, known
    N.reset()
    money.set_daily(5000)
    store.save("bets.json", {"picks": [], "combos": [N.combo(N.SUR, 5.0, 1000, FCFA)]})
    assert "envoie la cote obtenue chez 1xBet" in str(played.todo(DAY))
    # la question de la mise ne dit pas « est noté » avant que la mise soit donnée
    world_known(budget=0)
    t = played.find("sur", DAY)
    ask = chat._ask(t)
    assert ask.startswith(played.NEED_STAKE) and "est noté" not in ask and "sera noté" in ask, ask
    # combi sur mesure retiré : plus de « ❌ Pas joué » dans la réponse
    big_custom()
    rows_ = fair.load_combos()
    rows_[0]["played"], rows_[0]["skipped"] = {"odds": 5.0, "stake": 100, "at": "x"}, True
    store.save(fair.COMBOS_FILE, rows_)
    assert "Pas joué" not in custom.track("m1", DAY)
    # devise : tout est en FCFA
    N.reset()
    assert "5 000 FCFA" in money.command(["5k"]) and money.daily() == 5000 and money.cur() == FCFA and store.load(money.FILE, {})["cur"] == FCFA
    assert "2 500 FCFA" in money.command(["2,5k"]) and money.daily() == 2500
    assert "5 000 FCFA" in money.command(["5000", "FCFA"]) and "5 000 FCFA" in money.command(["5", "000", "F"])
    for bad in (["50", "€"], ["5000", "euros"], ["50", "USD"]):
        out = money.command(bad)
        assert "FCFA" in out and "Budget du jour :" not in out and money.daily() == 5000 and money.cur() == FCFA, (bad, out)
    assert money.parse_amount(["5k"]) == (5000, None) and money.parse_amount(["5000"]) == (5000, None)
    assert played.parse_odds_stake(["5,40", "2k"]) == (5.4, 2000, None) and played.parse_odds_stake(["5,40", "2", "k"]) == (5.4, 2000, None)
    assert played.parse_odds_stake(["5,40", "2000", "FCFA"]) == (5.4, 2000, None) and played.parse_odds_stake(["5,40", "2000F"]) == (5.4, 2000, None)
    assert played.parse_odds_stake(["5,40", "2000", "euros"])[2] == "devise"
    world_known()
    with Fakes():
        chat.set_pending("sur", DAY, 5)
        out = chat.route("5,40 2k")[1]
    assert "mise 2 000 FCFA" in out and "K" not in out.replace("FCFA", ""), out
    print("ok  15 textes à jour (pied de Mes paris, question de mise, combi retiré, bilan sans « hors bankroll ») ; devise verrouillée FCFA (5k = 5 000, 2k = 2 000)")


# ---------- 16. longue liste « Mes paris » ----------

def test_long_list_pages():
    world_known()
    rows = fair.load_combos()
    tpl = next(r for r in rows if r.get("legs"))
    for i in range(1, 23):
        r = copy.deepcopy(tpl)
        r.update(tier=f"m{i}", custom=True, target=10 + i, lo=9, hi=12, src="pronos", date=DAY, result=None, min_odds=10 + i + 0.31, coupon=f"CODE{i:03d}X")
        r.pop("played", None)
        rows.append(r)
    store.save(fair.COMBOS_FILE, rows)
    with Fakes():
        lst = played.todo(DAY)
    pages = [lst] + list(getattr(lst, "more", []))
    assert len(pages) >= 2, [len(str(p)) for p in pages]
    names = []
    for k, page in enumerate(pages):
        txt = str(page)
        assert len(txt) <= 3900 and len(telegram._chunks(txt)) == 1, (k, len(txt))
        assert "Tes paris du jour" in txt.splitlines()[0] and (k == 0 or f"{k + 1}/{len(pages)}" in txt.splitlines()[0]), txt.splitlines()[0]
        for label, d in kb(page.markup):
            if d.startswith(("joue|", "annule|")):
                what = d.split("|")[1]
                assert d.endswith(f"|l{k}"), (k, d)
                name = label.split(" · Je l'ai joué")[0].removeprefix("✅ ").removeprefix("✏️ Autre cote : ").removeprefix("↩️ Annuler : ")
                assert any(ln.startswith(("▫️ ", "✅ ")) and f" {name} · " in ln for ln in txt.splitlines()), (k, what, name)   # le pari est listé sur la page de son bouton
                names.append(what)
    assert len(set(names)) == 3 + 22, len(set(names))                       # chaque pari a son bouton, sur une seule page
    last = pages[-1]
    assert "Appuie sur ce que tu as joué" in str(last)
    # un appui sur la 2e page refait cette page-là, pas la liste entière
    what = next(d for d in datas(pages[1].markup) if d.startswith("joue|")).split("|")[1]
    with Fakes() as f:
        chat.on_callback(f"joue|{what}|{DAY}|l1", 700)
        chat.route("5,40 1000")
    mid, text, markup = f.rewrites[-1]
    assert mid == 700 and "Tes paris du jour" in text.splitlines()[0] and f"annule|{what}|{DAY}|l1" in datas(markup), (text[:80], datas(markup)[:4])
    # l'envoi : une page = un message, ses boutons avec elle
    sent = []
    old_api, old_env = telegram.api, {k: os.environ.get(k) for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")}
    os.environ.update(TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="1")
    telegram.api = lambda method, http_timeout=40, **p: (sent.append((method, p)), {"message_id": len(sent)})[1]
    try:
        with Fakes():
            lst = played.todo(DAY)
        assert telegram.send(lst)
    finally:
        telegram.api = old_api
        for k, v in old_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    msgs = [p for m, p in sent if m == "sendMessage"]
    assert len(msgs) == len(pages) and all(len(p["text"]) <= 4096 and p.get("reply_markup") for p in msgs), [len(p["text"]) for p in msgs]
    assert [p["text"].splitlines()[0] for p in msgs] == [str(p).splitlines()[0] for p in pages]
    print("ok  16 liste « Mes paris » longue : pages de 12 paris au plus, chacune avec son en-tête, son pied et ses boutons ; un message par page")


# ---------- 17 à 21. 1xBet ----------

class Net:
    """http.fetch simulé pour xbet.fetch_day : `body(url, params)` -> http.Result ; 7 Ko comptés par requête ; appels gardés."""

    def __init__(self, body):
        self.body, self.calls = body, []

    def fetch(self, url, source, ttl, headers=None, check_robots=True):
        self.calls.append(url)
        w = http.WIRE.setdefault(xbet.DOMAIN, {"bytes": 0, "requests": 0})
        w["bytes"] += 7000
        w["requests"] += 1
        return self.body(url, dict(parse_qsl(urlparse(url).query)))

    def __enter__(self):
        self.old, self.proxy = http.fetch, os.environ.get("XBET_PROXY")
        http.fetch = self.fetch
        os.environ["XBET_PROXY"] = "http://user-GB-20:pw@proxy.test:80"          # avec un numéro de session Webshare
        store.save(xbet.USAGE, {})
        store.save(state.FILE, {})
        return self

    def __exit__(self, *a):
        http.fetch = self.old
        os.environ["XBET_PROXY"] = self.proxy
        http.PROXIES.pop(xbet.DOMAIN, None)


def x_events(ids, **kw):
    return [("soccer_epl", {"id": f"e{i}", "home_team": "A", "away_team": "B", "commence_time": "2026-10-09T19:00:00Z", "xgid": i} | kw) for i in ids]


def ok_json(url, obj):
    return http.Result(url, json.dumps(obj).encode(), 200)


MAINX = {"constId": 1, "eventGroups": [{"groupId": 1, "events": [[{"type": 1, "cf": 2.0}], [{"type": 2, "cf": 3.0}], [{"type": 3, "cf": 4.0}]]},
                                       {"groupId": 17, "events": [[{"type": 9, "cf": 1.9, "parameter": 2.5}, {"type": 10, "cf": 1.9, "parameter": 2.5}]]}],
         "subGamesForMainGame": [{"id": 951, "gameTypeId": 2, "subGameName": "Corners"}]}


def test_xbet_unexpected_json_and_subgame():
    with Net(lambda url, q: ok_json(url, [] if q["gameId"] == "2" else None if q["gameId"] == "4" else MAINX)):
        out = xbet.fetch_day(x_events([1, 2, 3, 4, 5]))
        assert sorted(out) == ["e1", "e3", "e5"], sorted(out)             # [] / null : pas de données pour ce match, les autres sont lus
        u = xbet.usage()
        assert u["bytes"] >= 5 * 7000 and u["requests"] >= 5, u            # la session est fermée : les données du relevé sont comptées
        assert state.health("xbet")["rows"] == 3
    # même chose quand une erreur inattendue (réponse mal formée) survient au milieu du relevé
    bad = dict(MAINX, eventGroups=[{"groupId": 1, "events": "oups"}])
    with Net(lambda url, q: ok_json(url, bad if q["gameId"] == "2" else MAINX)):
        out = xbet.fetch_day(x_events([1, 2, 3]))
        assert "e1" in out and "e3" in out and xbet.usage()["bytes"] >= 3 * 7000, (sorted(out), xbet.usage())
    # un sous-match (corners) en erreur 500 ne fait pas perdre les cotes du marché principal
    def sub500(url, q):
        return http.Result(url, None, status=500, error="HTTP 500") if q["gameId"] == "951" else ok_json(url, MAINX)
    ev = x_events([900])
    with Net(sub500):
        none = xbet.fetch_day(ev, wanted={"e900": {"totals"}})
        both = xbet.fetch_day(ev, wanted={"e900": {"totals", "alternate_totals_corners"}})
    assert ("totals", "Over", 2.5) in none["e900"]["px"] and ("totals", "Over", 2.5) in both["e900"]["px"], both
    assert ("h2h", "Draw", None) in both["e900"]["px"] and not any(k[0] == "alternate_totals_corners" for k in both["e900"]["px"]), both
    print("ok  17 1xBet : réponse [] / null / mal formée = pas de données pour ce match (les autres sont lus, les données comptées) ; un sous-match en erreur garde le marché principal")


def test_xbet_circuit_breaker():
    dead = lambda err: (lambda url, q: http.Result(url, None, error=err))
    with Net(dead("réseau indisponible (ConnectTimeout)")) as net:
        out = xbet.fetch_day(x_events(range(1, 19)), live=True)
        assert out == {} and len(net.calls) <= 8, len(net.calls)          # AVANT : 23 appels (6 sessions essayées, puis une tentative par match)
        assert xbet.usage().get("session") is None, xbet.usage()          # aucune session morte n'est retenue
        err = state.health("xbet")["last_error"]
        assert "interrompu" in err and "3" in err, err
    # ConnectionError de 1xBet lui-même : au plus une session Webshare essayée en plus, pas cinq
    with Net(dead("réseau indisponible (ConnectionError)")) as net:
        out = xbet.fetch_day(x_events(range(1, 19)))
        assert out == {} and len(net.calls) <= 5, len(net.calls)
        assert xbet.usage().get("session") is None
    # un proxy en panne puis qui revient : la session qui marche est retenue
    def flaky(url, q, state_={"n": 0}):
        state_["n"] += 1
        return http.Result(url, None, error="réseau indisponible (ProxyError)") if state_["n"] <= 2 else ok_json(url, MAINX)
    with Net(flaky):
        out = xbet.fetch_day(x_events([1, 2]))
        assert sorted(out) == ["e1", "e2"] and xbet.usage()["session"] == 22, xbet.usage()
    # numéro de session retenu hors des bornes (dérive des relevés précédents) : on repart de la session de base
    with Net(lambda url, q: ok_json(url, MAINX)):
        store.save(xbet.USAGE, xbet.usage() | {"session": 900})
        ses = xbet._Session()
        assert xbet.session_no(http.PROXIES.get(xbet.DOMAIN)) == 20, http.PROXIES.get(xbet.DOMAIN)
        ses.close("test")
    print("ok  18 1xBet : disjoncteur après 3 matchs de suite en échec, ConnectionError = une seule session essayée, numéro de session borné et retenu seulement s'il marche")


def test_xbet_find_markers_and_orientation():
    ko = 1791572400

    def ev(h, a):
        return {"id": "e", "home_team": h, "away_team": a, "commence_time": "2026-10-09T19:00:00Z"}

    def found(kh, ka, xh, xa, dt=300):
        return xbet.find(ev(kh, ka), [{"gid": 1, "home": xh, "away": xa, "ts": ko + dt}]) is not None
    for kh, ka, xh, xa in (("Brazil", "Argentina", "Brazil U20", "Argentina U20"), ("Spain", "Italy", "Spain Women", "Italy Women"),
                           ("Real Madrid", "Getafe", "Real Madrid Castilla", "Getafe B"), ("Arsenal", "Chelsea", "Arsenal W", "Chelsea W"),
                           ("Ajax", "PSV", "Jong Ajax", "Jong PSV"), ("Brazil", "Argentina", "Brazil U20", "Argentina"),
                           ("Barcelona", "Real Madrid", "Barcelona B", "Real Madrid Castilla"), ("Bayern Munich", "Dortmund", "Bayern Munich II", "Dortmund")):
        assert not found(kh, ka, xh, xa), (kh, ka, xh, xa)
    for kh, ka, xh, xa in (("Brazil U20", "Argentina U20", "Brazil U20", "Argentina U20"), ("Real Sociedad B", "Getafe B", "Real Sociedad B", "Getafe B"),
                           ("Man City U21", "Chelsea U21", "Manchester City U21", "Chelsea U21"), ("Málaga", "Espanyol", "Málaga", "Espanyol"),
                           ("Real Madrid", "Atletico Madrid", "Real Madrid", "Atletico Madrid"), ("Aldosivi", "Sarmiento", "CAAldosivi", "Sarmiento")):
        assert found(kh, ka, xh, xa), (kh, ka, xh, xa)
    assert not xbet.loose("Real Madrid", "Real Madrid Castilla") and xbet.loose("Aldosivi", "CAAldosivi")
    # sens inversé : jamais accepté quand 1xBet range le même match à l'envers (les deux orientations se ressemblent)
    for kh, ka, xh, xa in (("Real Madrid", "Atletico Madrid", "Atletico Madrid", "Real Madrid"),
                           ("Manchester City", "Manchester United", "Manchester United", "Manchester City"),
                           ("Sheffield United", "Sheffield Wednesday", "Sheffield Wednesday", "Sheffield United"),
                           ("Bristol City", "Bristol Rovers", "Bristol Rovers", "Bristol City"), ("Dundee", "Dundee United", "Dundee United", "Dundee"),
                           ("Barcelona", "Espanyol", "Espanyol", "Barcelona")):
        assert not found(kh, ka, xh, xa, dt=0), (kh, ka, xh, xa)
    # le bon sens est préféré quand les deux matchs sont proposés
    cands = [{"gid": 1, "home": "Atletico Madrid", "away": "Real Madrid", "ts": ko}, {"gid": 2, "home": "Real Madrid", "away": "Atletico Madrid", "ts": ko}]
    assert xbet.find(ev("Real Madrid", "Atletico Madrid"), cands)["gid"] == 2
    print("ok  19 1xBet : U20 / Women / W / B / II / Castilla / Jong ne se confondent plus avec l'équipe A ; un match rangé à l'envers n'est pas accepté")


def test_save_coupon_malformed_never_raises():
    bad = [[{"xref": "oops", "xodds": 1.5}], [{"xref": [1], "xodds": 1.5}], [{"xref": [1, "x", 3], "xodds": 1.5}],
           [{"xref": [1, 10, 3.5], "xodds": "abc"}], [{"xref": [1, 10, "p"], "xodds": 1.5}], [{"xref": 7, "xodds": 1.5}]]
    old_post = http.post
    posted = []
    http.post = lambda url, body, **kw: (posted.append(body), http.Result(url, json.dumps({"Success": True, "Value": "CODE9"}).encode(), 200))[1]
    try:
        for legs in bad:
            assert xbet.save_coupon(legs) is None, legs
        assert posted == []
        assert xbet.save_coupon([{"xref": [900, 10, 3.5], "xodds": 1.55}]) == "CODE9" and len(posted) == 1
    finally:
        http.post = old_post
    print("ok  20 save_coupon : une référence mal formée ne lève jamais (None) ; un coupon valide part toujours")


def test_partial_refresh_stamp():
    N.reset()
    pr = [N.prono(i) | {"xat": "09:30"} for i in range(9)]
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pr, "xbet_at": "09:30"})
    with Fakes():
        app.run_xbet_refresh(N.at(12), N.fake_fetch({"m0": {("totals", "Under", 3.5): 1.60}}), announce=False)
    today = store.load("today.json", {})
    by_id = {p["id"]: p for p in today["pronos"]}
    assert by_id["m0"]["xat"] == "12:00" and by_id["m0"]["xodds"] == 1.6 and all(by_id[f"m{i}"]["xat"] == "09:30" for i in range(1, 9)), by_id["m1"]
    unread = fmt.plain(str(fmt.pronos([by_id["m1"], by_id["m2"]], 8, when=today["xbet_at"])))
    assert "09:30" in unread and "12:00" not in unread, unread                 # AVANT : « Cotes 1xBet de 12:00 » alors qu'elles datent de 09:30
    mixed = fmt.plain(str(fmt.pronos([by_id["m0"], by_id["m1"]], 8, when=today["xbet_at"])))
    assert "09:30" in mixed and "12:00" in mixed and "relevé partiel" in mixed, mixed
    full = fmt.plain(str(fmt.pronos([by_id["m0"]], 8, when=today["xbet_at"])))
    assert "12:00" in full and "partiel" not in full
    print("ok  21 relevé partiel : chaque pronostic garde l'heure de SA lecture ; l'en-tête dit « relevé partiel » quand elles diffèrent")


# ---------- 22. matin isolé ----------

class Boom:
    """Remplace temporairement des fonctions de modules par une qui lève (`pairs` = [(module, nom)])."""

    def __init__(self, *pairs):
        self.pairs = pairs

    def __enter__(self):
        self.old = [(m, n, getattr(m, n)) for m, n in self.pairs]

        def boom(*a, **k):
            raise RuntimeError("panne simulée")
        for m, n, _ in self.old:
            setattr(m, n, boom)

    def __exit__(self, *a):
        for m, n, f in self.old:
            setattr(m, n, f)


def test_morning_isolation():
    scan = {"leagues": {}, "sports": [], "paid": [], "error": "test"}
    with Boom((monde, "build")):
        data, pronos_ = app.world_report(scan, [], date(2030, 1, 1), N.at(5))
    assert data is None and pronos_ == [], (data, pronos_)
    with Boom((monde, "save")):
        data, pronos_ = app.world_report(scan, [], date(2030, 1, 1), N.at(5))
    assert data is not None
    # morning_report : un échec ne tue pas les autres parties
    settled_world(n=2, noted_last=1)
    old_fill = app.freeclose.fill
    app.freeclose.fill = lambda rows, now: 0
    try:
        for label, pairs in (("learn.update", [(learn, "update")]), ("describe_simple", [(learn, "describe_simple")]),
                             ("settle_pending", [(settle, "settle_pending")]), ("results_message", [(app, "results_message")]),
                             ("fair_report", [(app, "fair_report")]), ("bankroll", [(bankroll, "apply_results")])):
            settled_world(n=2, noted_last=1)
            with Fakes(), Boom(*pairs):
                msgs, model = app.morning_report(N.at(8, day=5))
            assert any("Combinés à vérifier chez 1xBet — résultats" in str(m) for m in msgs), (label, [str(m)[:60] for m in msgs])
            assert isinstance(model, dict) or model is not None, label
    finally:
        app.freeclose.fill = old_fill
    # daily_done n'est écrit qu'APRÈS l'envoi : un arrêt pendant l'envoi permet de refaire le matin
    store.save(app.DAILY_FILE, {})
    sent = []

    def send_then_die(m):
        sent.append(m)
        if len(sent) == 2:
            raise KeyboardInterrupt
        return True
    old_send = telegram.send
    telegram.send = send_then_die
    try:
        try:
            app.deliver_morning(["m1", "m2", "m3"], date(2030, 1, 1), N.at(5))
        except KeyboardInterrupt:
            pass
        assert store.load(app.DAILY_FILE, {}).get("day") is None, store.load(app.DAILY_FILE, {})
        telegram.send = lambda m: True
        app.deliver_morning(["m1", "m2", "m3"], date(2030, 1, 1), N.at(5))
        assert store.load(app.DAILY_FILE, {})["day"] == "2030-01-01"
        store.save(app.DAILY_FILE, {})
        app.deliver_morning(["m1"], date(2030, 1, 1), N.at(5), sample=True)
        assert store.load(app.DAILY_FILE, {}).get("day") is None, "--sample n'écrit jamais le garde-fou"
    finally:
        telegram.send = old_send
    print("ok  22 matin : monde / apprentissage / règlement / rapports isolés (un échec n'emporte pas le message) ; daily_done écrit après l'envoi")


# ---------- 23. relevé manuel ----------

def test_manual_refresh_gap_and_started():
    N.reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": N.pronos(9)})
    calls = []

    def silent(events, deadline=None, wanted=None, live=False, fresh_min=None, max_matches=None):
        calls.append(1)
        return {}
    with Fakes() as f:
        out1 = app.manual_refresh(N.at(10), silent)
        assert "ne répond pas" in out1 and "xbet_manual" not in store.load("today.json", {}), (out1, store.load("today.json", {}))
        out2 = app.manual_refresh(N.at(10, 3), silent)               # 3 min plus tard : rien n'avait été lu, on peut réessayer
        assert "ne répond pas" in out2 and len(calls) == 2, (out2, calls)
        ok = app.manual_refresh(N.at(10, 6), N.fake_fetch(N.under_px()))
        assert ok.startswith("✅ Cotes 1xBet actualisées") and store.load("today.json", {}).get("xbet_manual"), ok
        again = app.manual_refresh(N.at(10, 9), silent)
        assert "déjà actualisées" in again and len(calls) == 2, again
    # tous les matchs ont commencé : on le dit (et rien n'est demandé à 1xBet)
    N.reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": N.pronos(9)})
    with Fakes() as f:
        out = app.manual_refresh(N.at(23), silent)
    assert "commencé" in out and "ne répond pas" not in out and not f.sent and len(calls) == 2, (out, f.sent)
    assert "xbet_manual" not in store.load("today.json", {})
    print("ok  23 relevé manuel : le délai entre deux relevés n'est armé que si 1xBet a répondu ; matchs tous commencés : message juste")


# ---------- 24. décalage Telegram ----------

def test_poll_saves_offset_only_when_changed():
    saves = []
    old_api, old_save, old_env = telegram.api, store.save, {k: os.environ.get(k) for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")}
    os.environ.update(TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="1")
    store.save = lambda name, obj, *a, **k: (saves.append(name), old_save(name, obj, *a, **k))[1]
    try:
        old_save("telegram.json", {"offset": 0})
        telegram.api = lambda method, http_timeout=40, **p: []
        assert telegram.poll(lambda t: None) == 0 and "telegram.json" not in saves, saves
        telegram.api = lambda method, http_timeout=40, **p: ([{"update_id": 5, "message": {"text": "x", "chat": {"id": 1}}}]
                                                             if method == "getUpdates" else {"message_id": 1})
        assert telegram.poll(lambda t: None) == 1 and "telegram.json" in saves and store.load("telegram.json", {})["offset"] == 6
        saves.clear()
        telegram.api = lambda method, http_timeout=40, **p: [{"update_id": 7, "message": {"text": "x", "chat": {"id": 999}}}]
        telegram.poll(lambda t: None)
        assert store.load("telegram.json", {})["offset"] == 8 and saves.count("telegram.json") == 1, saves       # un autre chat : l'offset avance quand même
    finally:
        telegram.api, store.save = old_api, old_save
        for k, v in old_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    print("ok  24 poll : telegram.json n'est réécrit que si le décalage a changé")


# ---------- 25. commande /backup ----------

def test_backup_command():
    sent = []
    old_doc = telegram.send_document
    telegram.send_document = lambda path, caption=None, chat_id=None: (sent.append((path, caption, chat_id)), {"message_id": 99})[1]
    try:
        # /backup et synonymes
        assert app.handle_command("/backup") is None
        assert len(sent) == 1 and sent[0][0].endswith("kelly_backup.zip")
        assert "Sauvegarde Kelly" in sent[0][1] and "Mo" in sent[0][1]
        assert app.handle_command("sauvegarde") is None
        assert len(sent) == 2
        assert app.handle_command("backup") is None
        assert len(sent) == 3

        # Base introuvable
        old_path = store.db_path
        store.db_path = lambda: "/fichier/inexistant/kelly.db"
        try:
            assert "introuvable" in app.handle_command("/backup")
        finally:
            store.db_path = old_path

        # Échec Telegram -> message d'erreur
        telegram.send_document = lambda *a, **k: (_ for _ in ()).throw(telegram.TelegramError("réseau coupé"))
        r = app.handle_command("/backup")
        assert "Échec de la sauvegarde" in r and "réseau coupé" in r
    finally:
        telegram.send_document = old_doc

    # telegram.send_document unitaire (validation des paramètres et gestion d'erreurs)
    old_env = {k: os.environ.get(k) for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")}
    for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        os.environ.pop(k, None)
    try:
        try:
            telegram.send_document(store.db_path())
            assert False, "devait lever TelegramError"
        except telegram.TelegramError as e:
            assert "manquant" in str(e)
    finally:
        for k, v in old_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    print("ok  25 commande /backup : envoi de kelly.db, gestion d'erreurs et synonymes")


TESTS = [test_noted_combo_never_replaced, test_update_keeps_note_on_rounding, test_report_announces_every_settled_combo,
         test_morning_total_includes_fair_combos, test_noted_rows_survive_retention, test_no_score_is_unknown_not_void,
         test_odds_final_same_price_basis, test_morning_combos_share_usage, test_thousands_space_in_odds, test_absurd_odds_ask_confirmation,
         test_combi_and_bilan_inputs, test_list_keeps_whole_keyboard, test_pending_cleared_and_double_tap, test_route_casual_texts,
         test_texts_and_currency, test_long_list_pages, test_xbet_unexpected_json_and_subgame, test_xbet_circuit_breaker,
         test_xbet_find_markers_and_orientation, test_save_coupon_malformed_never_raises, test_partial_refresh_stamp, test_morning_isolation,
         test_manual_refresh_gap_and_started, test_poll_saves_offset_only_when_changed, test_backup_command]


def main():
    failed = []
    for t in TESTS:
        try:
            t()
        except Exception:                           # noqa: BLE001 - on veut la liste complète des points qui échouent
            failed.append(t.__name__)
            print(f"FAIL {t.__name__}")
            traceback.print_exc(limit=4, file=sys.stdout)
    if failed:
        print(f"\n{len(failed)} test(s) en échec sur {len(TESTS)} : {', '.join(failed)}")
        sys.exit(1)
    print(f"tests audit (lot I) : {len(TESTS)} tests OK")


if __name__ == "__main__":
    main()
