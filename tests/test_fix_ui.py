"""Non-régression de l'interface budget / texte libre : montants avec séparateurs de milliers, budget absurde, FCFA qui fuient dans
la bankroll en unités, mises à 0, anciens « Pas joué » sur un pari réglé, routage du texte libre, réponse « Autre cote », affichages.
Hors ligne. Lancer : python -m tests.test_fix_ui"""
import json
import math
import os
import tempfile
from datetime import date

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-fixui-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-fixui-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-fixui-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import app, bankroll, chat, config as C, fmt, money, played, settle, store, telegram, track  # noqa: E402

DAY = "2026-10-09"
SUR, COSTAUD = C.COMBOS[0][0], C.COMBOS[1][0]


def wipe():
    for f in os.listdir(store.DATA_DIR):
        os.remove(os.path.join(store.DATA_DIR, f))
    bankroll.save({"bankroll": 100.0, "initial": 100.0})


def combo(tier, odds_=5.0, stake=1000, cur="FCFA", result=None, **kw):
    c = {"tier": tier, "keys": [f"k{tier[:2]}"], "odds": odds_, "prob": 0.2, "stake": stake, "date": DAY, "result": result}
    return c | ({"cur": cur} if cur else {}) | kw


def leg(i, odds_, prob, edge):
    return {"key": f"k{i}", "id": f"e{i}", "odds": odds_, "prob": prob, "edge": edge, "n_books": 14, "weight": 1.0, "score": edge,
            "market": "h2h", "name": f"H{i}", "why": "", "pm": None, "commence": f"2026-10-09T{12 + i % 10:02d}:00:00Z",
            "home": f"H{i}", "away": f"A{i}", "event": f"H{i} - A{i}", "league": "L", "start": f"{12 + i % 10}:00",
            "pick": f"H{i} gagne", "group": "soccer", "sport_key": "soccer_epl"}


def test_amount_parsing():
    wipe()
    ok = {"5000": 5000, "5 000": 5000, "5 000": 5000, "5 000": 5000, "5.000": 5000, "5,000": 5000, "5,5": 5.5, "5.5": 5.5,
          "1.234.567": 1234567, "1 234 567": 1234567, "1.234,56": 1234.56, "1,234.56": 1234.56, "2 000,50": 2000.5, "0,5": 0.5,
          "5,40": 5.4, "12": 12, "1000": 1000}
    for txt, want in ok.items():
        v, c = money.parse_amount([txt])
        assert v == want and c is None, (txt, v, c)
        v, c = money.parse_amount(txt.split())            # « 5 » « 000 » : mots séparés par Telegram
        assert v == want and c is None, (txt, v, c)
    assert money.parse_amount(["50", "€"]) == (50, "€") and money.parse_amount(["5", "000", "fcfa"]) == (5000, "FCFA")
    assert money.parse_amount(["5000FCFA"]) == (5000, "FCFA")
    for bad in ("inf", "nan", "-5", "1e9", "abc", "1.2.3", "5,40,5", "99999999999", "5 0000", "5 000 abc def"):
        assert money.parse_amount(bad.split()) == (None, None), bad
    assert money.parse_amount(["5000", "<b>"]) == (None, None)
    # commande budget : le montant entier, la devise seulement si un mot suit
    assert "5 000 FCFA" in app.handle_command("budget 5 000") and money.daily() == 5000 and money.cur() == "FCFA"
    assert "5 000 FCFA" in app.handle_command("budget 5.000") and money.daily() == 5000
    assert "5 000 FCFA" in app.handle_command("budget 5,000") and money.daily() == 5000
    assert "5,5" in app.handle_command("/budget 5,5") and money.daily() == 5.5
    # tout est en FCFA (lot I) : une autre devise est refusée (le budget ne change pas), « 5k » = 5 000 FCFA
    assert "Kelly compte tout en FCFA" in app.handle_command("budget 50 €") and money.daily() == 5.5 and money.cur() == "FCFA"        # budget inchangé
    app.handle_command("budget 5 000 fcfa")
    assert money.daily() == 5000 and money.cur() == "FCFA"
    assert "5 000 FCFA" in app.handle_command("budget 5k") and money.daily() == 5000 and money.cur() == "FCFA"
    for bad in ("budget inf", "budget nan", "budget -5", "budget 1e300", "budget 99999999999", "budget abc"):
        assert "invalide" in app.handle_command(bad), bad
        assert money.daily() == 5000, bad                       # un montant refusé ne touche pas au budget
    assert "supprimé" in app.handle_command("budget 0") and not money.active()
    # /joue : mise avec milliers, cote avec virgule ou point
    wipe()
    store.save("bets.json", {"picks": [], "combos": [combo(SUR, cur=None, stake=10)]})
    for txt, stake in (("5,40 2 000", 2000), ("5.40 2.000", 2000), ("5,40 2,5", 2.5), ("5,40 1.234,5", 1234.5), ("5,40", 1234.5)):   # cote seule : même mise
        assert "Noté" in played.command(["sur"] + txt.split(), DAY), txt
        p = store.load("bets.json", {})["combos"][0]["played"]
        assert p["odds"] == 5.4 and p["stake"] == stake, (txt, p)
    for bad in ("5,40 2 00", "5,40 -3", "5,40 inf", "inf", "nan", "-5", "5,40 0", "1e3"):
        r = played.command(["sur"] + bad.split(), DAY)
        assert "Noté" not in r, (bad, r)
    # sans mise conseillée (pas de budget du jour) ni mise donnée : Kelly demande la mise, jamais une mise en unités inventée
    store.save("bets.json", {"picks": [], "combos": [combo(SUR, cur=None, stake=10)]})
    r = played.command(["sur", "5,40"], DAY)
    assert r == "Avec quelle mise en FCFA ? Réponds ta mise, par exemple 1 000 (cote 5,40 retenue)." and "played" not in store.load("bets.json", {})["combos"][0], r
    assert "Noté" in played.command(["sur", "5,40", "2000"], DAY)
    # dans /joue la place est fixée (cote puis mise) : « 2 000 » = cote 2, mise 0 (refusée) ; « 2 500 » = cote 2, mise 500
    assert "supérieure à 0" in played.command(["sur", "2", "000"], DAY) and store.load("bets.json", {})["combos"][0]["played"]["odds"] == 5.4
    assert "Noté" in played.command(["sur", "2", "500"], DAY) and store.load("bets.json", {})["combos"][0]["played"]["stake"] == 500
    assert played.parse_odds_stake(["2", "000"], strict=True)[2] == "ambigu" and played.parse_odds_stake(["2", "000"])[2] is None
    print("ok  montants avec séparateurs de milliers")


def test_allocate_robust():
    wipe()
    for weird in ("inf", float("inf"), float("nan"), "abc", -5, None, [1], True, 1e300):
        store.save(money.FILE, {"daily": weird, "cur": "FCFA"})
        assert not money.active() and money.daily() == 0, weird
        assert money.allocate([{"kind": "pari", "tier": "pari", "prob": 0.5, "odds": 2.0}])[0] == [0]
        money.round_step(weird if isinstance(weird, (int, float)) else 0)
    store.save(money.FILE, {"daily": "5000", "cur": "<b>x</b>"})
    assert money.daily() == 5000 and money.cur() == "FCFA" and money.money(10, "<i>") == "10 FCFA"
    store.save(money.FILE, {"daily": 5000})
    bad = [{"kind": "pari", "tier": "pari", "prob": float("nan"), "odds": 2.0}, {"kind": "fair", "tier": float("nan"), "prob": 0.3, "odds": 5},
           {"kind": "fair", "tier": "x", "prob": 0.3, "odds": 5}, {"kind": "combo"}, {"kind": "combo", "tier": SUR, "prob": None, "odds": 3},
           {"kind": "combo", "tier": SUR, "prob": 0.3, "odds": float("inf")}, {"kind": "combo", "tier": SUR, "prob": 0.3, "odds": 5.0}]
    stakes, spent, kept = money.allocate(bad)
    assert len(stakes) == len(bad) and all(math.isfinite(s) for s in stakes) and stakes[3] == 0 and stakes[-1] > 0
    assert money.allocate(bad, float("inf"))[0] == [0] * len(bad) and money.allocate(bad, 1e300)[0] == [0] * len(bad)
    # build_day ne plante pas avec un budget absurde stocké à la main : il retombe en mode « sans budget » (aucune mise conseillée)
    store.save(money.FILE, {"daily": float("inf"), "cur": "FCFA"})
    store.save("bets.json", {"picks": [], "combos": []})
    msgs, _, combos_ = app.build_day([leg(i, 1.9, 0.6, 0.08) for i in range(8)], date(2026, 10, 9))
    assert msgs and all("cur" not in c for c in combos_)
    print("ok  budget absurde et répartition robuste")


def test_no_fcfa_in_bankroll():
    wipe()
    money.set_daily(5000)
    bets = {"picks": [], "combos": [combo(SUR, 5.0, 1000, result="win", odds_final=5.0), combo(COSTAUD, 13.0, 500, result="loss")]}
    # budget actif : les combinés ne sont pas traités (morning_report) ; « budget 0 » puis le matin suivant : ils ne touchent pas la bankroll
    money.command(["0"])
    b = bankroll.apply_results(bets["combos"])
    assert b["bankroll"] == 100.0 and all(c["counted"] for c in bets["combos"]), (b, bets)
    # mode réel : un combiné à vérifier noté, misé sur le budget, n'entre pas non plus dans la bankroll
    bankroll.save({"bankroll": 100.0, "initial": 100.0, "real": True})
    fair_c = {"date": DAY, "tier": 5, "min_odds": 5.5, "stake": 400, "cur": "FCFA", "result": "win", "played": {"odds": 6.0, "stake": 400},
              "legs": [{"key": "f1", "min_odds": 2.2, "result": "win"}, {"key": "f2", "min_odds": 2.5, "result": "win"}]}
    row = {"key": "s", "date": DAY, "odds": 1.9, "stake": 800, "cur": "FCFA", "result": "loss", "played": {"odds": 1.9, "stake": 800}}
    assert bankroll.apply_results([fair_c], played_only=True)["bankroll"] == 100.0 and fair_c["counted"]
    assert bankroll.apply_results([row], played_only=True)["bankroll"] == 100.0 and row["counted"]
    # un pari en unités (sans devise) compte toujours
    bankroll.save({"bankroll": 100.0, "initial": 100.0})
    u = combo(SUR, 5.0, 10, cur=None, result="win", odds_final=5.0)
    assert bankroll.apply_results([u])["bankroll"] == 140.0 and u["counted"]
    # le circuit du matin : budget actif puis supprimé
    bankroll.save({"bankroll": 100.0, "initial": 100.0})
    money.set_daily(5000)
    bets = {"picks": [], "combos": [combo(SUR, 5.0, 1000, result="win", odds_final=5.0, played={"odds": 5.0, "stake": 1000})]}
    store.save("bets.json", bets)
    money.command(["0"])
    assert bankroll.apply_results(store.load("bets.json", {})["combos"])["bankroll"] == 100.0
    print("ok  le budget en FCFA ne touche jamais la bankroll en unités")


def test_played_never_switches_real_mode_for_budget_or_zero():
    wipe()
    money.set_daily(5000)
    store.save("bets.json", {"picks": [], "combos": [combo(SUR, 5.0, 1000), combo(COSTAUD, 13.0, 0, cur=None)]})
    assert "Noté" in played.command(["sur", "5,40"], DAY) and not bankroll.load().get("real")
    # mise de 0 (trop petite pour le budget) : pas de mise conseillée, Kelly demande la mise (jamais une mise en unités inventée)
    r = played.command(["costaud", "13"], DAY)
    assert r.startswith("Avec quelle mise en FCFA ?") and "Noté" not in r and not bankroll.load().get("real"), r
    assert "played" not in store.load("bets.json", {})["combos"][1]
    assert "supérieure à 0" in played.command(["sur", "5,40", "0"], DAY)
    # le bouton « ✅ Je l'ai joué » d'un pari sans mise conseillée pose la question cote + mise, et la garde ouverte tant que la mise manque
    chat.clear_pending()
    ask = chat.on_callback(f"joue|costaud|{DAY}")
    assert ask.startswith("À quelle cote et avec quelle mise en FCFA ?") and chat.get_pending()["what"] == "costaud", ask
    assert chat.route("13")[1].startswith("Avec quelle mise en FCFA ?") and chat.get_pending()["what"] == "costaud"
    _, reply = chat.route("13 1 000")
    assert reply.startswith("✅ Noté") and "💰 1 000 FCFA à" in reply and chat.get_pending() is None, reply
    c1 = store.load("bets.json", {})["combos"][1]
    assert c1["played"]["stake"] == 1000 and c1["cur"] == "FCFA" and not bankroll.load().get("real"), c1
    assert chat.on_callback(f"joue|sur|{DAY}").startswith("À quelle cote as-tu finalement joué")        # déjà noté : on corrige la cote
    # pari du jour sans mise : stake 0 enregistré, jamais de mise en unités inventée
    chat.clear_pending()
    track.save([{"key": "s", "id": "s", "date": DAY, "sent": True, "single": True, "odds": 1.9, "prob": 0.6, "stake": 0, "cur": None,
                 "result": None, "pick": "X gagne", "market": "h2h"}])
    assert played.command(["pari", "1,9"], DAY).startswith("Avec quelle mise en FCFA ?") and not bankroll.load().get("real")
    # /joue tapé sans mise ouvre l'attente : la réponse « cote mise » suivante le note
    assert app.handle_command("/joue pari 1,9").startswith("Avec quelle mise en FCFA ?") and chat.get_pending()["what"] == "pari"
    assert chat.route("1,9 500")[1].startswith("✅ Noté") and track.load()[0]["played"]["stake"] == 500
    print("ok  pas de mode réel pour un pari du budget ou à mise 0 ; la mise manquante est demandée")


def test_zero_stake_has_no_buttons():
    wipe()
    money.set_daily(5000)
    store.save("bets.json", {"picks": [], "combos": []})
    legs = [leg(i, 1.9, 0.6, 0.08) for i in range(8)]
    orig = money.allocate

    def zeroing(items, budget=None, detail=False):                 # le Costaud et le pari du jour : plus petits que l'arrondi
        r = orig(items, budget, detail)
        return ([0 if it["tier"] in (COSTAUD, "pari") else s for it, s in zip(items, r[0])],) + tuple(r[1:])
    money.allocate = zeroing
    try:
        msgs, picks, combos_ = app.build_day(legs, date(2026, 10, 9))
    finally:
        money.allocate = orig
    zero = [c for c in combos_ if not c["stake"]]
    assert [c["tier"] for c in zero] == [COSTAUD] and "cur" not in zero[0], combos_
    assert picks[0]["stake"] == 0 and "cur" not in picks[0]                     # le pari du jour à 0 : noté 0, sans devise
    cost = next(m for m in msgs if "COSTAUD" in m)
    assert "pas de mise" in cost and [b["text"] for b in cost.markup["inline_keyboard"][0]] == ["✅ Je l'ai joué"], cost    # sans mise dans le bouton
    single = next(m for m in msgs if "PARI DU JOUR" in m)
    assert "pas de mise" in single and [b["text"] for b in single.markup["inline_keyboard"][0]] == ["✅ Je l'ai joué"], single
    funded = [c for c in combos_ if c["stake"]]
    assert funded and all(c["cur"] == "FCFA" for c in funded)
    with_buttons = [m for m in msgs if getattr(m, "markup", None) and "inline_keyboard" in m.markup]
    assert len(with_buttons) == len(combos_) + 1, (len(with_buttons), len(combos_))         # chaque pari a son « ✅ Je l'ai joué » (on peut jouer sa propre mise)
    sur_btn = next(m for m in with_buttons if "joue|sur|" in json.dumps(m.markup)).markup["inline_keyboard"][0][0]["text"]
    assert sur_btn.startswith("✅ Je l'ai joué — ") and sur_btn.endswith(" FCFA") and "skip|" not in json.dumps(with_buttons[0].markup)
    # le pari du jour et le Costaud enregistrés avec une mise de 0 : « ✅ Je l'ai joué » demande la cote ET la mise, bankroll intacte
    track.save([])
    track.record(picks, {c["key"] for c in picks}, DAY, single_key=picks[0]["key"])
    store.save("bets.json", {"picks": [], "combos": [c | {"date": DAY, "result": None} for c in combos_]})
    store.save("today.json", {"day": DAY, "picks": [], "pronos": []})            # « Mes paris » liste le jour Kelly en cours
    assert chat.on_callback(f"joue|pari|{DAY}").startswith("À quelle cote et avec quelle mise en FCFA ?")
    assert chat.on_callback(f"joue|costaud|{DAY}").startswith("À quelle cote et avec quelle mise en FCFA ?")
    assert not bankroll.load().get("real") and chat.get_pending()["what"] == "costaud"
    todo = app.handle_command("🧾 Mes paris")                                    # la liste offre le bouton du Costaud, sans mise dans le libellé
    cost_btn = next(b["text"] for row in todo.markup["inline_keyboard"] for b in row if b["callback_data"] == f"joue|costaud|{DAY}|l0")
    assert cost_btn.startswith("✅ Costaud · Je l'ai joué") and "FCFA" not in cost_btn, cost_btn
    # sans budget : les boutons ✅ restent
    store.save(money.FILE, {})
    msgs, _, _ = app.build_day(legs, date(2026, 10, 9))
    assert any(getattr(m, "markup", None) and "joue|sur|" in json.dumps(m.markup) for m in msgs)
    # chat.played_button directement
    money.set_daily(5000)
    assert chat.played_button("sur", DAY, stake=0)["inline_keyboard"][0][0]["text"] == "✅ Je l'ai joué"
    assert chat.played_button("sur", DAY, stake=2500)["inline_keyboard"][0][0]["text"] == "✅ Je l'ai joué — 2 500 FCFA"
    assert isinstance(chat.with_played_button(fmt.Html("x"), "sur", DAY, stake=0), telegram.Html)
    print("ok  pas de mise conseillée : « ✅ Je l'ai joué » sans montant, la mise est demandée")


def test_pas_joue_refused_once_settled():
    wipe()
    money.set_daily(5000)
    store.save("bets.json", {"picks": [], "combos": [combo(SUR, 5.0, 1000, result="loss", played={"odds": 5.0, "stake": 1000}),
                                                     combo(COSTAUD, 13.0, 500)]})
    r = chat.on_callback(f"skip|sur|{DAY}", 5)
    assert "déjà réglé" in r and "skipped" not in store.load("bets.json", {})["combos"][0], r
    assert "déjà réglé" in money.skip("sur", DAY) and "déjà réglé" in money.skip("sur", DAY, undo=True)
    assert money.result(store.load("bets.json", {}), [], [])[1] == -1000        # la perte du pari noté reste au bilan
    assert "retiré" in money.skip("costaud", DAY) and store.load("bets.json", {})["combos"][1]["skipped"]
    assert "compté comme joué" in money.skip("costaud", DAY, undo=True)
    print("ok  ancien « Pas joué » refusé sur un pari réglé")


def test_free_text_routing():
    wipe()
    store.save("today.json", {"day": DAY, "picks": [leg(1, 1.9, 0.6, 0.08) | {"home": "Paris FC", "away": "Rennes", "event": "Paris FC - Rennes"},
                                                    leg(2, 1.9, 0.6, 0.08) | {"home": "Atlético Madrid", "away": "Getafe",
                                                                              "event": "Atlético Madrid - Getafe"}], "pronos": []})
    for txt in ("Paris", "paris", "Paris du jour", "paris du jour", "paris:"):
        assert chat.route(txt) == ("/top", None), txt
    for txt in ("mes paris", "Mes paris"):                  # lot I : « mes paris » est la liste (bouton « 🧾 Mes paris »), pas /top
        assert chat.route(txt) == ("/joue", None), txt
    assert chat.route("Paris FC") == ("/analyse Paris FC", None)
    cmd, reply = chat.route("Paris Saint-Germain")
    assert cmd != "/top Saint-Germain" and not (cmd or "").startswith("/top"), (cmd, reply)
    assert chat.route("top 5")[0] == "/top 5"
    # /analyse et /match : sans accents, comme la détection d'équipe du texte libre
    cmd, _ = chat.route("Atletico")
    assert cmd == "/analyse Atletico", cmd
    today = store.load("today.json", {})
    out = app.analyse(today["picks"], "Atletico", [])
    assert "Aucun match du jour" not in out and "Atlético Madrid" in out, out
    assert "Atlético Madrid" in app.handle_command("/match atletico") and "Atlético Madrid" in app.handle_command("/match ATLÉTICO")
    assert "Atlético Madrid" in app.handle_command("/analyse Atletico Madrid")
    assert "Aucun match du jour" in app.handle_command("/match Zzzzz")
    assert "Paris FC" in app.handle_command("/match paris fc")
    print("ok  routage du texte libre et recherche sans accents")


def test_autre_cote_answer():
    wipe()
    money.set_daily(5000)
    edits = []
    old_edit = telegram.edit_buttons
    telegram.edit_buttons = lambda mid, markup, chat_id=None: edits.append((mid, markup)) or True
    try:
        store.save("bets.json", {"picks": [], "combos": [combo(SUR, 5.0, 1000)]})
        # sans attente, en mode budget : le bouton cité existe
        hint = chat.route("5,40")[1]
        assert "✅ Je l'ai joué" in hint and "✏️ Autre cote" in hint, hint
        assert chat.route("2 000")[1] == hint
        money.command(["0"])
        assert "Je l'ai joué" in chat.route("5,40")[1]                    # sans budget : le même bouton
        money.set_daily(5000)
        # question puis réponse « 5,40 2 000 »
        assert chat.on_callback(f"joue|sur|{DAY}", 11).startswith("À quelle cote")
        _, reply = chat.route("5,40 2 000")
        assert reply.startswith("✅ Noté") and "2 000 FCFA" in reply, reply
        p = store.load("bets.json", {})["combos"][0]["played"]
        assert p["odds"] == 5.4 and p["stake"] == 2000 and chat.get_pending() is None
        # le bouton devient « ✅ Joué à 5,40 · 2 000 FCFA » inerte + « ✏️ Autre cote » et « ↩️ Annuler » utiles
        rows = edits[-1][1]["inline_keyboard"]
        assert edits[-1][0] == 11 and rows[0][0]["text"] == "✅ Joué à 5,40 · 2 000 FCFA" and rows[0][0]["callback_data"] == "noop", rows
        assert [b["text"] for b in rows[1]] == ["✏️ Autre cote", "↩️ Annuler"], rows
        assert [b["callback_data"] for b in rows[1]] == [f"joue|sur|{DAY}", f"annule|sur|{DAY}"]
        assert not any("skip" in b["callback_data"] for row in rows for b in row)
        # « 5,40 2.000 », « 5,40 2000 FCFA »
        for txt in ("5,40 2.000", "5.40 2 000 FCFA", "5,40 2000"):
            chat.on_callback(f"joue|sur|{DAY}", 12)
            assert chat.route(txt)[1].startswith("✅ Noté"), txt
            assert store.load("bets.json", {})["combos"][0]["played"]["stake"] == 2000, txt
        # « 2 000 » seul, « 2 500 », absurde : on redemande, l'attente reste
        for txt in ("2 000", "2 500", "2000", "0,5", "1", "5,40 2 00", "1 2 3"):
            chat.on_callback(f"joue|sur|{DAY}", 13)
            before = store.load("bets.json", {})["combos"][0]["played"]
            _, reply = chat.route(txt)
            assert "pas compris la cote" in reply, (txt, reply)
            assert chat.get_pending() and chat.get_pending()["msg"] == 13, txt
            assert store.load("bets.json", {})["combos"][0]["played"] == before, txt
        assert chat.route("2,00 500")[1].startswith("✅ Noté") and chat.get_pending() is None
        # hors budget : la mise est celle donnée (FCFA), les mêmes boutons
        money.command(["0"])
        store.save("bets.json", {"picks": [], "combos": [combo(SUR, 5.0, 10, cur=None)]})
        chat.on_callback(f"joue|sur|{DAY}", 14)
        chat.route("5,40 2")
        assert [b["text"] for b in edits[-1][1]["inline_keyboard"][0]] == ["✅ Joué à 5,40 · 2 FCFA"], edits[-1]
        c0 = store.load("bets.json", {})["combos"][0]
        assert c0["played"]["stake"] == 2 and c0["cur"] == "FCFA" and c0["stake"] is None, c0           # l'ancienne mise en unités n'est plus une mise conseillée
    finally:
        telegram.edit_buttons = old_edit
    print("ok  réponse « Autre cote » : milliers, ambiguïté, boutons « Joué à… » / « Autre cote » / « Annuler »")


def test_display():
    wipe()
    money.set_daily(5000)
    # budget_line : jamais « journée pauvre » sur une journée riche (le reste n'est qu'un arrondi)
    rich = [{"kind": "pari", "tier": "pari", "prob": 0.6, "odds": 1.9}] + \
           [{"kind": "combo", "tier": t, "prob": 0.3, "odds": 4.0} for t, _, _ in C.COMBOS] + \
           [{"kind": "fair", "tier": 5.5, "prob": 0.25, "odds": 5.5}, {"kind": "fair", "tier": 13, "prob": 0.1, "odds": 13}]
    stakes, spent, kept, poor = money.allocate(rich, detail=True)
    assert not poor and kept > 0
    assert "journée pauvre" not in money.budget_line(spent, kept, poor) and "tout est réparti" in money.budget_line(spent, kept, poor)
    stakes, spent, kept, poor = money.allocate([{"kind": "combo", "tier": SUR, "prob": 0.2, "odds": 5.2}], detail=True)
    assert poor and "journée pauvre" in money.budget_line(spent, kept, poor)
    assert len(money.allocate(rich)) == 3
    store.save("bets.json", {"picks": [], "combos": []})
    msgs, _, _ = app.build_day([leg(i, 1.9, 0.6, 0.08) for i in range(8)], date(2026, 10, 9))
    assert "journée pauvre" in msgs[0], msgs[0]                           # 2 combinés seulement : les parts ne remplissent pas le budget
    # devise échappée dans le HTML
    store.save(money.FILE, {"daily": 5000, "cur": "<i>x</i>"})                # fichier modifié à la main : devise illisible -> FCFA
    assert money.budget_line(1000, 0, False) == "💰 Budget du jour : <b>5 000 FCFA</b> · tout est réparti"
    assert settle._sum_txt([combo(SUR, result="win", odds_final=5.0, cur="A<B", played={"odds": 5.0, "stake": 100})]).count("<") == 0
    assert app._signed(1000, {"cur": "A<B"}) == "+1 000 FCFA"
    assert money.signed(5, "€") == "+5 €" and money.money(5, "&") == "5 FCFA"
    money.set_daily(5000)
    # _sum_txt : seuls les paris NOTÉS comptent, tous en FCFA (une ligne sans devise est en FCFA)
    old = combo(SUR, 5.0, 10, cur=None, result="win", odds_final=5.0)
    new_loss = combo(COSTAUD, 13.0, 1000, result="loss")
    new_win = combo(SUR, 5.0, 500, result="win", odds_final=5.0)
    assert settle._sum_txt([old, new_loss, new_win]) == "", "une mise conseillée seule ne compte pas"
    n = lambda c, o, st: c | {"played": {"odds": o, "stake": st}}                                         # noqa: E731
    assert settle._sum_txt([n(old, 5.0, 10), n(new_loss, 13.0, 1000)]) == "−960 FCFA", settle._sum_txt([n(old, 5.0, 10), n(new_loss, 13.0, 1000)])
    assert settle._sum_txt([n(old, 5.0, 10)]) == "+40 FCFA" and settle._sum_txt([n(new_loss, 13.0, 1000), n(new_win, 5.0, 500)]) == "+1 000 FCFA"
    assert settle._sum_txt([n(new_win, 6.0, 500)]) == "+2 500 FCFA"                                       # cote corrigée
    assert settle._sum_txt([n(new_loss, 13.0, 1000) | {"skipped": True}]) == ""                           # ancien « pas joué » : hors compte
    # bilan détail : FCFA en FCFA, cote corrigée, pas joué
    bets = {"picks": [], "combos": [combo(SUR, 5.0, 1000, result="win", odds_final=5.0, played={"odds": 5.5, "stake": 1000}),
                                    combo(COSTAUD, 13.0, 500, result="loss", skipped=True),
                                    combo(C.COMBOS[3][0], 50.0, 200, result="loss")]}
    store.save("bets.json", bets)
    track.save([])
    det = settle.bilan(bets, detail=True)
    sur_line = next(x for x in det.splitlines() if x.startswith(fmt.tier_name(SUR).split(" (")[0]))
    assert "+4 500 FCFA" in sur_line and " u" not in sur_line and "unités" not in sur_line, sur_line
    cost_line = next(x for x in det.splitlines() if x.startswith(fmt.tier_name(COSTAUD).split(" (")[0]))
    assert "FCFA" not in cost_line and "tes paris" not in cost_line, cost_line          # pas noté (ancien « pas joué ») : aucun montant
    # played.summary_lines : en FCFA, « pas joué » exclu
    lines = played.summary_lines(bets, [], [])
    assert lines and "résultat +4 500 FCFA" in lines[0] and " u" not in lines[0].split("résultat")[1], lines
    assert "1 gagné sur 1" in lines[0] or "1 gagné sur 2" in lines[0], lines
    mixed = {"picks": [], "combos": [combo(SUR, 5.0, 10, cur=None, result="win", odds_final=5.0, played={"odds": 5.0, "stake": 10}),
                                     combo(COSTAUD, 13.0, 1000, result="loss", played={"odds": 13.0, "stake": 1000})]}
    assert "résultat −960 FCFA" in played.summary_lines(mixed, [], [])[0]          # plus d'unités : tout en FCFA
    # bilan simple : le résultat de TES paris notés, par palier
    simple = settle.bilan(mixed)
    assert "· tes paris +40 FCFA" in simple and "· tes paris −1 000 FCFA" in simple and " u)" not in simple, simple
    print("ok  affichages : journée riche, devise échappée, tout en FCFA, cote corrigée")


def test_commands_numbers_guard():
    wipe()
    store.save("today.json", {"day": DAY, "picks": [leg(1, 1.9, 0.6, 0.08)], "pronos": []})
    assert app.handle_command("/combi inf") == "Exemple : /combi 30, ou /combi 20 tirs fautes" and app.handle_command("/combi nan") == "Exemple : /combi 30, ou /combi 20 tirs fautes"
    for arg in ("", " inf", " -5", " 50000"):                  # /bank ne règle plus rien : Kelly travaille en FCFA avec le budget du jour
        r = app.handle_command("/bank" + arg)
        assert "ne suit plus de bankroll en unités" in r and "FCFA" in r and "Mon capital" in r and not r.startswith("Montant"), r
    assert bankroll.load()["bankroll"] == 100.0 and bankroll.load()["initial"] == 100.0
    print("ok  /combi refuse inf / nan ; /bank est inoffensif (FCFA et budget du jour)")


def main():
    test_amount_parsing()
    test_allocate_robust()
    test_no_fcfa_in_bankroll()
    test_played_never_switches_real_mode_for_budget_or_zero()
    test_zero_stake_has_no_buttons()
    test_pas_joue_refused_once_settled()
    test_free_text_routing()
    test_autre_cote_answer()
    test_display()
    test_commands_numbers_guard()
    print("tests interface budget : OK")


if __name__ == "__main__":
    main()
