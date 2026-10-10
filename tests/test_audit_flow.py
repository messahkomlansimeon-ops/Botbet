"""Audit du flux Telegram : combinés du matin renvoyés (dernière version envoyée comme base, suivi, boutons), relevé manuel borné,
pronostics gardés, textes des combinés aux cotes 1xBet, boutons conservés, suivi fair.json, nouvel essai de Telegram.
Hors ligne. Lancer : python -m tests.test_audit_flow"""
import os
import tempfile
import time
from datetime import date, datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-flow-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-flow-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-flow-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ["XBET_PROXY_FILE"] = os.path.join(os.environ["DATA_DIR"], "pas-de-fichier.env")      # jamais le vrai proxy de la machine
os.environ.pop("XBET_PROXY", None)

import requests  # noqa: E402

from kelly import app, calib, chat, combos as CB, config as C, fair, fmt, money, played, settle, store, telegram  # noqa: E402
from kelly.sources import xbet  # noqa: E402

DAY = "2030-01-01"
KO = f"{DAY}T19:00:00Z"


def prono(i, **kw):
    """Pronostic 💎 : moins de 3,5 buts à 1,55, match m<i>, id 1xBet 900+i."""
    c = {"id": f"m{i}", "sport_key": "soccer_epl", "home": f"H{i}", "away": f"A{i}", "commence": KO, "start": "19:00", "league": "L",
         "market": "totals", "name": "Under", "point": 3.5, "who": None, "prob": 0.72, "p0": 0.72, "min_odds": 1.46, "conf": 0.6,
         "n_min": 8, "score": 0.2 - i / 1000, "key": f"m{i}|totals||3.5|Under", "pick": fmt.label("totals", "Under", 3.5, None, "soccer"),
         "xodds": 1.55, "xev": 0.12, "xgid": 900 + i, "xref": [900 + i, 10, 3.5],
         "psh": {"under|3.5": [0.70, "pinnacle", 1]}}      # Pinnacle voit 70 % : sans avis du marché, l'ensemble (buts) ne validerait jamais ce pari
    return c | kw


def fake_px(drop=()):
    """Cotes 1xBet relues : moins de 3,5 buts à 1,55 partout, sauf les matchs de `drop` (pari retiré par 1xBet)."""
    return {f"m{i}": {"gid": 900 + i, "px": {} if f"m{i}" in drop else {("totals", "Under", 3.5): 1.55, ("totals", "Over", 3.5): 2.4},
                      "refs": {} if f"m{i}" in drop else {("totals", "Under", 3.5): [900 + i, 10, 3.5]}} for i in range(7)}


def fetcher(drop=(), calls=None):
    def fetch(events, deadline=None, wanted=None):
        if calls is not None:
            calls.append({"ids": [e["id"] for _, e in events], "deadline": deadline})
        return {k: v for k, v in fake_px(drop).items() if k in {e["id"] for _, e in events}}
    return fetch


class Fakes:
    """Envoi capturé (texte + arguments), code coupon simulé, vérification en direct simulée : aucun réseau."""

    def __init__(self, drop=(), got=True):
        self.sent, self.kw, self.coupons = [], [], []
        self.drop, self.got = drop, got

    def __enter__(self):
        from kelly import xprice
        self.old = (xbet.save_coupon, telegram.send, xprice.live_check)
        real_check = xprice.live_check
        xbet.save_coupon = lambda ls: (self.coupons.append([c["id"] for c in ls]), "K7Q2X")[1]
        telegram.send = lambda text, *a, **k: (self.sent.append(text), self.kw.append(k))
        xprice.live_check = lambda ids, now=None, fetch=None, fresh_min=None: real_check(
            ids, now, fetch=lambda ev, deadline=None, wanted=None, live=True, fresh_min=None:
            {k: v for k, v in fake_px(self.drop).items() if k in {e["id"] for _, e in ev}} if self.got else {})
        return self

    def __exit__(self, *a):
        from kelly import xprice
        xbet.save_coupon, telegram.send, xprice.live_check = self.old


def setup(pronos=None, stake=None, budget=None, n_legs=4):
    """today.json du jour, combiné du palier 5 du matin (m0..m3) dans fair_combos.json, journal des versions vide."""
    store.save(calib.FILE, {})
    calib._memo.clear()
    store.save(money.FILE, {"daily": budget, "cur": "FCFA"} if budget else {})
    pronos = pronos if pronos is not None else [prono(i) for i in range(7)]
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos})
    legs = [prono(i) for i in range(n_legs)]
    store.save(fair.COMBOS_FILE, [fair.combo_row(5, (legs, 1.55 ** n_legs, 0.72 ** n_legs), DAY, stake, "FCFA" if stake else None)])
    store.save(app.COMBO_UPDATES, {})
    return legs


def buttons(msg):
    return [b["callback_data"] for row in msg.markup["inline_keyboard"] for b in row if "callback_data" in b]


def at(h, m=0):
    return datetime(2030, 1, 1, h, m, tzinfo=timezone.utc)


def test_last_version_is_the_base():
    """C1 : après un premier « 🔄 COMBINÉ À JOUR », le contrôle « un match a commencé » et la reconstruction partent de la dernière version
    envoyée (jambe ajoutée comprise), pas de celle du matin ; le bouton « Vérifier » aussi."""
    early = f"{DAY}T14:00:00Z"                  # m4, le futur remplaçant, joue à 14:00 ; les autres à 19:00
    setup(pronos=[prono(i, commence=early) if i == 4 else prono(i) for i in range(7)])
    morning = {"m0", "m1", "m2", "m3"}
    with Fakes() as f:
        assert app.run_xbet_refresh(now=at(13), announce=False, fetch=fetcher(("m1",))) == 0
    assert len(f.sent) == 1 and "🔄 COMBINÉ À JOUR 13:00" in f.sent[0] and "❌ Retiré : H1 – A1" in f.sent[0], f.sent
    v1 = store.load(app.COMBO_UPDATES, {})["tiers"]["5"]
    ids1 = [l["id"] for l in v1["legs"]]
    added = (set(ids1) - morning).pop()
    assert added == "m4" and "m1" not in ids1 and len(ids1) == 4, ids1
    assert {"commence", "xodds", "xref", "pick", "home", "away", "prob", "min_odds", "key"} <= set(v1["legs"][0])
    # le match de la jambe ajoutée a commencé (14:00) : le combiné est en jeu, rien n'est renvoyé ni modifié (avant : repartait du matin)
    before = (store.load(app.COMBO_UPDATES, {}), store.load(fair.COMBOS_FILE, []))
    with Fakes() as f:
        app.run_xbet_refresh(now=at(14, 30), announce=False, fetch=fetcher(("m3",)))
        assert not f.sent, f.sent
        msg = app.refresh_combo("5", DAY, now=at(14, 30))
        assert "en jeu" in msg and "un match a commencé" in msg and not f.sent and not f.coupons, (msg, f.sent)
    assert (store.load(app.COMBO_UPDATES, {}), store.load(fair.COMBOS_FILE, [])) == before
    # bouton « Vérifier » sous le combiné renvoyé : même combiné (H1 ne revient pas), « ⏳ » d'abord (message éphémère)
    with Fakes() as f:
        msg = app.refresh_combo("5", DAY, now=at(13, 30))
    assert f.sent[0].startswith("⏳ Je vérifie ce combiné chez 1xBet") and f.kw[0].get("queue") is False, (f.sent, f.kw)
    assert "aucun pari à changer" in msg and "H1 – A1" not in msg and "H4 – A4" in msg, msg[:400]
    assert [l["id"] for l in store.load(app.COMBO_UPDATES, {})["tiers"]["5"]["legs"]] == ids1
    print("ok  C1 dernière version envoyée = base du contrôle, de la reconstruction et du bouton Vérifier")


def test_nothing_refreshed():
    """C2 : relevé qui n'a rien relu -> aucun combiné jugé ni renvoyé."""
    setup()
    orig = CB.patch_combo
    CB.patch_combo = lambda *a, **k: (_ for _ in ()).throw(AssertionError("patch_combo appelé sans rien relu"))
    try:
        with Fakes() as f:
            assert app._update_combos(DAY, store.load("today.json", {})["pronos"], at(13), set()) == 0
            assert app.run_xbet_refresh(now=at(13), announce=False, fetch=lambda ev, deadline=None, wanted=None: {}) == 0
        assert not f.sent and store.load(app.COMBO_UPDATES, {}) == {}
    finally:
        CB.patch_combo = orig
    print("ok  C2 rien relu : aucun combiné touché")


def test_tracking_follows_the_sent_version():
    """C3 : un combiné renvoyé à jour remplace le matin dans le suivi (jambes, clés, cote ; mise gardée) et garde ses boutons de mise ;
    un combiné déclaré injouable sort du règlement et de l'argent ; il revit si un remplaçant apparaît."""
    legs = setup(stake=500, budget=5000)
    with Fakes() as f:
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fetcher(("m1",)))
    row = fair.load_combos()[0]
    assert row["orig_keys"] == [c["key"] for c in legs] and "m1|totals||3.5|Under" not in row["keys"]
    assert row["updated"] == "13:00" and row["stake"] == 500 and row["cur"] == "FCFA" and [l["key"] for l in row["legs"]] == row["keys"]
    assert row["min_odds"] == round(1.55 ** 4, 2) and row["result"] is None
    msg = f.sent[0]
    assert "💰 Mise : <b>500 FCFA</b>" in msg and "Mise indicative" not in msg and "seulement si 1xBet paie" not in msg, msg
    assert "Cotes 1xBet de 13:00 : vérifie-les avant de valider." in msg and "ce matin" not in msg
    # plus de « ❌ Pas joué » : « ✅ Je l'ai joué » avec la mise conseillée, puis vérifier
    assert buttons(msg) == [f"joue|v5|{DAY}", f"xmaj|5|{DAY}"], buttons(msg)
    assert msg.markup["inline_keyboard"][0][0]["text"] == "✅ Je l'ai joué — 500 FCFA", msg.markup
    # sans budget : « ✅ Je l'ai joué » + vérifier, et la ligne « règle ton budget » (jamais une mise en unités)
    setup()
    with Fakes() as f:
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fetcher(("m1",)))
    assert buttons(f.sent[0]) == [f"joue|v5|{DAY}", f"xmaj|5|{DAY}"] and "Mise indicative" not in f.sent[0]
    assert "Mise : règle ton budget du jour avec « 💰 Mon budget »" in f.sent[0] and f.sent[0].markup["inline_keyboard"][0][0]["text"] == "✅ Je l'ai joué"
    assert "mise" not in fair.load_combos()[0] or fair.load_combos()[0].get("stake") is None
    # injouable : pas de remplaçant (seulement les 4 matchs du combiné), un pari retiré
    setup(pronos=[prono(i) for i in range(4)], stake=500, budget=5000)
    with Fakes() as f:
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fetcher(("m1",)))
    assert len(f.sent) == 1 and "ne le joue pas" in f.sent[0], f.sent
    row = fair.load_combos()[0]
    assert row["dead"] and "skipped" not in row and row["result"] == "void", "pas noté : rien à retirer du bilan"
    # noté joué (le ticket est placé) : JAMAIS déclaré injouable ni annulé ni sorti du bilan — Kelly prévient seulement (lot I : _watch_ticket)
    setup(pronos=[prono(i) for i in range(4)], stake=500, budget=5000)
    assert "✅ Noté" in played.command(["v5", "5,40"], DAY)
    with Fakes() as f:
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fetcher(("m1",)))
    row = fair.load_combos()[0]
    assert "dead" not in row and "skipped" not in row and "auto_skipped" not in row and row["result"] is None, row
    assert row["played"]["odds"] == 5.4 and len(played.items({"combos": []}, [], [row])) == 1
    assert len(f.sent) == 1 and "ton ticket" in f.sent[0] and "ne le joue pas" not in f.sent[0], f.sent
    setup(pronos=[prono(i) for i in range(4)], stake=500, budget=5000)
    with Fakes():
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fetcher(("m1",)))
    row = fair.load_combos()[0]
    far = datetime(2030, 1, 5, tzinfo=timezone.utc)
    rows, done = settle.settle_fair_combos(far, stats_for=lambda *a, **k: None)
    assert not done and rows[0]["result"] == "void", "un combiné mort n'est jamais réglé"
    assert app.fair_combos_report(far) == [] and settle.fair_combo_line(rows) == ""
    assert money.result({"combos": []}, [], rows)[0] == 0 and money.gain(rows[0]) is None
    assert "🚫 Aux cotes 1xBet" in played.todo(DAY) and ": plus jouable chez 1xBet" in played.todo(DAY), played.todo(DAY)
    # sans budget : drapeau « dead » seul, mêmes exclusions
    setup(pronos=[prono(i) for i in range(4)])
    with Fakes():
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fetcher(("m1",)))
    row = fair.load_combos()[0]
    assert row["dead"] and "skipped" not in row and row["result"] == "void"
    # ... et il revit si un remplaçant apparaît (bouton Vérifier)
    store.save("today.json", {"day": DAY, "picks": [], "pronos": [prono(i) for i in range(7)]})
    with Fakes():
        msg = app.refresh_combo("5", DAY, now=at(13, 30))
    row = fair.load_combos()[0]
    assert "dead" not in row and row["result"] is None and row["updated"] == "13:30" and "COMBINÉ À JOUR" in msg
    print("ok  C3 suivi = dernière version envoyée, boutons de mise, combiné mort exclu du règlement et du bilan")


def test_keep_pronos():
    """C4 : les paris montrés et les jambes des combinés du jour ne tombent jamais de today.json, même en queue de classement."""
    store.save(calib.FILE, {})
    calib._memo.clear()
    pronos = [prono(i, prob=0.59, xodds=None, xev=None, xgid=None, xref=None, score=1 - i / 1000, key=f"p{i}", id=f"p{i}") for i in range(60)]
    pronos[50].update(xodds=2.0, xev=0.18)                      # seul 💎 : le pari montré, loin dans le classement
    leg = pronos[55]
    combos_ = [(5, ([leg | {"odds": 1.6}], 1.6, 0.59)), (12, None)]
    old = pronos[:40] + [c for c in pronos[40:] if CB.fair_eligible(c) or c.get("xgid")][:300]
    assert pronos[50] not in old and leg not in old, "l'ancienne règle les perdait"
    keep = app.keep_pronos(pronos, combos_)
    assert pronos[50] in keep and leg in keep and len(keep) == 40, len(keep)          # les 2 obligatoires + les 38 meilleurs des autres
    assert keep == [c for c in pronos if c in keep], "ordre du classement conservé"
    assert pronos[37] in keep and pronos[38] not in keep and pronos[45] not in keep
    assert app.keep_pronos([], []) == []
    print("ok  C4 paris montrés et jambes des combinés toujours gardés")


def test_manual_refresh_order_and_deadline():
    """C5 : relevé manuel borné (C.XBET_MANUAL_SECONDS) ; jambes des combinés et paris 💎 montrés relus avant le reste."""
    assert C.XBET_MANUAL_SECONDS == 150
    pronos = [prono(i, xodds=None, xev=None, xref=None) if i < 2 else prono(i) for i in range(7)]       # m0, m1 : ni 💎 ni dans un combiné
    setup(pronos=pronos)
    store.save(fair.COMBOS_FILE, [fair.combo_row(5, ([prono(i) for i in (6, 5, 4, 3)], 5.7, 0.2), DAY)])
    calls = []
    with Fakes() as f:
        reply = app.manual_refresh(now=at(16), fetch=fetcher((), calls))
    assert f.sent[0].startswith("⏳") and "1 à 2 minutes" not in f.sent[0] and "3 minutes" in f.sent[0] and f.kw[0].get("queue") is False
    order = calls[0]["ids"]
    assert set(order[:5]) == {"m2", "m3", "m4", "m5", "m6"} and order[5:] == ["m0", "m1"], order
    assert 0 < calls[0]["deadline"] - time.monotonic() <= C.XBET_MANUAL_SECONDS, "relevé manuel borné à 150 s"
    assert reply.startswith("✅ Cotes 1xBet actualisées à 16:00"), reply
    calls.clear()
    with Fakes():
        app.run_xbet_refresh(now=at(15), announce=False, fetch=fetcher((), calls))                  # 15:00 : le délai par défaut du matin
    assert calls[0]["deadline"] - time.monotonic() > C.XBET_MANUAL_SECONDS * 2
    print("ok  C5 relevé manuel : combinés et 💎 d'abord, délai de 150 s")


def test_texts():
    """C6 : textes des combinés aux cotes 1xBet (heure des cotes, pas de « mise seulement si 1xBet paie la cote mini », en-tête, libellés)."""
    store.save(calib.FILE, {})
    calib._memo.clear()
    legs = [prono(i) for i in range(4)]
    real = fmt.fair_combo(5.77, legs, 0.27, 0.5, stake_txt="500 FCFA", when="15:00")
    assert "Cotes 1xBet de 15:00 : vérifie-les avant de valider." in real and "ce matin" not in real
    assert "💰 Mise : <b>500 FCFA</b>" in real and "seulement si 1xBet paie la cote mini" not in real, real
    assert "🕒 Cotes 1xBet : vérifie-les avant de valider." in fmt.fair_combo(5.77, legs, 0.27, 0.5)
    mixed = fmt.fair_combo(5.0, legs[:3] + [prono(4, xodds=None, xref=None)], 0.27, 0.5, stake_txt="500 FCFA")
    assert "(si 1xBet paie la cote mini)" in mixed and "À VÉRIFIER" in mixed
    # en-tête du matin
    money.set_daily(5000)
    store.save("bets.json", {"picks": [], "combos": []})
    picks = [{"key": f"k{i}", "id": f"e{i}", "odds": 1.9, "prob": 0.6, "edge": 0.08, "n_books": 14, "weight": 1.0, "score": 0.08,
              "market": "h2h", "name": f"H{i}", "why": "", "pm": None, "commence": f"{DAY}T{12 + i}:00:00Z", "home": f"H{i}",
              "away": f"A{i}", "event": f"H{i} - A{i}", "league": "L", "start": f"{12 + i}:00", "pick": f"H{i} gagne", "group": "soccer",
              "sport_key": "soccer_epl"} for i in range(8)]
    with Fakes():
        msgs, _, _ = app.build_day(picks, date(2030, 1, 1), fair_combos=[(5, (legs, 5.77, 0.27))], odds_time="04:12")
        head, combo = msgs[0], next(m for m in msgs if "<b>💎 COMBINÉ — cote 5,77</b>" in m)
        assert "💎 1 combiné aux cotes 1xBet" in head and "code coupon" in head and "cotes de Kelly, pas du bookmaker" not in head, head
        assert "Cotes 1xBet de 04:12" in combo and "ce matin" not in combo and f"xmaj|5|{DAY}" in buttons(combo)
        msgs, _, _ = app.build_day(picks, date(2030, 1, 1), fair_combos=[(5, (legs[:3] + [prono(4, xodds=None, xref=None)], 5.0, 0.27))])
        assert "🎲 1 combiné à vérifier chez 1xBet" in msgs[0] and "cotes de Kelly, pas du bookmaker" in msgs[0]
    store.save(money.FILE, {})
    # libellés de /joue
    setup()
    t = played.find("v5", DAY)
    assert "combiné aux cotes 1xBet (cote 5,77)" in t.label and "mini" not in t.label and t.ref_word == "cote 1xBet", t.label
    row = fair.load_combos()
    row[0]["legs"][0]["xodds"] = None
    store.save(fair.COMBOS_FILE, row)
    assert "combiné à vérifier (cote mini 5,77)" in played.find("v5", DAY).label
    # /combi : tous les matchs commencent dans quelques minutes -> pas « 1xBet ne répond pas »
    soon = [prono(i, commence="2030-01-01T13:05:00Z") for i in range(7)]
    setup(pronos=soon)
    with Fakes() as f:
        out = app._live_reply((soon[:4], 5.77, 0.27), 5.0, 6.25, now=at(13))
    assert "commencent dans moins de" in out and "ne répond pas" not in out, out
    with Fakes() as f:
        out = app._live_reply(([prono(i) for i in range(4)], 5.77, 0.27), 5.0, 6.25, now=at(13),
                              check=lambda ids, now, fresh_min=None: ([], set(), "13:00"))
    assert "1xBet ne répond pas" in out
    print("ok  C6/C11 textes des combinés aux cotes 1xBet")


def test_buttons_keep_verify_row():
    """C7 : « ✏️ Autre cote », « ↩️ Annuler » et les anciens « ❌ Pas joué » / « ↩️ Finalement joué » gardent la ligne « 🎟️ Vérifier ce combiné »
    d'un combiné réel."""
    setup(stake=500, budget=5000)
    edits = []
    old = telegram.edit_buttons
    telegram.edit_buttons = lambda mid, markup, chat_id=None: edits.append((mid, markup)) or True
    verify = f"xmaj|5|{DAY}"
    flat = lambda markup: [b["callback_data"] for row in markup["inline_keyboard"] for b in row if "callback_data" in b]       # noqa: E731
    try:
        assert "retiré de ton bilan" in chat.on_callback(f"skip|v5|{DAY}", 42)
        assert flat(edits[-1][1]) == [f"unskip|v5|{DAY}", verify]
        assert "compté comme joué" in chat.on_callback(f"unskip|v5|{DAY}", 42)
        assert flat(edits[-1][1]) == [f"joue|v5|{DAY}", verify] and edits[-1][1]["inline_keyboard"][0][0]["text"] == "✅ Je l'ai joué — 500 FCFA"
        # cote 1xBet connue (combiné aux cotes 1xBet) + mise conseillée : un appui note tout de suite à la cote du combiné, sans rien demander
        assert chat.on_callback(f"joue|v5|{DAY}", 42).startswith("✅ Noté") and chat.get_pending() is None
        assert flat(edits[-1][1]) == ["noop", f"joue|v5|{DAY}", f"annule|v5|{DAY}", verify]
        assert edits[-1][1]["inline_keyboard"][0][0]["text"].startswith("✅ Joué à 5,77")
        # « ✏️ Autre cote » demande la cote (c'est son rôle) ; l'utilisateur la touche après avoir lu le message (le même appui 2 fois de suite = double appui)
        chat.forget_taps()
        assert "À quelle cote as-tu finalement joué" in chat.on_callback(f"joue|v5|{DAY}", 42)
        assert "Noté" in chat.route("5,40")[1]
        assert flat(edits[-1][1]) == ["noop", f"joue|v5|{DAY}", f"annule|v5|{DAY}", verify]
        assert edits[-1][1]["inline_keyboard"][0][0]["text"].startswith("✅ Joué à 5,40")
        # « ↩️ Annuler » retire la note et remet « ✅ Je l'ai joué », la ligne « Vérifier » reste
        assert "Note annulée" in chat.on_callback(f"annule|v5|{DAY}", 42)
        assert flat(edits[-1][1]) == [f"joue|v5|{DAY}", verify] and "played" not in fair.load_combos()[0]
        assert chat.on_callback(f"annule|v5|{DAY}", 42).endswith("n'était pas noté.")
        # combiné pas réel (cote mini) : pas de ligne « Vérifier »
        rows = fair.load_combos()
        rows[0]["legs"][0]["xodds"] = None
        store.save(fair.COMBOS_FILE, rows)
        chat.on_callback(f"skip|v5|{DAY}", 43)
        assert flat(edits[-1][1]) == [f"unskip|v5|{DAY}"]
        # un combiné du matin classique (« sur ») n'en a jamais
        assert chat.refresh_row("sur", DAY) == [] and chat.keep_refresh(None, "pari", DAY) is None
    finally:
        telegram.edit_buttons = old
    print("ok  C7 la ligne « Vérifier ce combiné » survit aux changements de boutons")


def test_fair_record_flags_and_lines():
    """C9 : un pronostic suivi en silence puis montré sort du silence ; une ligne qui change met à jour le suivi existant."""
    store.save(fair.FILE, [])
    a = prono(1, key="m1|totals||2.5|Under", point=2.5)
    quiet = prono(2)
    assert fair.record([quiet, a], DAY, shown=[quiet]) == 2
    rows = {r["key"]: r for r in fair.load()}
    assert rows[a["key"]].get("x") == 1 and "x" not in rows[quiet["key"]]
    assert fair.record([a], DAY, shown=[a]) == 0
    assert "x" not in {r["key"]: r for r in fair.load()}[a["key"]], "annoncé depuis : plus en silence"
    # même match, même marché, autre ligne : une seule ligne de suivi, mise à jour
    moved = prono(1, key="m1|totals||3.5|Under", point=3.5, prob=0.7, xodds=1.7)
    assert fair.record([moved], DAY, shown=[moved]) == 0
    rows = [r for r in fair.load() if r["id"] == "m1"]
    assert len(rows) == 1 and rows[0]["key"] == moved["key"] and rows[0]["point"] == 3.5 and rows[0]["xodds"] == 1.7 and rows[0]["prob"] == 0.7
    # déjà réglé : intact
    fr = fair.load()
    for r in fr:
        if r["id"] == "m1":
            r["result"] = "win"
    store.save(fair.FILE, fr)
    again = prono(1, key="m1|totals||4.5|Under", point=4.5)
    assert fair.record([again], DAY, shown=[again]) == 0
    assert [r["point"] for r in fair.load() if r["id"] == "m1"] == [3.5]
    # annonce du relevé de l'après-midi : par match et marché, pas par clé
    ann = prono(0, key="m0|totals||2.5|Under", point=2.5, prob=0.72)
    store.save(calib.FILE, {})
    calib._memo.clear()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": [ann], "announced": ["m0|totals||3.5|Under"]})    # ancien format : clé
    store.save(fair.COMBOS_FILE, [])                # aucun combiné du matin (un combiné « à vérifier » resté d'un test passerait aux cotes 1xBet)
    store.save(app.COMBO_UPDATES, {})
    sent = []
    old = telegram.send
    telegram.send = lambda text, *a, **k: sent.append(text)
    px = {"m0": {"gid": 900, "px": {("totals", "Under", 2.5): 1.8}, "refs": {}}}
    try:
        n = app.run_xbet_refresh(now=at(15), announce=True, fetch=lambda ev, deadline=None, wanted=None: px)
    finally:
        telegram.send = old
    assert store.load("today.json", {})["pronos"][0]["xodds"] == 1.8, "la ligne a bien été relue à 1xBet"
    assert n == 0 and not sent, "ligne changée : pas de seconde annonce"
    print("ok  C9 sortie du silence, une ligne de suivi par match et marché, annonce par match et marché")


def test_catch_up_day():
    """C8 : le rattrapage d'avant 04:00 écrit la date UTC du jour ; le relevé et le relevé manuel l'acceptent (et celui de la veille)."""
    for day, hour, ok in (("2030-01-01", 2, True), ("2029-12-31", 2, True), ("2029-12-30", 2, False), ("2030-01-01", 15, True),
                          ("2029-12-31", 15, False)):
        setup()
        t = store.load("today.json", {})
        t["day"] = day
        store.save("today.json", t)
        calls = []
        with Fakes():
            app.run_xbet_refresh(now=at(hour), announce=False, fetch=fetcher((), calls))
        assert bool(calls) is ok, (day, hour, calls)
    setup()
    calls = []
    with Fakes():
        reply = app.manual_refresh(now=at(2), fetch=fetcher((), calls))
    assert calls and reply.startswith("✅ Cotes 1xBet actualisées à 02:00"), reply
    print("ok  C8 relevé et relevé manuel acceptent le jour écrit par le rattrapage d'avant 04:00")


def test_telegram_retry():
    """C10 : un envoi n'est rejoué que si la requête n'est certainement pas partie ; lecture / édition : nouvel essai sans risque."""
    os.environ["TELEGRAM_TOKEN"] = "123:abc"
    old = telegram._session
    calls = []

    class Sess:
        def __init__(self, exc, then=None):
            self.exc, self.then = exc, then

        def post(self, url, **kw):
            calls.append(url.rsplit("/", 1)[-1])
            if len(calls) == 1 or self.then is None:
                raise self.exc
            return self.then
    ok = type("R", (), {"ok": True, "status_code": 200, "json": lambda self: {"ok": True, "result": {"message_id": 7}}})()
    try:
        # connexion gardée fermée par Telegram AVANT de lire la requête (RemoteDisconnected) : rejouée tout de suite ;
        # coupure au milieu de la réponse : jamais rejouée (Telegram a pu recevoir le message)
        for exc, method, n in ((requests.ConnectionError("Connection aborted, RemoteDisconnected"), "sendMessage", 2),
                               (requests.ConnectionError("Connection broken: IncompleteRead(0 bytes read)"), "sendMessage", 1),
                               (requests.ReadTimeout("lent"), "sendMessage", 1),
                               (requests.ConnectTimeout("injoignable"), "sendMessage", 2),
                               (requests.exceptions.ProxyError("502"), "sendMessage", 2),
                               (requests.ConnectionError("Max retries (Caused by NewConnectionError('x'))"), "sendMessage", 2),
                               (requests.ConnectionError("Connection aborted"), "getUpdates", 2)):
            calls.clear()
            telegram._session = Sess(exc, ok)
            try:
                res = telegram.api(method, chat_id=1, text="x")
            except telegram.TelegramError:
                res = None
            assert len(calls) == n, (type(exc).__name__, str(exc), method, calls)
            assert (res is not None) == (n == 2), (exc, res)
    finally:
        telegram._session = old
        os.environ.pop("TELEGRAM_TOKEN", None)
    print("ok  C10 un message n'est jamais renvoyé s'il a pu arriver")


def main():
    test_last_version_is_the_base()
    test_nothing_refreshed()
    test_tracking_follows_the_sent_version()
    test_keep_pronos()
    test_manual_refresh_order_and_deadline()
    test_texts()
    test_buttons_keep_verify_row()
    test_fair_record_flags_and_lines()
    test_catch_up_day()
    test_telegram_retry()
    print("tests flux Telegram : OK")


if __name__ == "__main__":
    main()
