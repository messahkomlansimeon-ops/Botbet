"""Corrections de la relecture, un test par point : (2) pas de mise sur une espérance négative, (3) lignes asiatiques en quarts écartées du live, (4) « ✅ Je l'ai
joué » ne note pas une cote trop vieille, (5) un combi sur mesure noté ne perd ni sa note ni ses paris à « Vérifier », (6) relance du matin : les combinés
déjà enregistrés sont renvoyés tels quels, (7) un seul Usage pour tous les tickets du jour, (10) /backup : copie cohérente, fichiers uniques, sans verrou.
Hors ligne (1xBet, Telegram et coupons simulés). Lancer : python -m tests.test_review_fixes"""
import copy
import os
import sqlite3
import sys
import tempfile
import traceback
import zipfile
from datetime import datetime, timezone

from tests import test_combi_track as T       # fixe DATA_DIR (dossier temporaire), proxy simulé, monde de test (pari, pronostic 💎, Fakes, combi sur mesure...)
from kelly import (app, chat, combos as CB, config as C, custom, fair, fmt, gapbets, live, money, played, settle, statswindow, store,  # noqa: E402
                   telegram, xprice)
C.NOVALUE_STAKE = 50.0       # les combinés du jeu d'essai n'ont pas de valeur au sens des voies : mise fixe optionnelle (défaut 0 = suivi en fantôme) pour exercer le flux de mise
C.NOVALUE_SEND = True       # les combinés des jeux d'essai n'ont pas de valeur au sens des voies : ils doivent être ENVOYÉS pour exercer les messages (défaut du bot : fantôme)

DAY, FCFA = T.DAY, "FCFA"
UTC = timezone.utc


# ---------- 2. espérance négative : aucune mise ----------

def test_negative_ev_no_stake():
    # money.allocate : mise 0 dès que proba x cote - 1 <= 0 ; le plancher CONF_MIN ne vaut que pour une espérance positive
    st, spent, kept = money.allocate([{"kind": "pari", "tier": "pari", "prob": 0.40, "odds": 2.0}], budget=5000)
    assert st == [0] and spent == 0 and kept == 5000, (st, spent, kept)
    assert money.allocate([{"kind": "pari", "tier": "pari", "prob": 0.50, "odds": 2.0}], budget=5000)[0] == [0], "espérance nulle : pas de mise"
    small = money.allocate([{"kind": "pari", "tier": "pari", "prob": 0.51, "odds": 2.0}], budget=5000)[0][0]
    assert small > 0, "espérance positive (+2 %) : mise (plancher CONF_MIN de la part)"
    mixed = money.allocate([{"kind": "pari", "tier": "pari", "prob": 0.40, "odds": 2.0}, {"kind": "pari", "tier": "pari", "prob": 0.60, "odds": 2.0}],
                           budget=5000)[0]
    alone = money.allocate([{"kind": "pari", "tier": "pari", "prob": 0.60, "odds": 2.0}], budget=5000)[0]
    assert mixed == [0, alone[0]], (mixed, alone)
    assert money.allocate([{"kind": "combo", "tier": "🎯", "prob": "x", "odds": 2.0}], budget=5000)[0] == [0], "donnée illisible : 0, jamais d'erreur"
    # les mots : « Kelly ne conseille pas de miser dessus » pour une espérance négative, « trop petite » sinon
    assert money.stake_words(0, 0.40, 2.0) == money.NO_EDGE and "espérance négative" in money.NO_EDGE
    assert money.stake_words(0, 0.60, 2.0) == "pas de mise (trop petite pour ton budget)" and money.stake_words(0) == "pas de mise (trop petite pour ton budget)"
    # sans budget ni capital (None) : pas de montant, mais l'espérance négative est quand même dite ; espérance positive : rien
    assert money.stake_words(None, 0.4, 2.0) == money.NO_EDGE and money.stake_words(None, 0.6, 2.0) is None and money.stake_words(None) is None
    assert money.stake_words(500, 0.4, 2.0, " (x)") == "500 FCFA (x)"
    assert app._stake_words(0, 0.40, 2.0) == money.NO_EDGE and gapbets.stake_txt(0, {"p_sharp": 0.4, "xodds": 2.0}) == money.NO_EDGE
    assert gapbets.stake_txt(0, {"p_sharp": 0.6, "xodds": 2.0}).startswith("pas de mise") and gapbets.stake_txt(None, {"p_sharp": 0.4, "xodds": 2.0}) == money.NO_EDGE
    # fmt : aucune ligne « Mise : … » pour une espérance négative
    for html in (str(fmt.combo("🛡️ SÛR", [T.pick(1)], 2.0, 0.40, stake_txt=money.NO_EDGE)), str(fmt.single(T.pick(1), stake_txt=money.NO_EDGE)),
                 str(fmt.fair_combo(2.0, [T.prono(1)], 0.4, stake_txt=money.NO_EDGE))):
        assert "Kelly ne conseille pas de miser dessus" in html and "Mise :" not in html, html
    assert "Mise : <b>500 FCFA</b>" in str(fmt.combo("🛡️ SÛR", [T.pick(1)], 2.0, 0.60, stake_txt="500 FCFA"))
    block = fmt.gap_block(None, T.pick(1) | {"edge": 0.1}, {"title": "t", "line": "l", "age": "a"}, None, money.NO_EDGE)
    assert "Kelly ne conseille pas de miser dessus" in block and "Mise" not in block, block
    # ligne de suivi d'une espérance négative (mise du matin 0) : « Mes paris » dit pourquoi
    T.reset()
    money.set_daily(5000)
    row = {"keys": ["k"], "tier": "🎯 COSTAUD", "prob": 0.4, "odds": 2.0, "stake": 0, "date": DAY}
    assert money.suggested(row) == 0 and app._stake_txt(row) == money.NO_EDGE
    assert app._stake_txt(row | {"prob": 0.6}) == "pas de mise (trop petite pour ton budget)"
    # pari du jour du matin à espérance négative (build_day) : stake 0, message sans ligne « Mise : »
    top = T.pick(1, prob=0.40, odds=2.0, score=0.5)
    old = (CB.make_pool, CB.plan)
    CB.make_pool, CB.plan = (lambda picks: list(picks)), (lambda picks, tiers=None, usage=None, rejected=None: [(t, None) for t, _, _ in C.COMBOS])
    try:
        with T.Fakes():
            msgs, sent_picks, _ = app.build_day([top], DAY)
    finally:
        CB.make_pool, CB.plan = old
    single = next(str(m) for m in msgs if "PARI DU JOUR" in str(m))
    assert "Kelly ne conseille pas de miser dessus" in single and "Mise :" not in single, single
    assert sent_picks[0]["stake"] == 0 and "cur" not in sent_picks[0], sent_picks[0]
    # combiné live à espérance négative : plus de « Mise : X » à côté de « Espérance négative »
    T.reset()
    money.set_daily(5000)
    raw = [live_game(i, 40 + i * 5, home=f"Team{i}", away=f"Other{i}") for i in range(1, 4)]
    old_best = CB.best_combo
    CB.best_combo = lambda cands, lo, hi, max_legs=None, min_legs=None: (cands[:2], 2.0, 0.40)
    try:
        with T.Fakes():
            msg = live.combo(2.0, DAY, get=lambda u: {"Value": raw}, model_for=lambda m, k: LIVE_PRED if k else None, now=LIVE_NOW)
    finally:
        CB.best_combo = old_best
    text = str(msg)
    assert "Espérance négative" in text and "Kelly ne conseille pas de miser dessus" in text and "Mise :" not in text, text
    assert store.load(custom.FILE, {})["items"]["m1"]["stake"] == 0
    print("ok  2 espérance négative : allocate rend 0 (plancher CONF_MIN seulement si espérance > 0), messages sans ligne « Mise : » (pari du jour, combinés, live)")


# ---------- 3. live : seulement les lignes en .5 ----------

LIVE_NOW = datetime(2026, 10, 10, 15, 30, tzinfo=UTC)
LIVE_PRED = {"pred": {"lh": 1.6, "la": 0.9, "q": 1.0, "n": [60, 60]}}


def live_game(gid, minute, score=(1, 0), home="Arsenal", away="Leeds", odds=None):
    cells = odds if odds is not None else [
        {"T": 1, "C": 1.30}, {"T": 2, "C": 4.6}, {"T": 3, "C": 11.0}, {"T": 4, "C": 1.05}, {"T": 5, "C": 1.22}, {"T": 6, "C": 3.6},
        {"T": 9, "P": 1.5, "C": 1.55}, {"T": 10, "P": 1.5, "C": 2.40}, {"T": 9, "P": 2.5, "C": 3.10}, {"T": 10, "P": 2.5, "C": 1.36},
        {"T": 180, "C": 3.0, "CV": "3.0"}, {"T": 181, "C": 1.38}]
    return {"I": gid, "LE": "England. Premier League", "O1E": home, "O2E": away, "S": int(LIVE_NOW.timestamp()) - minute * 60 - 900,
            "SC": {"TS": minute * 60, "CP": 1 if minute < 45 else 2, "FS": {"S1": score[0], "S2": score[1]},
                   "ST": [{"Key": 0, "Value": [{"ID": 71, "S1": "0", "S2": "0", "N": "Red card"}]}]}, "E": cells}


def test_live_quarter_lines():
    odds = [{"T": 9, "P": p, "C": 1.9} for p in (1.5, 2.0, 2.25, 2.5, 2.75, 3.0, 3.5)] + [{"T": 10, "P": p, "C": 1.9} for p in (1.5, 2.0, 2.25, 2.5, 2.75, 3.0, 3.5)]
    m = live.parse(live_game(1, 63, odds=odds))
    lines = sorted({k[2] for k in m["odds"] if k[0] == "totals"})
    assert lines == [1.5, 2.5, 3.5], lines                       # ni 2,0 / 3,0 (remboursement) ni 2,25 / 2,75 (demi-gain, demi-perte)
    p = live.probs(m, LIVE_PRED["pred"])
    assert {k[2] for k in p if k[0] == "totals"} == {1.5, 2.5, 3.5}
    # une ligne en quart injectée à la main n'est pas chiffrée comme une ligne en .5
    m["odds"][("totals", "Over", 2.25)] = (1.9, 9, 2.25)
    assert ("totals", "Over", 2.25) not in live.probs(m, LIVE_PRED["pred"])
    keep, seen, _ = live.legs([live_game(1, 63, odds=odds)], now=LIVE_NOW, data={}, model_for=lambda mm, k: LIVE_PRED if k else None)
    assert all(c["point"] in (None, 1.5, 2.5, 3.5) for c in keep + seen), [c["point"] for c in seen]
    print("ok  3 live : seulement les lignes de buts en .5 (2,25 / 2,75 / lignes entières écartées de la lecture et des probabilités)")


# ---------- 4. « ✅ Je l'ai joué » : jamais une cote trop vieille ----------

def morning_combo():
    T.reset()
    money.set_daily(5000)
    store.save("bets.json", {"picks": [], "combos": [{
        "tier": C.COMBOS[0][0], "keys": ["k1"], "odds": 5.06, "prob": 0.25, "stake": 1000, "cur": FCFA, "date": DAY, "result": None,
        "xlegs": [{"key": "k1", "xodds": 5.06, "xref": [1, 2, 3]}], "coupon": "C1", "at": "09:30"}]})


def test_stale_reference_odds():
    old_now = played._now
    try:
        # 7 h 30 après la lecture du matin (09:30 -> 17:00) : la cote n'est pas notée telle quelle, Kelly demande la cote obtenue
        morning_combo()
        played._now = lambda: datetime(2030, 1, 1, 17, 0, tzinfo=UTC)
        t = played.find("sur", DAY)
        assert t.known and t.read_at == "09:30" and played.read_age(t) == 450 and played.is_stale(t)
        with T.Fakes() as f:
            ask = chat.on_callback(f"joue|sur|{DAY}", 5)
        assert "date de plus de 3 h" in ask and "09h30" in ask and "À quelle cote as-tu joué" in ask and "1 000 FCFA" in ask, ask
        assert not played.find("sur", DAY).item.get("played"), "rien n'est noté sans la cote"
        p = chat.get_pending()
        assert p and p["what"] == "sur" and not p.get("odds"), p
        with T.Fakes():
            out = chat.route("5,40")[1]                    # la cote obtenue + la mise conseillée
        assert out.startswith("✅ Noté") and "💰 1 000 FCFA à 5,40" in out and "Cote lue par Kelly" not in out, out
        assert played.find("sur", DAY).item["played"]["odds"] == 5.4
        # cote lue il y a 1 h : noté tout de suite, avec l'heure de lecture dans la confirmation
        morning_combo()
        played._now = lambda: datetime(2030, 1, 1, 10, 30, tzinfo=UTC)
        assert not played.is_stale(played.find("sur", DAY))
        with T.Fakes():
            out = chat.on_callback(f"joue|sur|{DAY}", 5)
        assert out.startswith("✅ Noté") and "💰 1 000 FCFA à 5,06 → si ça passe : +4 060 FCFA" in out and "📈" not in out and "📉" not in out, out
        assert "Cote lue par Kelly à 09h30" in out and "✏️ Autre cote" in out, out
        assert played.find("sur", DAY).item["played"]["odds"] == 5.06
        # pile à la limite (180 min) : encore frais ; heure de lecture inconnue (ancienne ligne) : comme avant
        morning_combo()
        played._now = lambda: datetime(2030, 1, 1, 12, 30, tzinfo=UTC)
        assert not played.is_stale(played.find("sur", DAY))
        played._now = lambda: datetime(2030, 1, 1, 12, 31, tzinfo=UTC)
        assert played.is_stale(played.find("sur", DAY))
        bets = store.load("bets.json", {})
        bets["combos"][0].pop("at")
        store.save("bets.json", bets)
        t = played.find("sur", DAY)
        assert t.read_at is None and played.read_age(t) is None and not played.is_stale(t)
        # le jour suivant : une cote de la veille est périmée
        morning_combo()
        played._now = lambda: datetime(2030, 1, 2, 9, 0, tzinfo=UTC)
        assert played.is_stale(played.find("sur", DAY))
        # « /joue sur 5,40 » (cote donnée) n'est jamais refusé
        with T.Fakes():
            assert played.command(["sur", "5,40"], DAY).startswith("✅ Noté")
    finally:
        played._now = old_now
    print("ok  4 « ✅ Je l'ai joué » : cote lue il y a plus de XBET_COMBI_FRESH_MIN min -> « à quelle cote ? » ; cote fraîche -> notée avec son heure de lecture")


# ---------- 5. combi sur mesure noté : jamais modifié par « Vérifier » ----------

def test_noted_custom_never_dropped():
    # (a) pronostics (cote 1xBet des pronostics) : un pari retiré par 1xBet ne remplace rien dans le ticket noté
    T.reset()
    money.set_daily(5000)
    T.keep_custom(T.pronos(4), stake=250, cur=FCFA, at="09:30")
    store.save("today.json", {"day": DAY, "picks": [], "pronos": T.pronos(7)})
    snap = copy.deepcopy(fair.load_combos()[0])
    assert snap["played"] and snap["custom"]
    with T.Fakes(code="NEWC1", drop=("m1",)) as f:
        out = app.refresh_combo("m1", DAY, now=T.at(13, 30))
    row = fair.load_combos()[0]
    assert row == snap and not f.coupons, (row, snap)
    assert "ton ticket" in out and "retirée" not in out and "COMBINÉ À JOUR" not in out and "H1 – A1" in out, out
    assert store.load(app.COMBO_UPDATES, {}).get("tiers", {}).get("m1", {}).get("keys", snap["keys"]) == snap["keys"]
    # (b) cote qui bouge de plus que l'arrondi, tous les paris encore là : la note reste
    T.reset()
    money.set_daily(5000)
    T.keep_custom(T.pronos(4), stake=250, cur=FCFA, at="09:30")
    store.save("today.json", {"day": DAY, "picks": [], "pronos": T.pronos(7)})
    snap = copy.deepcopy(fair.load_combos()[0])
    with T.Fakes(code="NEWC2"):
        app.refresh_combo("m1", DAY, now=T.at(13, 30))
    assert fair.load_combos()[0] == snap, "mêmes paris : rien n'est réécrit"
    # (c) combi sur mesure de paris réels (src picks) : relu et affiché, jamais remplacé, note et code gardés
    T.reset()
    money.set_daily(5000)
    picks, priced = T.world()
    xprice.price_picks(picks, fetch=T.fake_fetch(priced))
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": [], "picks_at": "09:30"})
    with T.Fakes():
        app.custom_combo(picks, [], 5.0, DAY, when="09:30")
        custom.track("m1", DAY)
    snap = copy.deepcopy(fair.load_combos()[0])
    assert snap["played"] and snap["src"] == "picks"
    cheaper = {"e1": {("h2h", "HOME", None): 1.2}, "e2": {("h2h", "HOME", None): 1.45}, "e3": {("h2h", "HOME", None): T.XB}, "e4": {("h2h", "HOME", None): T.XB}}
    with T.Fakes(code="VERIF9") as f:
        out = app.refresh_combo("m1", DAY, now=T.at(13), fetch=T.fake_fetch(cheaper))
    assert fair.load_combos()[0] == snap, "ticket noté : ligne, note, paris et code inchangés"
    assert store.load(app.COMBO_UPDATES, {}).get("tiers", {}).get("m1") is None
    assert "Ton ticket" in out and "ne changent pas" in out and "Moins bien payé" in out and "VERIF9" in str(out), out
    # garde-fous de la couche fair et de _record_version
    assert fair.is_noted(snap) and not fair.is_noted(snap | {"skipped": True}) and not fair.is_noted({})
    row = copy.deepcopy(snap)
    assert fair.update_combo(row, ([T.prono(9)], 9.9, 0.5), "14:00", "X") == snap and row == snap
    rows, sent = [row], {"day": DAY, "tiers": {}}
    store.save(fair.COMBOS_FILE, rows)
    assert app._record_version(sent, row, rows, ([T.prono(9)], 9.9, 0.5), "14:00", "X") is False and sent["tiers"] == {} and row == snap
    # le comportement DROPPED_NOTE reste pour une note retirée du bilan (skipped) : un autre ticket
    row["skipped"] = True
    assert app._record_version(sent, row, rows, ([T.prono(9)], 9.9, 0.5), "14:00", "X") is True and "played" not in row and row["keys"] == [T.prono(9)["key"]]
    print("ok  5 combi sur mesure noté : « Vérifier ce combiné » ne retire ni sa note ni ses paris (pronostics et paris réels), DROPPED_NOTE gardé hors note")


# ---------- 6. relance du matin : les combinés déjà envoyés sont renvoyés tels quels ----------

def other_plan(picks):
    """Un plan différent de celui d'origine (autres paris) : ce que donnerait une relance avec d'autres cotes."""
    legs = sorted(picks, key=lambda c: c["key"])[-2:]
    return [(t, (legs, round(legs[0]["odds"] * legs[1]["odds"], 2), 0.4) if i == 0 else None) for i, (t, _, _) in enumerate(C.COMBOS)]


def test_relaunch_resends_recorded_combos():
    T.reset()
    money.set_daily(5000)
    picks, priced = T.world()
    xprice.price_picks(picks, fetch=T.fake_fetch(priced))
    with T.Fakes(code="MORN1"):
        msgs1, sent_picks1, sent1 = app.build_day(picks, DAY, odds_time="09:30")
    assert sent1 and sent1[0]["coupon"] == "MORN1"
    settle.record_combos(sent1, DAY, replace=True)
    first = store.load("bets.json", {})["combos"][0]
    keys1 = list(first["keys"])
    # relance : le plan serait différent (autres cotes / autres paris), mais le combiné enregistré est renvoyé tel quel
    orig_plan = CB.plan
    CB.plan = lambda p, tiers=None, usage=None, rejected=None: other_plan(p)
    try:
        assert [c["key"] for c in other_plan(picks)[0][1][0]] != keys1, "le plan de la relance est bien différent de l'envoi d'origine"
        with T.Fakes(code="OTHER") as f:
            msgs2, _, sent2 = app.build_day(picks, DAY, odds_time="09:55")
    finally:
        CB.plan = orig_plan
    assert [c["keys"] for c in sent2] == [keys1] and sent2[0]["tier"] == first["tier"], sent2
    assert sent2[0]["coupon"] == "MORN1" and sent2[0]["at"] == "09:30" and sent2[0]["odds"] == first["odds"] and sent2[0].get("xlegs") == first.get("xlegs")
    assert [c["id"] for c in picks if c["key"] in keys1] not in f.coupons, "même code coupon : aucun nouveau code demandé pour ce combiné"
    text2 = "\n".join(str(m) for m in msgs2)
    assert "MORN1" in text2 and all(first_leg in text2 for first_leg in (picks[0]["home"],)), text2
    settle.record_combos(sent2, DAY, replace=True)
    rows = [c for c in store.load("bets.json", {})["combos"] if c["date"] == DAY]
    assert len(rows) == 1 and rows[0]["keys"] == keys1 and rows[0]["coupon"] == "MORN1", rows
    # un jambe disparue (match commencé...) : le combiné est reconstruit ; pas noté -> la ligne de suivi prend la nouvelle version
    gone = [c for c in picks if c["key"] != keys1[0]]
    CB.plan = lambda p, tiers=None, usage=None, rejected=None: other_plan(p)
    try:
        with T.Fakes(code="NEW3"):
            _, _, sent3 = app.build_day(gone, DAY, odds_time="10:30")
    finally:
        CB.plan = orig_plan
    assert sent3 and sent3[0]["keys"] != keys1, sent3
    settle.record_combos(sent3, DAY, replace=True)
    row3 = next(c for c in store.load("bets.json", {})["combos"] if c["date"] == DAY and c["tier"] == first["tier"])
    assert row3["keys"] == sent3[0]["keys"] and row3.get("coupon") == sent3[0].get("coupon") and row3["keys"] != keys1, row3
    # ... mais noté : le ticket joué reste le suivi, rien de nouveau n'est envoyé pour ce palier et la ligne n'est pas touchée
    bets = store.load("bets.json", {})
    bets["combos"] = [first | {"played": {"odds": 5.0, "stake": 500, "at": "x"}}]
    store.save("bets.json", bets)
    CB.plan = lambda p, tiers=None, usage=None, rejected=None: other_plan(p)
    try:
        with T.Fakes(code="NEW4"):
            msgs4, _, sent4 = app.build_day(gone, DAY, odds_time="10:40")
    finally:
        CB.plan = orig_plan
    assert not sent4, sent4
    settle.record_combos(sent3, DAY, replace=True)
    kept = next(c for c in store.load("bets.json", {})["combos"] if c["date"] == DAY and c["tier"] == first["tier"])
    assert kept["keys"] == keys1 and kept["played"]["stake"] == 500, kept
    # sans replace (comportement d'avant) : le premier envoyé reste
    store.save("bets.json", {"picks": [], "combos": [first]})
    settle.record_combos(sent3, DAY)
    assert store.load("bets.json", {})["combos"][0]["keys"] == keys1

    # combinés à vérifier (fair_combos.json) : mêmes règles
    T.reset()
    money.set_daily(5000)
    legs = T.pronos(4)
    other = [T.prono(i) for i in (4, 5, 6)]
    combo_a = (legs, round(1.55 ** 4, 3), 0.72 ** 4)
    combo_b = (other, round(1.55 ** 3, 3), 0.72 ** 3)
    fair.record_combos([(5, combo_a)], DAY, {5: 500}, FCFA, coupons={5: "FAIRC"}, at="09:30")
    pronos = T.pronos(7)
    again = app._recorded_fair(DAY)
    sent = app._resend_fair(pronos, again, [(5, combo_b), (12, None)])
    assert [t for t, _ in sent] == [5, 12] and sent[1][1] is None
    assert [c["key"] for c in sent[0][1][0]] == [c["key"] for c in legs] and sent[0][1][1] == again["5"]["min_odds"] and sent[0][1][2] == again["5"]["prob"]
    # replace : mêmes paris -> la ligne est gardée (nouvelle mise), autres paris -> remplacée si pas notée, gardée si notée
    fair.record_combos([(5, combo_a)], DAY, {5: 800}, FCFA, coupons={5: "ZZZ"}, at="10:00", replace=True)
    r = fair.load_combos()[0]
    assert r["stake"] == 800 and r["coupon"] == "FAIRC" and r["at"] == "09:30" and len(fair.load_combos()) == 1, r
    fair.record_combos([(5, combo_b)], DAY, {5: 300}, FCFA, coupons={5: "NEWF"}, at="10:30", replace=True)
    r = fair.load_combos()[0]
    assert r["keys"] == [c["key"] for c in other] and r["coupon"] == "NEWF" and r["stake"] == 300 and len(fair.load_combos()) == 1, r
    rows = fair.load_combos()
    rows[0]["played"] = {"odds": 3.0, "stake": 100, "at": "x"}
    store.save(fair.COMBOS_FILE, rows)
    fair.record_combos([(5, combo_a)], DAY, {5: 900}, FCFA, coupons={5: "LATE"}, at="11:00", replace=True)
    r = fair.load_combos()[0]
    assert r["keys"] == [c["key"] for c in other] and r["coupon"] == "NEWF" and r["played"], "noté : jamais remplacé"
    sent = app._resend_fair(pronos, app._recorded_fair(DAY), [(5, combo_a)])        # noté et plus bâtissable : rien de nouveau
    assert sent[0][1] is not None and [c["key"] for c in sent[0][1][0]] == [c["key"] for c in other]
    sent = app._resend_fair([p for p in pronos if p["key"] != other[0]["key"]], app._recorded_fair(DAY), [(5, combo_a)])
    assert sent == [(5, None)], "ticket noté dont un pari a disparu : aucun autre combiné n'est envoyé à sa place"
    # sans replace : le premier envoyé reste (comportement d'avant, statswindow)
    fair.record_combos([(5, combo_a)], DAY, {5: 1}, FCFA)
    assert fair.load_combos()[0]["keys"] == [c["key"] for c in other]
    print("ok  6 relance du matin : combinés enregistrés renvoyés tels quels (mêmes paris, code, heure) ; reconstruit seulement si une jambe a disparu, ligne remplacée "
          "si pas notée, gardée si notée")


# ---------- 7. un seul Usage pour tous les tickets du jour ----------

def test_shared_usage():
    T.reset()
    risky = {"id": "e1", "key": "e1|x", "prob": 0.60}
    safe = {"id": "e2", "key": "e2|x", "prob": 0.80}
    u = CB.Usage()
    u.add([risky, safe])
    assert not u.allowed(risky) and u.allowed(safe)
    # le matin : pari du jour et écart sharp comptés dans l'Usage de la journée, après les combinés réels
    picks = [T.pick(i, score=0.5 - i / 100, prob=0.80, edge=0.2) for i in range(1, 8)]
    gap = {"key": picks[6]["key"], "edge": 0.1}
    old = CB.plan
    CB.plan = lambda p, tiers=None, usage=None, rejected=None: [(t, None) for t, _, _ in C.COMBOS]
    try:
        quality, top, top_gap, shown, plan, rejected, usage = app.morning_plan(picks, [gap, {"key": top_key(picks)}])
    finally:
        CB.plan = old
    assert top and usage.by_leg[CB.leg_id(top)] == 1 and usage.by_leg[CB.leg_id(picks[6])] == 1, usage.by_leg
    assert [g["key"] for g in shown] == [picks[6]["key"]] and len(top_gap) == 1
    # fair_plan_all part de cet Usage (et non d'un Usage vide)
    seen = {}
    old_fp, old_sp = CB.fair_plan, CB.fair_shots_plan
    CB.fair_plan = lambda pronos, tiers=None, usage=None: seen.setdefault("a", usage) and [] or []
    CB.fair_shots_plan = lambda pronos, tiers=None, usage=None: seen.setdefault("b", usage) and [] or []
    CB.plan = lambda p, tiers=None, usage=None, rejected=None: [(t, None) for t, _, _ in C.COMBOS]
    try:
        app.fair_plan_all(T.pronos(3), picks, [gap])
    finally:
        CB.fair_plan, CB.fair_shots_plan, CB.plan = old_fp, old_sp, old
    assert seen["a"] is seen["b"] and CB.leg_id(top) in seen["a"].by_leg and CB.leg_id(picks[6]) in seen["a"].by_leg, seen
    # end to end : une jambe risquée prise par un combiné réel n'est reprise par aucun combiné à vérifier (ni par un combiné « tirs »)
    risky_picks = [T.pick(i, score=0.9 - i / 100, prob=0.62, edge=0.05, odds=1.8) for i in range(1, 7)]
    real_legs = [risky_picks[0], risky_picks[1]]
    old_pool = CB.make_pool

    def fake_plan(p, tiers=None, usage=None, rejected=None):
        usage.add(real_legs)
        return [(C.COMBOS[0][0], (real_legs, 3.24, 0.38))] + [(t, None) for t, _, _ in C.COMBOS[1:]]
    prons = [T.prono(i, id=f"e{i + 1}", key=risky_picks[i]["key"], prob=0.62, p0=0.62) for i in range(6)]      # la MÊME jambe (clé) que celle des combinés réels
    CB.plan, CB.make_pool = fake_plan, (lambda p: list(p))
    try:
        shared = app.fair_plan_all(prons, risky_picks, [])
        alone = app.fair_plan_all(prons)                 # avant : un Usage à part, sans les combinés réels
    finally:
        CB.plan, CB.make_pool = old, old_pool
    used = {c["id"] for _, combo in shared if combo for c in combo[0]}
    used_alone = {c["id"] for _, combo in alone if combo for c in combo[0]}
    assert used and used_alone & {"e1", "e2"}, (used, used_alone)       # sans l'Usage commun, les jambes risquées de e1 / e2 étaient reprises
    assert not (used & {"e1", "e2"}), used
    print("ok  7a Usage du matin : combinés réels, puis pari du jour et écarts sharp, repris par les combinés à vérifier")


def top_key(picks):
    return max(picks, key=lambda c: c["score"])["key"]


def test_day_usage_all_families():
    T.reset()
    picks = [T.pick(i, prob=0.62) for i in range(1, 8)]               # jambes risquées (< LEG_SAFE_PROB)
    safe_pick = T.pick(8, prob=0.80)
    store.save("today.json", {"day": DAY, "picks": picks + [safe_pick], "pronos": []})
    store.save("bets.json", {"picks": [], "combos": [{"tier": "🛡️ SÛR", "keys": [picks[0]["key"], safe_pick["key"]], "odds": 3.0, "prob": 0.3, "date": DAY,
                                                          "result": None}]})
    fair.record_combos([(5, ([T.prono(1, id="e2", key="e2|x"), T.prono(2, id="e8", key="e8|x", prob=0.8)], 2.4, 0.5))], DAY)
    rows = fair.load_combos()
    rows.append(rows[0] | {"tier": "m1", "custom": True, "keys": ["e6|x"], "legs": [T.prono(9, id="e6", key="e6|x")]})      # la ligne de suivi d'un combi sur mesure joué ne compte pas deux fois (il compte par combi_sent.json)
    store.save(fair.COMBOS_FILE, rows)
    store.save("tracked.json", [
        {"key": picks[2]["key"], "id": "e3", "prob": 0.62, "date": DAY, "sent": True, "single": True, "result": None},
        {"key": picks[3]["key"], "id": "e4", "prob": 0.62, "date": DAY, "sent": True, "sharpgap": True, "single": False, "result": None},
        {"key": picks[4]["key"], "id": "e5", "prob": 0.62, "date": "2029-12-31", "sent": True, "single": True, "result": None}])    # un autre jour : ne compte pas
    u = app.day_usage(DAY)
    ks = {i: next(c["key"] for c in picks + [safe_pick] if c["id"] == i) for i in ("e1", "e3", "e4", "e5", "e8")}
    assert set(u.by_leg) == {ks["e1"], ks["e8"], "e2|x", "e8|x", ks["e3"], ks["e4"]}, u.by_leg          # réels, à vérifier / stats, pari du jour, écart sharp ; ni sur mesure ni autre jour
    for k in (ks["e1"], "e2|x", ks["e3"], ks["e4"]):
        assert not u.allowed({"key": k, "prob": 0.62}), k                       # une jambe risquée : un seul ticket, toutes familles confondues
    assert u.by_leg[ks["e8"]] == 1 and u.allowed({"key": ks["e8"], "prob": 0.80}), "sûre : jusqu'à LEG_REUSE_MAX"
    u.add([{"key": ks["e8"]}, {"key": ks["e8"]}])
    assert u.by_leg[ks["e8"]] == 3 and not u.allowed({"key": ks["e8"], "prob": 0.70}) and u.allowed({"key": ks["e8"], "prob": 0.85}), "3 tickets : plafond des sûres, pas des très sûres (5)"
    assert u.allowed({"key": ks["e5"], "prob": 0.62}) and u.allowed({"key": "e6|x", "prob": 0.62})
    # _others_usage (relevé de 15:00, « Vérifier ») : les mêmes tickets, sauf la ligne refaite
    rows = fair.load_combos()
    assert "e2|x" not in app._others_usage(rows, rows[0], DAY, {"day": DAY, "tiers": {}}).by_leg and ks["e1"] in app._others_usage(rows, rows[0], DAY, {"day": DAY, "tiers": {}}).by_leg
    # la fenêtre des stats part du même Usage
    seen = {}
    old = CB.fair_stats_window
    CB.fair_stats_window = lambda pronos, usage=None, accept=None: (seen.setdefault("u", usage), None)[1]
    try:
        today = {"day": DAY, "pronos": [], "picks": picks}
        statswindow.plan_combo(app, today, {"combos": [], "announced": []}, fair.load_combos(), datetime(2030, 1, 1, 14, 0, tzinfo=UTC))
    finally:
        CB.fair_stats_window = old
    assert set(seen["u"].by_leg) >= {ks["e1"], ks["e8"], "e2|x", ks["e3"], ks["e4"]}, seen
    print("ok  7b statswindow / relevé : Usage de tous les tickets du jour (combinés réels, à vérifier, stats, pari du jour, écarts sharp)")


# ---------- 10. /backup ----------

def test_backup_consistent_unique_and_lockless():
    T.reset()
    store.save("bets.json", {"picks": [], "combos": [{"tier": "x", "keys": ["a"], "date": DAY}]})
    seen = []
    old = telegram.send_document

    def fake_send(path, caption=None, chat_id=None):
        tmp = os.path.dirname(path)
        names = sorted(os.listdir(tmp))
        with zipfile.ZipFile(path) as zf:
            assert zf.namelist() == ["kelly.db"]
            out = os.path.join(tmp, "check.db")
            with open(out, "wb") as f:
                f.write(zf.read("kelly.db"))
        con = sqlite3.connect(out)
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        docs = [r[0] for r in con.execute("SELECT name FROM docs")]
        con.close()
        store.save("written_during_upload.json", {"x": 1})        # un autre processus écrit pendant l'envoi : la copie n'est pas touchée
        seen.append((path, names, ok, docs, caption))
        return {"message_id": 1}

    telegram.send_document = fake_send
    try:
        assert app.handle_command("/backup") is None
        assert app.handle_command("backup") is None
        # sans verrou : le serveur répond à « backup » même quand un passage tient le verrou des données (chat.quick), l'envoi ne bloque personne
        with store.data_lock(wait=0):
            assert chat.quick("/sauvegarde") == "✅ Sauvegarde envoyée." and chat.quick("backup") == "✅ Sauvegarde envoyée."
        assert chat.quick("hello") is None
    finally:
        telegram.send_document = old
    assert len(seen) == 4, seen
    for path, names, ok, docs, caption in seen:
        assert path.endswith("kelly_backup.zip") and ok == "ok" and "bets.json" in docs and "Sauvegarde Kelly" in caption, (path, ok, docs)
        assert names == sorted(names) and "kelly_backup.zip" in names and len(names) == 2, names      # le zip + la copie de la base (mkstemp)
        assert not os.path.exists(os.path.dirname(path)), "fichiers temporaires effacés"
    assert len({os.path.dirname(s[0]) for s in seen}) == 4, "un dossier temporaire par sauvegarde (jamais un chemin fixe)"
    assert not os.path.exists(os.path.join(tempfile.gettempdir(), "kelly_backup.zip")), "plus de chemin fixe /tmp/kelly_backup.zip"
    # échec d'envoi : message d'erreur ET nettoyage
    dirs = []

    def boom(path, caption=None, chat_id=None):
        dirs.append(os.path.dirname(path))
        raise telegram.TelegramError("réseau coupé")

    telegram.send_document = boom
    try:
        r = app.handle_command("/backup")
        assert "Échec de la sauvegarde" in r and "réseau coupé" in r and not os.path.exists(dirs[0])
        assert "Échec de la sauvegarde" in chat.quick("backup")
    finally:
        telegram.send_document = old
    # base introuvable
    old_path = store.db_path
    store.db_path = lambda: "/fichier/inexistant/kelly.db"
    try:
        assert "introuvable" in app.handle_command("/backup") and "introuvable" in chat.quick("backup")
    finally:
        store.db_path = old_path
    print("ok  10 /backup : copie cohérente (API de sauvegarde SQLite), dossier temporaire unique effacé, envoyée sans le verrou des données")


TESTS = [test_negative_ev_no_stake, test_live_quarter_lines, test_stale_reference_odds, test_noted_custom_never_dropped,
         test_relaunch_resends_recorded_combos, test_shared_usage, test_day_usage_all_families, test_backup_consistent_unique_and_lockless]


def main():
    failed = []
    for t in TESTS:
        try:
            t()
        except Exception:                           # noqa: BLE001 - on veut la liste complète des points qui échouent
            failed.append(t.__name__)
            print(f"FAIL {t.__name__}")
            traceback.print_exc(limit=6, file=sys.stdout)
    if failed:
        print(f"\n{len(failed)} test(s) en échec sur {len(TESTS)} : {', '.join(failed)}")
        sys.exit(1)
    print(f"tests relecture : {len(TESTS)} tests OK")


if __name__ == "__main__":
    main()
