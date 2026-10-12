"""Je note ce que je joue, en FCFA : seuls les paris NOTÉS (« ✅ Je l'ai joué », « ✅ Je le joue », /joue) comptent (résultats, bilan, alertes « tu as
joué »), la mise par défaut est la mise conseillée du budget du jour (sinon Kelly la demande), jamais d'unités, migration des anciennes notes,
et les paris du matin bâtis avant les cotes 1xBet passent aux cotes 1xBet (avec leur code coupon) au relevé suivant. Hors ligne (1xBet,
Telegram et coupons simulés). Lancer : python -m tests.test_notes_fcfa"""
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-notes-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-notes-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-notes-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("STATS_API_KEY", None)
os.environ["XBET_PROXY_FILE"] = os.path.join(os.environ["DATA_DIR"], "pas-de-fichier.env")      # jamais le vrai proxy de la machine
os.environ["XBET_PROXY"] = "http://user:secret@proxy.test:80"

from kelly import app, bankroll, calib, chat, config as C, custom, fair, fmt, migrate, money, played, settle, store, telegram, track, xalerts, xprice  # noqa: E402
from kelly.sources import xbet  # noqa: E402

DAY = "2030-01-01"
KO = f"{DAY}T19:00:00Z"
TYP, XB, PROB = 1.6, 1.5, 0.7                       # cote typique des books, cote 1xBet, proba de consensus
UTC = timezone.utc
SUR, COSTAUD = C.COMBOS[0][0], C.COMBOS[1][0]
FCFA = "FCFA"


def at(h, m=0, day=1):
    return datetime(2030, 1, day, h, m, tzinfo=UTC)


def reset():
    """Données du jour vides, fiabilité neutre, pas de budget, aucune alerte envoyée, aucune question en attente, pas de migration faite."""
    for f in ("tracked.json", "fair.json", fair.COMBOS_FILE):
        store.save(f, [])
    store.save("bets.json", {"picks": [], "combos": []})
    store.save("bankroll.json", {"bankroll": 100.0, "initial": 100.0})
    for f in (app.COMBO_UPDATES, money.FILE, calib.FILE, custom.FILE, xalerts.FILE, chat.PENDING_FILE, "odds_history.json", migrate.FILE):
        store.save(f, {})
    store.save("today.json", {"day": DAY, "picks": [], "pronos": []})
    calib._memo.clear()
    C.XALERTS_ENABLED, C.XALERT_LIVE_MAX = True, 3


def pick(n, **kw):
    """Pari réel (The Odds API) du match e<n> : 1X2 de l'équipe à domicile à la cote typique TYP."""
    home, away = f"Home{n}", f"Away{n}"
    c = {"key": f"e{n}|h2h||{home}", "id": f"e{n}", "sport_key": "soccer_spain_la_liga", "group": "soccer", "event": f"{home} - {away}",
         "home": home, "away": away, "league": "Liga", "start": "19:00", "commence": KO, "market": "h2h", "who": None, "point": None,
         "name": home, "pick": fmt.label("h2h", home, None, None, "soccer"), "prob": PROB, "prob_book": PROB, "prob_src": "consensus",
         "n_sharp": 1, "odds": TYP, "best_odds": 1.8, "book": "Coolbet", "edge": PROB * TYP - 1, "spread": 0.02, "steam": 0.0,
         "n_books": 10, "weight": 1.0, "score": 0.12 + n / 100, "why": ""}
    return c | kw


def leg(i, odds_, prob, edge):
    """Jambe du matin pour build_day (voir tests/test_budget.py)."""
    return {"key": f"k{i}", "id": f"e{i}", "odds": odds_, "prob": prob, "edge": edge, "n_books": 14, "weight": 1.0, "score": edge,
            "market": "h2h", "name": f"H{i}", "why": "", "pm": None, "commence": f"{DAY}T{12 + i % 10:02d}:00:00Z",
            "home": f"H{i}", "away": f"A{i}", "event": f"H{i} - A{i}", "league": "L", "start": f"{12 + i % 10}:00",
            "pick": f"H{i} gagne", "group": "soccer", "sport_key": "soccer_epl"}


def prono(i, **kw):
    """Pronostic 💎 : moins de 3,5 buts à 1,55 chez 1xBet (cote du matin 1,55), match m<i>."""
    c = {"id": f"m{i}", "sport_key": "soccer_epl", "home": f"H{i}", "away": f"A{i}", "commence": KO, "start": "19:00", "league": "L",
         "event": f"H{i} - A{i}", "market": "totals", "name": "Under", "point": 3.5, "who": None, "prob": 0.72, "p0": 0.72,
         "min_odds": 1.46, "conf": 0.6, "n_min": 8, "score": 0.2 - i / 1000, "key": f"m{i}|totals||3.5|Under",
         "pick": fmt.label("totals", "Under", 3.5, None, "soccer"), "xodds": 1.55, "xodds0": 1.55, "xev": 0.12, "xgid": 900 + i,
         "xref": [900 + i, 10, 3.5],
         "psh": {"under|3.5": [0.70, "pinnacle", 1]}}      # Pinnacle voit 70 % : sans avis du marché, l'ensemble (buts) ne validerait jamais ce pari
    return c | kw


def unpriced(i, **kw):
    """Le même pronostic avant la lecture de 1xBet : « à vérifier » (cote mini seulement)."""
    return prono(i, xodds=None, xodds0=None, xev=None, xgid=None, xref=None, **kw)


def pronos(n=7, **kw):
    return [prono(i, **kw) for i in range(n)]


def fake_fetch(priced, calls=None):
    """1xBet vu par les tests : `priced` = {id Kelly: {(marché, côté, ligne): cote}} ; « HOME » = l'équipe à domicile."""
    def fetch(events, deadline=None, wanted=None, live=False, fresh_min=None, max_matches=None):
        if calls is not None:
            calls.append([ev["id"] for _, ev in events])
        out = {}
        for k, ev in events:
            px = priced.get(ev["id"])
            if px is not None:
                gid = 9000 + int(ev["id"][1:])
                px = {(m, ev["home_team"] if n == "HOME" else n, p): v for (m, n, p), v in px.items()}
                out[ev["id"]] = {"gid": gid, "px": px, "refs": {k_: [gid, 1 + i, 0] for i, k_ in enumerate(px)}}
        return out
    return fetch


def under_px(n=9):
    """Prix 1xBet de « moins de 3,5 buts » à 1,55 pour les matchs m0..m<n-1>."""
    return {f"m{i}": {("totals", "Under", 3.5): 1.55} for i in range(n)}


class Fakes:
    """Messages et boutons capturés, code coupon simulé (None : échec), relevé en direct des pronostics simulé (1,55 sur « moins de 3,5 buts ») :
    aucun réseau."""

    def __init__(self, code="K7Q2X"):
        self.sent, self.edits, self.coupons, self.code = [], [], [], code

    def __enter__(self):
        self.old = (xbet.save_coupon, telegram.send, telegram.edit_buttons, xprice.live_check)
        real_check = xprice.live_check
        xbet.save_coupon = lambda ls: (self.coupons.append([c.get("id") for c in ls]), self.code)[1]
        telegram.send = lambda text, *a, **k: (self.sent.append(text), True)[1]
        telegram.edit_buttons = lambda mid, markup, chat_id=None: (self.edits.append((mid, markup)), True)[1]
        px = {f"m{i}": {"gid": 900 + i, "px": {("totals", "Under", 3.5): 1.55}, "refs": {("totals", "Under", 3.5): [900 + i, 10, 3.5]}}
              for i in range(9)}
        xprice.live_check = lambda ids, now=None, fetch=None, fresh_min=None: real_check(
            ids, now, fetch=lambda ev, deadline=None, wanted=None, live=True, fresh_min=None:
            {k: v for k, v in px.items() if k in {e["id"] for _, e in ev}})
        return self

    def __exit__(self, *a):
        xbet.save_coupon, telegram.send, telegram.edit_buttons, xprice.live_check = self.old

    def last_buttons(self):
        return [b["callback_data"] for row in self.edits[-1][1]["inline_keyboard"] for b in row if "callback_data" in b]

    def last_texts(self):
        return [b["text"] for row in self.edits[-1][1]["inline_keyboard"] for b in row if "callback_data" in b]


def buttons(msg):
    return [b["callback_data"] for row in (getattr(msg, "markup", None) or {"inline_keyboard": []})["inline_keyboard"] for b in row if "callback_data" in b]


def texts(msg):
    return [b["text"] for row in (getattr(msg, "markup", None) or {"inline_keyboard": []})["inline_keyboard"] for b in row if "callback_data" in b]


def no_units(text, what=""):
    """Aucune mise ni aucun gain en « u » / « unités » dans le texte d'un message (HTML retiré) ; le mot seul, dans une explication, est permis."""
    t = fmt.plain(str(text))
    assert not re.search(r"\d\s?(?:u|unités?)(?![A-Za-zÀ-ÿ])", t) and "💼 Bankroll" not in t and "Bankroll :" not in t, (what, t)      # aucun montant en unités


def combo(tier, odds_=5.0, stake=None, cur=None, result=None, **kw):
    """Combiné de bets.json (un seul pari « k<tier> »)."""
    c = {"tier": tier, "keys": [f"k{tier[:2]}"], "odds": odds_, "prob": 0.2, "stake": stake, "date": DAY, "result": result}
    return c | ({"cur": cur} if cur else {}) | kw


# ---------- 1. la mise par défaut ----------

def test_default_stake_with_budget():
    reset()
    money.set_daily(5000)
    store.save("bets.json", {"picks": [], "combos": [combo(SUR, 5.0, stake=1000, cur=FCFA)]})
    t = played.find("sur", DAY)
    assert t.stake == 1000, "mise conseillée du matin (budget du jour)"
    reply = played.command(["sur", "5,40"], DAY)            # la cote seule : la mise conseillée est reprise
    assert reply.startswith("✅ Noté") and "joué à 5,40 chez 1xBet, mise 1 000 FCFA" in reply, reply
    c = store.load("bets.json", {})["combos"][0]
    assert c["played"]["odds"] == 5.4 and c["played"]["stake"] == 1000 and c["cur"] == FCFA, c
    # une autre cote sur un pari déjà noté garde la mise notée (et la mise donnée la remplace)
    assert "mise 1 000 FCFA" in played.command(["sur", "5,10"], DAY) and store.load("bets.json", {})["combos"][0]["played"]["odds"] == 5.1
    assert "mise 2 000 FCFA" in played.command(["sur", "5,10", "2000"], DAY)
    # budget fixé après le matin (pas de mise du matin) : la mise conseillée est calculée sur le budget d'aujourd'hui
    store.save("bets.json", {"picks": [], "combos": [combo(SUR, 5.0, prob=0.25)]})      # espérance positive (+25 %) : à 0 %, Kelly ne conseille aucune mise
    want = money.allocate([{"kind": "combo", "tier": SUR, "prob": 0.25, "odds": 5.0}])[0][0]
    assert want > 0 and played.find("sur", DAY).stake == want, (want, played.find("sur", DAY).stake)
    assert f"mise {money.money(want)}" in played.command(["sur", "5,40"], DAY)
    # mise du matin jugée trop petite (0) : pas de mise conseillée, Kelly la demande
    store.save("bets.json", {"picks": [], "combos": [combo(SUR, 5.0, stake=0)]})
    assert played.find("sur", DAY).stake == 0 and played.command(["sur", "5,40"], DAY).startswith("Avec quelle mise en FCFA ?")
    print("ok  1 cote seule + budget : mise conseillée en FCFA (du matin, ou calculée sur le budget), cote corrigée : mise gardée")


def test_default_stake_without_budget():
    reset()
    store.save("bets.json", {"picks": [], "combos": [combo(SUR, 5.0, stake=2.0)]})          # ancienne ligne : mise en unités, jamais reprise
    assert played.find("sur", DAY).stake == 0
    r = played.command(["sur", "5,40"], DAY)
    assert r == "Avec quelle mise en FCFA ? Réponds ta mise, par exemple 1 000 (cote 5,40 retenue).", r
    assert "played" not in store.load("bets.json", {})["combos"][0]
    # par le bouton : la question, puis la cote seule ne suffit pas (la question reste ouverte), puis « 5,40 1 000 »
    with Fakes() as f:
        ask = chat.on_callback(f"joue|sur|{DAY}", 11)
        assert ask.startswith("À quelle cote et avec quelle mise en FCFA ?") and "1 000" in ask, ask
        again = chat.route("5,40")[1]
        assert again.startswith("Avec quelle mise en FCFA ?") and chat.get_pending()["what"] == "sur" and chat.get_pending()["msg"] == 11, again
        assert "played" not in store.load("bets.json", {})["combos"][0]
        out = chat.route("5,40 1 000")[1]
    c = store.load("bets.json", {})["combos"][0]
    assert out.startswith("✅ Noté") and "mise 1 000 FCFA" in out and chat.get_pending() is None, out
    assert c["played"]["odds"] == 5.4 and c["played"]["stake"] == 1000 and c["cur"] == FCFA and c["stake"] is None, c
    assert f.edits[-1][0] == 11 and f.last_texts() == ["✅ Joué à 5,40 · 1 000 FCFA", "✏️ Autre cote", "↩️ Annuler"], f.last_texts()
    # /joue tapé sans mise ouvre la même attente
    store.save("bets.json", {"picks": [], "combos": [combo(SUR, 5.0)]})
    chat.clear_pending()
    with Fakes():
        assert app.handle_command("/joue sur 5,40").startswith("Avec quelle mise en FCFA ?") and chat.get_pending()["what"] == "sur"
        assert chat.route("5,40 2 000")[1].startswith("✅ Noté") and store.load("bets.json", {})["combos"][0]["played"]["stake"] == 2000
    # les protections de la saisie libre restent : « 2 000 » seul est ambigu, une cote absurde est refusée, l'attente reste
    chat.clear_pending()
    with Fakes():
        chat.on_callback(f"joue|sur|{DAY}", 12)
        for txt in ("2 000", "5000", "1", "5,40 2 00"):
            r = chat.route(txt)[1]
            assert "pas compris la cote" in r and chat.get_pending()["msg"] == 12, (txt, r)
    print("ok  1 cote seule sans budget : la mise est demandée (jamais d'unités), « 5,40 1 000 » noté en FCFA, protections de saisie gardées")


# ---------- 2. seuls les paris notés comptent ----------

def settled_world():
    """Quatre paris réglés d'un jour avec mise conseillée (cur + stake) : Sûr gagné, Costaud perdu, pari du jour perdu, combiné à vérifier gagné."""
    store.save("bets.json", {"picks": [], "combos": [
        combo(SUR, 5.0, 1000, FCFA, result="win", odds_final=5.0, keys=["a"]),
        combo(COSTAUD, 13.0, 500, FCFA, result="loss", keys=["b"])]})
    track.save([{"key": "s", "id": "s", "date": DAY, "sent": True, "single": True, "odds": 1.9, "prob": 0.6, "stake": 800, "cur": FCFA,
                 "result": "loss", "pick": "X gagne", "market": "h2h", "home": "H", "away": "A", "commence": KO}])
    store.save(fair.COMBOS_FILE, [{"date": DAY, "tier": 5, "min_odds": 5.5, "prob": 0.2, "keys": ["f1", "f2"], "stake": 400, "cur": FCFA,
                                   "result": "win", "legs": [{"key": "f1", "min_odds": 2.2, "result": "win"},
                                                             {"key": "f2", "min_odds": 2.5, "result": "win"}]}])


def money_result():
    return money.result(store.load("bets.json", {}), track.load(), fair.load_combos())


def test_only_noted_bets_count():
    reset()
    money.set_daily(5000)
    settled_world()
    bets, rows, fc = store.load("bets.json", {}), track.load(), fair.load_combos()
    # des mises conseillées seules : rien ne compte, nulle part
    assert money_result() == (0, 0, 0) and money.summary_lines(bets, rows, fc, today=DAY) == []
    assert all(money.gain(x) is None for x in bets["combos"] + rows + fc)
    assert played.items(bets, rows, fc) == [] and played.summary_lines(bets, rows, fc) == []
    bilan = str(settle.bilan(bets, rows=rows))
    assert "Tes paris notés" not in bilan and "FCFA" not in bilan and "Joués chez 1xBet" not in bilan, bilan
    assert "🎯 <b>Combinés</b> : 1 gagné sur 2" in bilan, bilan
    det = settle.bilan(bets, rows=rows, detail=True)
    assert "FCFA" not in det and "tes paris" not in det and "Joués chez 1xBet" not in det, det
    cmd = str(app.handle_command("/bilan"))
    assert "Budget du jour" in cmd and "Tes paris notés" not in cmd, cmd
    # alertes « tu as joué » : un pari seulement conseillé n'est pas un pari joué
    unsettled = [r | {"result": None} for r in rows]
    comb = [c | {"result": None} for c in bets["combos"]]
    assert xalerts.took_bets(unsettled, comb, DAY) == ({}, set()), "mise conseillée seule : jamais « tu l'as joué »"
    assert not xalerts._took(unsettled[0]) and not xalerts._took({"cur": FCFA, "stake": 500})
    # l'ancienne règle auto-comptait les paris du budget : plus rien ne se compte tout seul, même après un « ❌ Pas joué » d'un ancien message
    assert "retiré de ton bilan" in chat.on_callback(f"skip|sur|{DAY}") or "déjà réglé" in chat.on_callback(f"skip|sur|{DAY}")
    # les mêmes paris, NOTÉS (cote et mise de 1xBet) : ils comptent, en FCFA
    bets["combos"][0]["played"] = {"odds": 5.0, "stake": 1000}
    bets["combos"][1]["played"] = {"odds": 13.0, "stake": 500}
    rows[0]["played"] = {"odds": 1.9, "stake": 800}
    fc[0]["played"] = {"odds": 5.5, "stake": 400}
    store.save("bets.json", bets)
    track.save(rows)
    store.save(fair.COMBOS_FILE, fc)
    n, total, staked = money_result()
    assert n == 4 and abs(total - (4000 - 500 - 800 + 1800)) < 1e-6 and staked == 2700, (n, total, staked)
    lines = money.summary_lines(bets, rows, fc, today=DAY)
    assert lines[0].startswith("7 derniers jours : +4 500 FCFA sur 4 paris (2 gagnés)") and "FCFA" in lines[1], lines
    simple = str(settle.bilan(bets, rows=rows))
    assert "💰 <b>Tes paris notés</b>" in simple and "+4 500 FCFA sur 4 paris" in simple, simple
    no_units(simple, "bilan")
    det = settle.bilan(bets, rows=rows, detail=True)
    assert "Joués chez 1xBet : 2 gagnés sur 4 · résultat +4 500 FCFA" in det and "tes paris : +4 000 FCFA" in det, det
    no_units(det, "bilan détail")
    cmd = app.handle_command("/bilan")
    assert "+4 500 FCFA" in cmd and "Budget du jour" in cmd and cmd.markup["inline_keyboard"][0][0]["callback_data"] == "bilan|detail"
    no_units(cmd, "/bilan")
    no_units(app.handle_command("/bilan détail"), "/bilan détail")
    unsettled = [r | {"result": None} for r in track.load()]
    comb = [c | {"result": None} for c in store.load("bets.json", {})["combos"]]
    took, noted = xalerts.took_bets(unsettled, comb, DAY)
    assert set(took) == {"s", "a", "b"} and noted == set(took) and took["s"] == 1.9, (took, noted)
    # la bankroll en unités n'est jamais touchée par l'argent
    bankroll.apply_results(comb + unsettled)
    assert bankroll.load()["bankroll"] == 100.0
    print("ok  2 mises conseillées seules : hors résultats, bilan et alertes ; notées : comptées en FCFA, bilan sans unités")


def test_morning_results_only_noted():
    reset()
    money.set_daily(5000)
    settled_world()
    rows = track.load()
    msg = str(app.results_message(store.load("bets.json", {}), [dict(r) for r in rows], [dict(rows[0])]))
    assert "Pari du jour" in msg and "FCFA" not in msg and "bankroll" not in msg and "Hier : 1 combiné gagné sur 2" in msg, msg
    no_units(msg, "résultats du matin")
    # les mêmes paris NOTÉS : le montant apparaît (cote et mise de 1xBet, en FCFA), et le résultat de tes paris notés
    bets = store.load("bets.json", {})
    bets["combos"][0]["played"] = {"odds": 5.0, "stake": 1000}
    rows[0]["played"] = {"odds": 1.9, "stake": 800}
    msg = str(app.results_message(bets, [dict(r) for r in rows], [dict(rows[0])]))
    assert "· −800 FCFA" in msg and "chez 1xBet</b> · +4 000 FCFA" in msg, msg
    assert "résultat de tes paris notés : +3 200 FCFA" in msg, msg
    no_units(msg, "résultats du matin (notés)")
    # rien de noté et rien de gagné : ni bankroll ni unités, une ligne honnête
    only_lost = {"picks": [], "combos": [combo(COSTAUD, 13.0, 500, FCFA, result="loss", keys=["b"])]}
    msg = str(app.results_message(only_lost, [], []))
    assert "COSTAUD perdu" in msg and "FCFA" not in msg and "bankroll" not in msg, msg
    print("ok  2 résultats du matin : un montant seulement pour un pari noté, jamais de bankroll en unités")


# ---------- 3. les boutons ----------

def test_buttons():
    reset()
    money.set_daily(5000)
    store.save("bets.json", {"picks": [], "combos": []})
    legs = [leg(i, 1.9, 0.6, 0.08) for i in range(8)]
    msgs, sent_picks, sent_combos = app.build_day(legs, date(2030, 1, 1))
    guys = [m for m in msgs if getattr(m, "markup", None) and "inline_keyboard" in m.markup]
    assert len(guys) == len(sent_combos) + 1                          # chaque pari du matin : un seul bouton
    for m in guys:
        assert len(texts(m)) == 1 and re.fullmatch(r"✅ Je l'ai joué — [\d ]+ FCFA", texts(m)[0]), texts(m)
        assert buttons(m)[0].startswith("joue|") and "Pas joué" not in json.dumps(m.markup, ensure_ascii=False)
    sur = next(m for m in msgs if "SÛR" in m)
    stake = next(c for c in sent_combos if c["tier"] == SUR)["stake"]
    assert texts(sur) == [f"✅ Je l'ai joué — {money.money(stake)}"] and f"💰 Mise : <b>{money.money(stake)}</b>" in sur, (texts(sur), sur)
    # sans budget : le bouton sans montant, et une ligne (une par message) qui dit de régler le budget
    store.save(money.FILE, {})
    msgs, _, combos2 = app.build_day(legs, date(2030, 1, 1))
    for m in (m for m in msgs if getattr(m, "markup", None) and "inline_keyboard" in m.markup):
        assert texts(m) == ["✅ Je l'ai joué"], texts(m)
    sur = next(m for m in msgs if "SÛR" in m)
    assert str(sur).count("Mise : fixe ton capital avec « 💼 Mon capital »") == 1 and "💰 mise" not in sur, sur
    assert all(c["stake"] is None and "cur" not in c for c in combos2)
    for m in msgs:
        no_units(m, "message du matin sans budget")
    # « Mes paris » : ✅ avec la mise conseillée pour un pari pas noté ; « ✏️ Autre cote » + « ↩️ Annuler » pour un pari noté
    reset()
    money.set_daily(5000)
    store.save("bets.json", {"picks": [], "combos": [combo(SUR, 5.0, 1000, FCFA)]})
    todo = played.todo(DAY)
    assert texts(todo)[0] == "✅ Sûr · Je l'ai joué — 1 000 FCFA" and buttons(todo) == [f"joue|sur|{DAY}|l0"], texts(todo)       # les boutons de la liste portent son numéro de page
    assert "▫️ Sûr · " in todo and " · mise 1 000 FCFA" in todo and "Seuls les paris notés comptent" in todo, todo
    with Fakes() as f:
        chat.on_callback(f"joue|sur|{DAY}", 31)
        assert "ta mise sera de 1 000 FCFA" in chat.on_callback(f"joue|sur|{DAY}", 31)      # la question annonce la mise conseillée
        chat.route("5,40")
    todo = played.todo(DAY)
    assert "✅ Sûr · joué à 5,40 · 1 000 FCFA" in todo and buttons(todo) == [f"joue|sur|{DAY}|l0", f"annule|sur|{DAY}|l0"], todo
    assert texts(todo) == ["✏️ Autre cote : Sûr", "↩️ Annuler : Sûr"], texts(todo)
    assert f.edits[-1][0] == 31 and f.last_texts() == ["✅ Joué à 5,40 · 1 000 FCFA", "✏️ Autre cote", "↩️ Annuler"]
    # ↩️ Annuler = /joue <quoi> 0 : la note tombe, « ✅ Je l'ai joué — 1 000 FCFA » revient (la mise conseillée reste)
    with Fakes() as f:
        assert chat.on_callback(f"annule|sur|{DAY}", 31).startswith("Note annulée")
        assert f.last_texts() == ["✅ Je l'ai joué — 1 000 FCFA"] and f.last_buttons() == [f"joue|sur|{DAY}"]
        assert chat.on_callback(f"annule|sur|{DAY}", 31).endswith("n'était pas noté.")
    c = store.load("bets.json", {})["combos"][0]
    assert "played" not in c and c["stake"] == 1000 and c["cur"] == FCFA
    assert played.command(["sur", "0"], DAY).endswith("n'était pas noté.")
    # aucun nouveau message ne crée « ❌ Pas joué » ; les anciens boutons répondent encore
    assert "retiré de ton bilan" in chat.on_callback(f"skip|sur|{DAY}", 31) and "compté comme joué" in chat.on_callback(f"unskip|sur|{DAY}", 31)
    print("ok  3 boutons : « ✅ Je l'ai joué — X FCFA », « ✏️ Autre cote » + « ↩️ Annuler » (= /joue 0), plus de « ❌ Pas joué »")


# ---------- 4. « ✅ Je le joue » ----------

def custom_msg(budget=True, target=5.0):
    reset()
    if budget:
        money.set_daily(5000)
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos()})
    with Fakes() as f:
        msg = app.custom_combo([], pronos(), target, DAY)
    return msg, f


def test_custom_is_a_note():
    msg, _ = custom_msg(budget=True, target=20.0)
    item = store.load(custom.FILE, {})["items"]["m1"]
    assert item["cur"] == FCFA and item["stake"] > 0 and f"{money.money(item['stake'])} (sur ton budget du jour)" in msg, (item, msg)
    no_units(msg, "combi sur mesure avec budget")
    with Fakes() as f:
        reply = chat.on_callback(f"cjoue|m1|{DAY}", 77)
    row = fair.load_combos()[0]
    assert row["custom"] and row["played"]["odds"] == row["min_odds"] == 21.49 and row["played"]["stake"] == item["stake"], row
    assert row["cur"] == FCFA and row["stake"] == item["stake"], row
    assert reply.startswith("✅ Noté : combi sur mesure (cote 20)") and f"mise {money.money(item['stake'])}" in reply, reply
    assert f.last_buttons() == ["noop", f"joue|vm1|{DAY}", f"annule|vm1|{DAY}", f"xmaj|m1|{DAY}"], f.last_buttons()
    assert f.last_texts()[1:3] == ["✏️ Autre cote", "↩️ Annuler"] and money_result()[0] == 0, "pas réglé : pas encore dans le résultat"
    text = str(played.todo(DAY))
    assert f"✅ Combi sur mesure (cote 20) · joué à 21,49 · {money.money(item['stake'])}" in text, text
    no_units(text, "Mes paris")
    # sans mise conseillée (pas de budget) : suivi, puis SEULE la mise est demandée (la cote 1xBet du combiné est connue)
    msg, _ = custom_msg(budget=False)
    no_units(msg, "combi sur mesure sans budget")
    assert "Mise : fixe ton capital avec « 💼 Mon capital »" in msg and store.load(custom.FILE, {})["items"]["m1"]["stake"] is None
    with Fakes() as f:
        reply = chat.on_callback(f"cjoue|m1|{DAY}", 78)
        row = fair.load_combos()[0]
        assert "played" not in row and reply.count("Avec quelle mise en FCFA ?") == 1 and "À quelle cote" not in reply, (row, reply)
        assert chat.get_pending()["what"] == "vm1" and chat.get_pending()["odds"] == 5.77
        assert f.last_buttons() == [f"joue|vm1|{DAY}", f"xmaj|m1|{DAY}"] and f.last_texts()[0] == "✅ Je l'ai joué"
        again = chat.on_callback(f"cjoue|m1|{DAY}", 78)
        assert "pas encore noté" in again and "Avec quelle mise en FCFA ?" in again            # deuxième appui : la question est reposée
        out = chat.route("500")[1]                                                              # un montant seul = la mise, à la cote 1xBet
    row = fair.load_combos()[0]
    assert out.startswith("✅ Noté : combi sur mesure (cote 5)") and "joué à 5,77 chez 1xBet, mise 500 FCFA" in out and row["played"] == {
        "odds": 5.77, "stake": 500, "at": row["played"]["at"]} and row["cur"] == FCFA, (out, row)
    # budget fixé APRÈS le message du combiné : la mise conseillée est calculée à l'appui, et le combiné est noté tout de suite
    msg, _ = custom_msg(budget=False, target=20.0)
    money.set_daily(5000)
    with Fakes() as f:
        reply = chat.on_callback(f"cjoue|m1|{DAY}", 79)
    row = fair.load_combos()[0]
    assert reply.startswith("✅ Noté") and row["played"]["stake"] > 0 and row["cur"] == FCFA, (reply, row)
    # mise du matin trop petite (budget minuscule) : suivi sans mise conseillée, seule la mise est demandée
    msg, _ = custom_msg(budget=False)
    money.set_daily(1)
    with Fakes():
        reply = custom.track("m1", DAY, 80)
    assert "Avec quelle mise en FCFA ?" in reply and "À quelle cote" not in reply and "played" not in fair.load_combos()[0], reply
    # combiné à cote MINI (pas chez 1xBet : aucune cote 1xBet connue) : la cote est demandée, jamais notée à la cote mini à la place
    typ = [pick(i) for i in range(1, 5)]
    flat = [unpriced(i) for i in range(7)]
    for budget in (True, False):
        reset()
        if budget:
            money.set_daily(5000)
        store.save("today.json", {"day": DAY, "picks": [], "pronos": flat})
        with Fakes() as f:
            msg = app.custom_combo(typ, flat, 4.5, DAY)
            assert "À VÉRIFIER CHEZ 1XBET" in msg and f"cjoue|m1|{DAY}" in buttons(msg), msg
            reply = chat.on_callback(f"cjoue|m1|{DAY}", 81)
            row = fair.load_combos()[0]
            assert "played" not in row and not played.is_real(row), row
            assert chat.get_pending()["what"] == "vm1" and "odds" not in chat.get_pending()
            if budget:                                  # mise conseillée : seule la cote est demandée
                assert "À quelle cote as-tu joué" in reply and "ta mise sera de" in reply, reply
                out = chat.route("4,60")[1]
                assert "joué à 4,60 chez 1xBet, mise" in out and fair.load_combos()[0]["played"]["stake"] > 0, out
            else:                                       # aucune mise conseillée : la cote et la mise
                assert "À quelle cote et avec quelle mise en FCFA ?" in reply, reply
                assert chat.route("2 000")[1].startswith("Je n'ai pas compris la cote")              # ici « 2 000 » est ambigu (cote ou mise ?) : redemandé
                out = chat.route("4,60 800")[1]
                assert "joué à 4,60 chez 1xBet, mise 800 FCFA" in out, out
    print("ok  4 « Je le joue » = une note : cote 1xBet connue jamais demandée (mise conseillée : noté tout de suite ; sinon seule la mise) ; cote mini : cote (et mise) demandées")


# ---------- 5. jamais d'unités ----------

def test_no_units_anywhere():
    reset()
    legs = [leg(i, 1.9, 0.6, 0.08) for i in range(8)]
    fair_combo = (5, ([prono(i) for i in range(4)], 1.55 ** 4, 0.72 ** 4))
    for budget in (None, 5000):
        store.save(money.FILE, {})
        if budget:
            money.set_daily(budget)
        store.save("bets.json", {"picks": [], "combos": []})
        fc_stakes = {}
        with Fakes():
            msgs, _, _ = app.build_day(legs, date(2030, 1, 1), pronos=pronos(4), fair_combos=[fair_combo, (12, ([unpriced(i) for i in range(5)], 12.5, 0.1))],
                                       fair_stakes=fc_stakes, odds_time="09:30", fair_coupons={})
        for m in msgs:
            no_units(m, f"matin (budget {budget})")
        for m in (m for m in msgs if "COMBINÉ" in m):
            assert ("💰 Mise : <b>" in m and "FCFA" in m) if budget else \
                (str(m).count("💰 Mise : fixe ton capital") == 1
                 and "💰" not in fmt.plain(m).replace("💰 Mise : fixe", "").replace("💼 Mon capital", "")), m      # sans budget : aucune autre ligne de mise
    # /top, /match, /analyse, aide, budget, bank
    store.save("today.json", {"day": DAY, "picks": [pick(i) for i in range(1, 5)], "pronos": pronos(3)})
    for cmd in ("/top", "/match Home1", "/aide", "/budget", "/bank", "/bank 5000", "/joue", "budget", "bilan"):
        no_units(app.handle_command(cmd), cmd)
    assert "ne suit plus de bankroll en unités" in app.handle_command("/bank 5000") and bankroll.load()["bankroll"] == 100.0
    # budget : explication
    store.save(money.FILE, {})
    out = str(app.handle_command("/budget"))
    assert "AUJOURD'HUI" in out and "changer n'importe quel jour" in out and "Seuls les paris que tu notes" in out, out
    assert "Pas encore de budget" in out and "<b>Mon capital</b>" in str(app.HELP) and "bankroll" not in str(app.HELP)
    assert "Seuls les paris que tu <b>notes</b>" in str(app.HELP) and "✅ Je l'ai joué" in str(app.HELP) and "↩️ Annuler" in str(app.HELP)
    print("ok  5 jamais de « u » ni d'« unités » : messages du matin (avec et sans budget), commandes, aide, budget, bilan")


def test_no_units_refresh_and_alerts():
    legs = [prono(i) for i in range(4)]
    for budget in (False, True):
        reset()
        if budget:
            money.set_daily(5000)
        store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos(7)})
        fair.record_combos([(5, (legs, 1.55 ** 4, 0.72 ** 4))], DAY, {5: 500} if budget else None, FCFA if budget else None, coupons={5: "FAIRC"}, at="04:12")
        # renvoi du matin (un pari retiré)
        px = {f"m{i}": ({} if i == 1 else {("totals", "Under", 3.5): 1.55}) for i in range(7)}
        with Fakes() as f:
            app.run_xbet_refresh(now=at(13), announce=False, fetch=fake_fetch(px))
        for m in f.sent:
            no_units(m, f"relevé (budget {budget})")
        assert any("COMBINÉ À JOUR" in str(m) for m in f.sent)
    # alerte 💰
    reset()
    picks = [pick(1), pick(2)]
    xprice.price_picks(picks, fetch=fake_fetch({"e1": {("h2h", "HOME", None): 1.80}, "e2": {("h2h", "HOME", None): 1.5}}))
    prev, moved = {"o": 1.9, "pb": 0.52}, pick(1, odds=1.55, prob=0.65, prob_book=0.65)
    today = {"day": DAY, "picks": picks, "pronos": [], "picks_at": "09:30"}
    for budget in (False, True):
        if budget:
            money.set_daily(5000)
        items = xalerts.lag_items([(0.3, moved, prev)], today, xalerts.Reader(fake_fetch({"e1": {("h2h", "HOME", None): 1.8}})), at(18))
        assert len(items) == 1
        assert ("💰 Mise conseillée : <b>" in items[0]["text"] and "FCFA (sur ton budget du jour)" in items[0]["text"]) if budget else \
            "💡 Mise conseillée : fixe ton capital avec « 💼 Mon capital »" in items[0]["text"]
        no_units(items[0]["text"], "alerte 💰")
    print("ok  5 relevé 1xBet, combiné renvoyé et alerte 💰 : mise conseillée en FCFA (ou ligne « règle ton budget »), jamais d'unités")


# ---------- 6. la migration ----------

def test_migration():
    reset()
    old_combo = combo(SUR, 5.0, stake=2.0, result="win", odds_final=5.0, played={"odds": 5.5, "stake": 100, "at": "x"}, counted=True)
    free_combo = combo(COSTAUD, 13.0, stake=3.0)
    budget_combo = combo(C.COMBOS[3][0], 50.0, stake=200, cur=FCFA, played={"odds": 49.0, "stake": 150, "at": "x"})
    store.save("bets.json", {"picks": [], "combos": [old_combo, free_combo, budget_combo]})
    single = {"key": "s", "id": "s", "date": DAY, "sent": True, "single": True, "odds": 1.9, "prob": 0.6, "stake": None, "result": "loss",
              "pick": "X gagne", "market": "h2h", "played": {"odds": 1.85, "stake": 250, "at": "x"}}
    store.save("tracked.json", [single, {"key": "t", "id": "t", "date": DAY, "sent": False, "odds": 2.0, "result": None}])
    row_fair = {"date": DAY, "tier": 5, "min_odds": 5.5, "prob": 0.2, "keys": ["f1"], "stake": 1.0, "result": None, "legs": [],
                "played": {"odds": 5.0, "stake": 300, "at": "x"}, "custom": True}
    store.save(fair.COMBOS_FILE, [row_fair, {"date": DAY, "tier": 12, "min_odds": 12.5, "prob": 0.1, "keys": [], "legs": [], "result": None}])
    before = {n: json.dumps(store.load(n, None), sort_keys=True) for n in ("bets.json", "tracked.json", fair.COMBOS_FILE)}
    lens = (len(store.load("bets.json", {})["combos"]), len(store.load("tracked.json", [])), len(store.load(fair.COMBOS_FILE, [])))
    assert migrate.notes_fcfa() == 3, "trois lignes notées sans devise : le combiné, le pari du jour, le combiné à vérifier"
    bets, rows, fc = store.load("bets.json", {}), store.load("tracked.json", []), store.load(fair.COMBOS_FILE, [])
    assert (len(bets["combos"]), len(rows), len(fc)) == lens, "aucune liste ne rétrécit"
    assert bets["combos"][0]["cur"] == FCFA and bets["combos"][0]["played"] == old_combo["played"] and bets["combos"][0]["stake"] is None
    assert bets["combos"][0]["counted"] is True and bets["combos"][0]["result"] == "win", "drapeaux conservés"
    assert bets["combos"][1] == free_combo, "un pari non noté n'est pas touché"
    assert bets["combos"][2] == budget_combo, "un pari déjà en FCFA n'est pas touché"
    assert rows[0]["cur"] == FCFA and rows[0]["played"]["stake"] == 250 and rows[1] == {"key": "t", "id": "t", "date": DAY, "sent": False, "odds": 2.0, "result": None}
    assert fc[0]["cur"] == FCFA and fc[0]["played"]["stake"] == 300 and fc[0]["custom"] and fc[1]["tier"] == 12 and "cur" not in fc[1]
    assert store.load(migrate.FILE, {}) == {"notes_fcfa": True}
    # idempotente : une seconde exécution (et un redémarrage) ne change rien
    after = {n: json.dumps(store.load(n, None), sort_keys=True) for n in ("bets.json", "tracked.json", fair.COMBOS_FILE)}
    assert migrate.notes_fcfa() == 0 and migrate.ensure() == 0
    assert {n: json.dumps(store.load(n, None), sort_keys=True) for n in after} == after and after != before
    # même une ligne « à l'ancienne » ajoutée ensuite n'est plus migrée (marqueur) ; le bilan en argent compte toutes les notes réglées, une fois
    n, total, staked = money.result(bets, rows, fc)
    assert n == 2 and abs(total - (100 * (5.5 - 1) - 250)) < 1e-6 and staked == 350, (n, total, staked)
    assert money.gain(bets["combos"][2] | {"result": "win", "odds_final": 50.0}) == 150 * (49.0 - 1)
    # la bankroll en unités ne les recompte pas
    before_bank = bankroll.load()
    bankroll.save(before_bank | {"real": True})
    for x in bets["combos"] + rows + fc:
        x.pop("counted", None)
    assert bankroll.apply_results([x for x in bets["combos"] + rows if x.get("result") in ("win", "loss")])["bankroll"] == before_bank["bankroll"]
    # la migration s'exécute au démarrage du bot et au passage du matin (sans jamais lever)
    store.save(migrate.FILE, {})
    store.save("tracked.json", [dict(single, cur=None) | {"played": {"odds": 1.85, "stake": 250}}])
    old_poll = telegram.poll
    telegram.poll = lambda *a, **k: 0
    try:
        app.run_commands()
    finally:
        telegram.poll = old_poll
    assert store.load(migrate.FILE, {}) == {"notes_fcfa": True} and store.load("tracked.json", [])[0]["cur"] == FCFA
    # une lecture impossible n'empêche jamais le démarrage et retente plus tard (pas de marqueur)
    store.save(migrate.FILE, {})
    old_load = migrate.fair.load_combos
    migrate.fair.load_combos = lambda: (_ for _ in ()).throw(RuntimeError("base illisible"))
    try:
        assert migrate.ensure() == 0 and store.load(migrate.FILE, {}) == {}
    finally:
        migrate.fair.load_combos = old_load
    print("ok  6 migration : notes sans devise passées en FCFA une seule fois (mise de la note inchangée, listes intactes, rien de recompté), au démarrage")


# ---------- 7. noms dans « Mes paris » ----------

def test_names_in_todo():
    reset()
    legs = [prono(i, xodds=None, xref=None, xev=None, xgid=None) for i in range(4)]
    mini = fair.combo_row(25, (legs, 25.43, 0.02), DAY, 100, FCFA) | {"played": {"odds": 23.45, "stake": 100, "at": "x"}}
    real = fair.combo_row(12, ([prono(i) for i in range(4)], 25.1, 0.02), DAY) | {"played": {"odds": 24.0, "stake": 200, "at": "x"}}
    shots = fair.combo_row("tc5", ([prono(i, xodds=None) for i in range(4)], 5.6, 0.2), DAY, 100, FCFA)
    store.save(fair.COMBOS_FILE, [mini, real | {"min_odds": 25.1}, shots])
    money.set_daily(5000)
    text = str(played.todo(DAY))
    assert "✅ À vérifier (cote mini 25) · joué à 23,45 · 100 FCFA" in text, text
    assert "✅ Aux cotes 1xBet (cote 25,1) · joué à 24,00 · 200 FCFA" in text, text
    assert "▫️ Tirs cadrés · cote mini 5,60 · mise 100 FCFA" in text, text
    assert "À vérifier (cote mini 5)" not in text, "le palier d'un combiné « tirs » garde son nom"
    no_units(text, "Mes paris")
    assert played._fair_name("5") == "À vérifier (cote mini 5)" and played._fair_name("tc5") == "Tirs cadrés" and played._fair_name("tv5") == "Tirs vers le but"
    print("ok  7 Mes paris : « ✅ À vérifier (cote mini 25) · joué à 23,45 · 100 FCFA », « Aux cotes 1xBet (cote 25,1) », combinés tirs gardent leur nom")


# ---------- 8. partie B : combinés et pari du jour bâtis avant la lecture de 1xBet ----------

def morning_unpriced(stake=500, n=4, tier=5, extra=None):
    """Combiné du matin « 🎲 à vérifier » (cotes mini, sans code) du palier 5 sur m0..m<n-1>, today.json avec des pronostics aujourd'hui chiffrés par 1xBet."""
    money.set_daily(5000)
    legs = [unpriced(i) for i in range(n)]
    fair.record_combos([(tier, (legs, round(1.46 ** n, 3), 0.72 ** n))], DAY, {tier: stake}, FCFA)
    store.save("today.json", {"day": DAY, "picks": [], "pronos": extra if extra is not None else pronos(9)})
    store.save(app.COMBO_UPDATES, {})
    return legs


def test_upgrade_to_xbet_prices():
    reset()
    morning_unpriced()
    row = fair.load_combos()[0]
    assert not played.is_real(row) and "coupon" not in row
    todo0 = str(played.todo(DAY))
    assert "À vérifier (cote mini 5)" in todo0 and "🎟️" not in todo0 and "<code>" not in todo0, todo0
    got = {f"m{i}" for i in range(9)}
    with Fakes(code="UPG01") as f:
        n = app._update_combos(DAY, pronos(9), at(13), got)
    assert n == 1 and len(f.sent) == 1 and f.coupons == [["m0", "m1", "m2", "m3"]], (n, f.sent, f.coupons)
    msg = f.sent[0]
    assert "💎 Ton combiné du matin est maintenant aux cotes 1xBet de 13:00, avec son code coupon" in msg, msg
    assert "Code : <code>UPG01</code>" in msg and "<b>💎 COMBINÉ — cote 5,77</b>" in msg and "Cotes 1xBet de 13:00" in msg, msg
    assert buttons(msg) == [f"joue|v5|{DAY}", f"xmaj|5|{DAY}"] and texts(msg)[0] == "✅ Je l'ai joué — 500 FCFA", buttons(msg)
    assert "💰 Mise : <b>500 FCFA</b>" in msg
    no_units(msg, "combiné passé aux cotes 1xBet")
    row = fair.load_combos()[0]
    assert played.is_real(row) and row["coupon"] == "UPG01" and row["at"] == "13:00" and row["updated"] == "13:00", row
    assert row["stake"] == 500 and row["cur"] == FCFA and row["min_odds"] == round(1.55 ** 4, 2)
    assert store.load(app.COMBO_UPDATES, {})["tiers"]["5"]["total"] == round(1.55 ** 4, 3)
    todo = str(played.todo(DAY))
    assert "Aux cotes 1xBet (cote 5,8)" in todo and "cote 1xBet 5,77 (13:00)" in todo and "🎟️ <code>UPG01</code>" in todo, todo
    assert chat.refresh_button("v5", DAY) == ("🎟️ Vérifier ce combiné", f"xmaj|5|{DAY}")
    # relevé suivant, rien n'a bougé : pas de second message
    with Fakes() as f:
        assert app._update_combos(DAY, pronos(9), at(13, 30), got) == 0 and not f.sent and not f.coupons
    print("ok  8 combiné « à vérifier » du matin : passe aux cotes 1xBet au relevé (message « 💎 maintenant aux cotes 1xBet », code coupon, Mes paris)")

    # un pari retiré par 1xBet : complété par un remplaçant dans la fourchette du palier
    reset()
    morning_unpriced()
    cur = [prono(0), prono(1, xodds=None, xodds0=None, xev=None, xref=None, xnone=1)] + [prono(i) for i in range(2, 9)]
    with Fakes(code="UPG02") as f:
        n = app._update_combos(DAY, cur, at(13), got)
    assert n == 1 and "❌ Retiré : H1 – A1" in f.sent[0] and "➕ Ajouté" in f.sent[0] and "Code : <code>UPG02</code>" in f.sent[0], f.sent
    row = fair.load_combos()[0]
    assert played.is_real(row) and "m1|totals||3.5|Under" not in row["keys"] and 5.0 <= row["min_odds"] <= 6.5, row
    # un match pas relu : sa jambe reste « à vérifier », le combiné n'est pas renvoyé (silence)
    reset()
    morning_unpriced()
    with Fakes() as f:
        assert app._update_combos(DAY, pronos(9), at(13), {"m0", "m1", "m2"}) == 0 and not f.sent and not f.coupons
    assert not played.is_real(fair.load_combos()[0])
    # pas de remplaçant possible : rien n'est dit, le combiné reste à vérifier (il n'est pas déclaré mort)
    reset()
    morning_unpriced(extra=[prono(0), prono(1, xodds=None, xnone=1), prono(2), prono(3)])
    with Fakes() as f:
        assert app._update_combos(DAY, store.load("today.json", {})["pronos"], at(13), {"m0", "m1", "m2", "m3"}) == 0 and not f.sent
    row = fair.load_combos()[0]
    assert "dead" not in row and row["result"] is None and not played.is_real(row)
    # NOTÉ (le ticket est placé), mort, sur mesure ou commencé : jamais touché
    for label, mutate in (("noté", lambda r: r.update(played={"odds": 5.4, "stake": 500, "at": "x"})), ("mort", lambda r: r.update(dead=True)),
                          ("sur mesure", lambda r: r.update(custom=True))):
        reset()
        morning_unpriced()
        rows = fair.load_combos()
        mutate(rows[0])
        store.save(fair.COMBOS_FILE, rows)
        snap = json.dumps(fair.load_combos(), sort_keys=True)
        with Fakes() as f:
            assert app._update_combos(DAY, pronos(9), at(13), got) == 0 and not f.sent and not f.coupons, label
        assert json.dumps(fair.load_combos(), sort_keys=True) == snap and store.load(app.COMBO_UPDATES, {}).get("tiers", {}) == {}, label
    reset()
    morning_unpriced()
    with Fakes() as f:
        assert app._update_combos(DAY, pronos(9), at(19, 30), got) == 0 and not f.sent, "un match a commencé : en jeu"
    print("ok  8 complété par un remplaçant ; match pas relu, pas de remplaçant : silence ; noté / mort / sur mesure / commencé : intact")


def test_upgrade_through_refresh():
    """Le relevé complet (run_xbet_refresh) : les pronostics du matin, pas encore chiffrés, sont remis aux cotes 1xBet, et le combiné qui s'appuie
    dessus part aux cotes 1xBet avec son code."""
    reset()
    morning_unpriced(extra=[unpriced(i) for i in range(9)])
    with Fakes(code="UPG03") as f:
        app.run_xbet_refresh(now=at(15), announce=False, fetch=fake_fetch(under_px()))
    ups = [m for m in f.sent if "est maintenant aux cotes 1xBet de 15:00" in str(m)]
    assert len(ups) == 1 and "Code : <code>UPG03</code>" in ups[0], f.sent
    row = fair.load_combos()[0]
    assert played.is_real(row) and row["coupon"] == "UPG03"
    print("ok  8 relevé complet : le combiné « à vérifier » passe aux cotes 1xBet avec son code")


def single_row():
    """La ligne de suivi du pari du jour (e1) dans tracked.json."""
    return next(r for r in track.load() if r["key"] == "e1|h2h||Home1")


def single_world(price=1.8, noted=False, started=False, absent=False):
    """Pari du jour chiffré au matin à la cote de référence des autres books (pas 1xBet), today.json et pronostics 💎 pour que le relevé tourne."""
    reset()
    picks = [pick(1), pick(2)]
    track.record(picks, {picks[0]["key"]}, DAY, at(4), single_key=picks[0]["key"])
    if noted:
        rows = track.load()
        r1 = next(r for r in rows if r["key"] == "e1|h2h||Home1")
        r1["played"], r1["cur"] = {"odds": 1.6, "stake": 500, "at": "x"}, FCFA
        track.save(rows)
    if started:
        picks[0]["commence"] = f"{DAY}T13:00:00Z"
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": pronos(3), "picks_at": "09:30"})
    px = under_px(3) | ({"e1": {("btts", "Yes", None): 1.8}} if absent else {"e1": {("h2h", "HOME", None): price}})
    return picks, px


def test_single_priced_at_refresh():
    picks, px = single_world()
    row = single_row()
    assert row["single"] is True and not row.get("xodds") and "cote de référence 1,60 (pas 1xBet)" in str(played.todo(DAY))
    calls = []
    with Fakes(code="SGL01") as f:
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fake_fetch(px, calls))
    msgs = [m for m in f.sent if "Le pari du jour est maintenant aux cotes 1xBet de 13:00" in str(m)]
    assert len(msgs) == 1, f.sent
    msg = msgs[0]
    assert "Code : <code>SGL01</code>" in msg and "cote 1,80" in msg and "Cotes 1xBet de 13:00" in msg and "⭐ PARI DU JOUR" in msg, msg
    assert buttons(msg) == [f"joue|pari|{DAY}", f"xmajr|pari|{DAY}"] and texts(msg)[0] == "✅ Je l'ai joué", buttons(msg)
    assert f.coupons.count(["e1"]) == 1
    no_units(msg, "pari du jour aux cotes 1xBet")
    row = single_row()
    assert row["xodds"] == 1.8 and row["xref"] == [9001, 1, 0] and row["xat"] == "13:00" and row["coupon"] == "SGL01" and row["odds"] == TYP, row
    today = store.load("today.json", {})
    assert today["picks"][0]["xodds"] == 1.8 and today["picks"][0]["book"] == "1xBet" and today["picks"][0]["typ_odds"] == TYP, today["picks"][0]
    todo = str(played.todo(DAY))
    assert "Pari du jour · cote 1xBet 1,80 (13:00)" in todo and "🎟️ <code>SGL01</code>" in todo and "pas 1xBet" not in todo, todo
    assert chat.refresh_button("pari", DAY) == ("🎟️ Vérifier ce combiné", f"xmajr|pari|{DAY}")
    # relevé suivant : déjà aux cotes 1xBet, rien de plus
    with Fakes() as f:
        app.run_xbet_refresh(now=at(15), announce=False, fetch=fake_fetch(px))
    assert not [m for m in f.sent if "Le pari du jour est maintenant" in str(m)] and not f.coupons.count(["e1"])
    # relevé manuel : la réponse le dit
    picks, px = single_world()
    with Fakes(code="SGL02"):
        reply = app.manual_refresh(now=at(16), fetch=fake_fetch(px))
    assert "Le pari du jour est maintenant aux cotes 1xBet" in reply and "Rien de nouveau" not in reply, reply
    print("ok  8 pari du jour sans cote 1xBet : chiffré au relevé, un message (« 🎟️ … maintenant aux cotes 1xBet de 13:00 ») avec son code coupon, Mes paris à jour")


def test_single_stays_silent():
    # pas proposé par 1xBet : silence, il garde « cote de référence (pas 1xBet) »
    picks, px = single_world(absent=True)
    with Fakes() as f:
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fake_fetch(px))
    assert not [m for m in f.sent if "Le pari du jour" in str(m)] and ["e1"] not in f.coupons
    assert not single_row().get("xodds") and "cote de référence 1,60 (pas 1xBet)" in str(played.todo(DAY))
    # 1xBet le paie, mais plus assez : plus d'avantage, silence
    picks, px = single_world(price=1.2)
    with Fakes() as f:
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fake_fetch(px))
    assert not [m for m in f.sent if "Le pari du jour" in str(m)] and not single_row().get("xodds") and ["e1"] not in f.coupons
    # noté : jamais retouché (pas même relu)
    picks, px = single_world(noted=True)
    calls = []
    with Fakes() as f:
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fake_fetch(px, calls))
    assert not [m for m in f.sent if "Le pari du jour" in str(m)] and not any("e1" in c for c in calls), calls
    assert not single_row().get("xodds") and single_row()["played"]["odds"] == 1.6
    # match commencé : non plus
    picks, px = single_world(started=True)
    calls = []
    with Fakes() as f:
        app.run_xbet_refresh(now=at(14), announce=False, fetch=fake_fetch(px, calls))
    assert not [m for m in f.sent if "Le pari du jour" in str(m)] and not any("e1" in c for c in calls), calls
    # 1xBet muet à ce relevé : aucune nouvelle tentative qui consommerait des données
    picks, px = single_world()
    calls = []
    with Fakes() as f:
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fake_fetch({}, calls))
    assert not [m for m in f.sent if "Le pari du jour" in str(m)] and not single_row().get("xodds")
    # une panne du relevé ne casse jamais le relevé
    picks, px = single_world()

    def boom(*a, **k):
        raise RuntimeError("1xBet en panne")
    with Fakes() as f:
        assert app._upgrade_single(store.load("today.json", {}), at(13), {"m0"}, boom) == 0 and not f.sent
    print("ok  8 pari du jour : silence si 1xBet ne le propose pas ou ne le paie plus assez ; jamais s'il est noté, commencé, ou si 1xBet est muet")


# ---------- 9. « notes seulement » = l'ARGENT seulement : tout ce que Kelly envoie le matin reste suivi ----------

class Scores:
    """Règlement sans réseau : les scores viennent de `games` (id du match -> (buts domicile, buts extérieur)), aucune autre source."""

    def __init__(self, games, default=(1, 0)):
        self.games, self.default = games, default

    def __enter__(self):
        self.old = (settle.FootballData.match, settle.FootballData.score, settle.fmmatch.match_stats)

        def score(_, b):
            h, a = self.games.get(b["id"], self.default)
            return {"id": b["id"], "completed": True, "scores": [{"name": b["home"], "score": str(h)}, {"name": b["away"], "score": str(a)}]}
        settle.FootballData.match = lambda self, b: None
        settle.FootballData.score = score
        settle.fmmatch.match_stats = lambda b: None
        return self

    def __exit__(self, *a):
        settle.FootballData.match, settle.FootballData.score, settle.fmmatch.match_stats = self.old


def world():
    """6 matchs de 1X2 à la cote typique 1,60 ; 1xBet paie 1,50 sur les 4 premiers (pari du jour : e4, combiné Sûr : e1..e4) ; e5 : pari absent
    de 1xBet ; e6 : match introuvable."""
    picks = [pick(i) for i in range(1, 7)]
    return picks, {f"e{i}": {("h2h", "HOME", None): XB} for i in range(1, 5)} | {"e5": {("btts", "Yes", None): 1.8}}


def test_unnoted_morning_bets_stay_tracked():
    """Les coupons du matin sont suivis même sans être notés : réglés, dans les statistiques gagné / perdu du bilan et des résultats, dans « Mes
    paris » avec leur code et leur bouton « Vérifier », mis à jour et surveillés par les alertes ⚠️. Seul l'ARGENT ne compte que les paris notés."""
    reset()
    money.set_daily(5000)
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    legs = [prono(i) for i in range(4)]
    with Fakes(code="MORN1") as f:
        msgs, sent_picks, sent_combos = app.build_day(picks, DAY, odds_time="09:30")
        fair.record_combos([(5, (legs, 1.55 ** 4, 0.72 ** 4))], DAY, {5: 500}, FCFA, coupons={5: "FAIRC"}, at="04:12")
    track.record(picks, {c["key"] for c in sent_picks}, DAY, at(4), single_key=app.single_key(sent_picks))
    settle.record_combos(sent_combos, DAY)
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": pronos(7), "picks_at": "09:30", "xbet_at": "09:30"})
    real = store.load("bets.json", {})["combos"][0]
    assert real["tier"] == SUR and real["xlegs"] and real["cur"] == FCFA and real["stake"] > 0 and "played" not in real, real
    assert all("played" not in x for x in track.load() + fair.load_combos()), "rien n'est noté"
    # « Mes paris » : les trois paris non notés, avec leur code coupon et leur bouton « Vérifier ce combiné »
    todo = played.todo(DAY)
    text = str(todo)
    assert "Sûr · cote 1xBet 5,06 (09:30)" in text and "Pari du jour · cote 1xBet 1,50 (09:30)" in text and "Aux cotes 1xBet (cote 5,8)" in text, text
    assert text.count("🎟️ <code>MORN1</code>") == 2 and "🎟️ <code>FAIRC</code>" in text, text
    assert {f"xmajr|sur|{DAY}", f"xmajr|pari|{DAY}", f"xmaj|5|{DAY}"} <= set(buttons(todo)), buttons(todo)
    assert all(not b.startswith("annule|") for b in buttons(todo)), "aucun n'est noté"
    # ⚠️ les alertes veillent sur ces paris, notés ou non (le pari du jour e4 et le combiné Sûr perdent un pari chez 1xBet)
    today = store.load("today.json", {})
    targets = xalerts.real_targets(today, track.load(), store.load("bets.json", {})["combos"], DAY, at(13))
    assert {t[2] for t in targets} == {"pari", "sur"}, [t[:3] for t in targets]

    class Gone:
        def get(self, c):
            return ("gone", None, None, None) if c["id"] == "e4" else ("ok", c["xodds"], c["xref"], "13:00")
    items = xalerts.danger_items(targets, Gone(), DAY)
    assert {it["key"].split("|")[1] for it in items} == {"pari", "sur"} and all(it["type"] == "danger" for it in items)
    assert xalerts.took_bets(track.load(), store.load("bets.json", {})["combos"], DAY) == ({}, set()), "mais personne n'a « joué » : ni ✅ ni 💰"
    # le combiné du matin est renvoyé à jour (nouveau code) même sans être noté
    px = {f"m{i}": ({} if i == 1 else {("totals", "Under", 3.5): 1.55}) for i in range(7)}
    with Fakes(code="NEWC2") as f:
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fake_fetch({k: v for k, v in px.items()}))
    assert any("COMBINÉ À JOUR 13:00" in str(m) and "Code : <code>NEWC2</code>" in str(m) for m in f.sent), f.sent
    # règlement le lendemain : les paris du matin sont réglés (1-0 / 2-0 partout : l'équipe à domicile gagne)
    after = at(8, day=2)
    home_wins = {f"e{i}": {"scores": [{"name": f"Home{i}", "score": "2"}, {"name": f"Away{i}", "score": "0"}]} for i in range(1, 7)}
    sbs = {"soccer_spain_la_liga": [{"id": k, "completed": True} | v for k, v in home_wins.items()]}
    bets, settled = settle.settle_pending(after, scores_by_sport=sbs)
    assert bets["combos"][0]["result"] == "win" and all(r["result"] == "win" for r in track.load() if r["sent"]), bets
    with Scores({}):
        msgs2 = app.fair_combos_report(after)
    assert fair.load_combos()[0]["result"] == "win"
    # résultats du matin : réglés et annoncés (gagné), SANS aucun montant (rien n'est noté)
    res = str(app.results_message(bets, track.load(), settled))
    assert "✅ SÛR gagné" in res and "✅ Pari du jour" in res and "Hier : 1 combiné gagné sur 1" in res and "FCFA" not in res, res
    rep = str(msgs2[0])
    assert "✅ Combiné aux cotes 1xBet (cote 5,77) gagné</b>" in rep, rep
    assert "FCFA" not in rep and "Combinés à vérifier chez 1xBet : 1 gagné sur 1 (le résultat en argent ne compte que ceux que tu as notés)." in rep, rep
    # bilan : les statistiques gagné / perdu comptent ces paris ; le résultat en argent (FCFA) NON
    simple = str(settle.bilan(bets))
    assert "🎯 <b>Combinés</b> : 1 gagné sur 1" in simple and "SÛR : 1 sur 1" in simple, simple
    assert "✅ <b>Paris envoyés</b>" in simple and "Combinés à vérifier chez 1xBet : 1 gagné sur 1" in simple, simple
    assert "FCFA" not in simple and "Tes paris notés" not in simple and "Joués chez 1xBet" not in simple, simple
    det = settle.bilan(bets, detail=True)
    assert "SÛR : 1/1" in det and "Combinés à vérifier chez 1xBet : 1 gagné sur 1" in det and "FCFA" not in det, det
    assert money_result() == (0, 0, 0) and played.summary_lines(bets, track.load(), fair.load_combos()) == []
    assert money.summary_lines(bets, track.load(), fair.load_combos(), today="2030-01-02") == []
    cmd = str(app.handle_command("/bilan"))
    assert "1 gagné sur 1" in cmd and "Tes paris notés" not in cmd, cmd
    # NOTÉ ensuite : le même pari entre alors dans l'argent (cote et mise de 1xBet), et lui seul
    bets["combos"][0]["played"], bets["combos"][0]["cur"] = {"odds": 5.0, "stake": 1000}, FCFA
    store.save("bets.json", bets)
    n, total, staked = money_result()
    assert n == 1 and staked == 1000 and abs(total - 1000 * (5.0 * (1.5 ** 4) / real["odds"] - 1)) < 1e-6, (n, total, staked)
    simple = str(settle.bilan(bets))
    assert "Tes paris notés" in simple and "sur 1 pari (1 gagné)" in simple and "🎯 <b>Combinés</b> : 1 gagné sur 1" in simple
    # un combi sur mesure suivi mais pas encore noté (la mise n'a pas été donnée) : surveillé par les alertes ⚠️ comme tout ce qui est suivi, mais pas « joué »
    reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos(7)})
    with Fakes():
        app.custom_combo([], pronos(), 5.0, DAY)
        chat.on_callback(f"cjoue|m1|{DAY}", 61)
    assert "played" not in fair.load_combos()[0]
    targets = xalerts.real_targets(store.load("today.json", {}), [], [], DAY, at(13))
    assert [t[0] for t in targets] == ["Le combi sur mesure (cote 5)"], targets
    assert xalerts.took_bets([], [], DAY) == ({}, set()), "pas noté : ni « tu l'as joué » ni « joue maintenant » écarté"
    print("ok  9 coupons du matin non notés : réglés, annoncés, dans les stats gagné / perdu, dans Mes paris (code + Vérifier), renvoyés et surveillés ; absents de l'argent")


# ---------- 10. cote 1xBet connue : jamais demandée ----------

def test_known_odds_never_asked():
    """Une cote 1xBet CONNUE (combiné ou pari du jour chiffré par 1xBet, version en cours) n'est jamais demandée par « ✅ Je l'ai joué » : avec
    une mise conseillée il est noté tout de suite à cette cote, sans mise conseillée seule la mise est demandée (un montant seul est la mise, « cote
    mise » corrige la cote). Seuls les paris SANS cote 1xBet (cote mini, cote de référence) demandent la cote. « ✏️ Autre cote » demande la cote."""
    reset()
    money.set_daily(5000)
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    legs = [prono(i) for i in range(4)]
    with Fakes(code="KN01") as f:
        msgs, sent_picks, sent_combos = app.build_day(picks, DAY, odds_time="09:30")
        fair.record_combos([(5, (legs, 1.55 ** 4, 0.72 ** 4))], DAY, {5: 500}, FCFA, coupons={5: "FAIRC"}, at="04:12")
    track.record(picks, {c["key"] for c in sent_picks}, DAY, at(4), single_key=app.single_key(sent_picks))
    settle.record_combos(sent_combos, DAY)
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": pronos(7), "picks_at": "09:30"})
    # 1. mise conseillée + cote 1xBet connue : un appui = noté, à la cote 1xBet, sans question
    for what, odds_txt, stake in (("sur", "5,06", None), ("pari", "1,50", None), ("v5", "5,77", 500)):
        with Fakes() as f:
            reply = chat.on_callback(f"joue|{what}|{DAY}", 90)
        t = played.find(what, DAY)
        assert reply.startswith("✅ Noté") and f"joué à {odds_txt} chez 1xBet" in reply and "À quelle cote" not in reply, (what, reply)
        assert chat.get_pending() is None and t.item["played"]["odds"] == float(odds_txt.replace(",", ".")) and t.item["cur"] == FCFA
        assert t.item["played"]["stake"] == (stake or t.stake) > 0
        assert f.edits[-1][0] == 90 and f.last_texts()[0].startswith("✅ Joué à") and f.last_texts()[1:3] == ["✏️ Autre cote", "↩️ Annuler"]
        assert "Note annulée" in chat.on_callback(f"annule|{what}|{DAY}", 90)
    # 2. cote connue, pas de mise conseillée (pas de budget) : seule la mise est demandée
    store.save(money.FILE, {})
    rows = store.load("bets.json", {})
    rows["combos"][0]["stake"] = None
    rows["combos"][0].pop("cur", None)
    store.save("bets.json", rows)
    with Fakes():
        ask = chat.on_callback(f"joue|sur|{DAY}", 91)
    assert ask.startswith("Avec quelle mise en FCFA ?") and "À quelle cote" not in ask and "cote 1xBet 5,06" in ask, ask
    assert chat.get_pending() == chat.get_pending() | {"what": "sur", "odds": 5.06, "msg": 91}
    for txt, odds_, stake in (("1000", "5,06", 1000), ("2 000", "5,06", 2000), ("1.500", "5,06", 1500), ("2500 FCFA", "5,06", 2500)):
        chat.set_pending("sur", DAY, 91, odds=5.06)
        with Fakes():
            out = chat.route(txt)[1]
        p = played.find("sur", DAY).item["played"]
        assert out.startswith("✅ Noté") and f"joué à {odds_} chez 1xBet" in out and p["stake"] == stake and p["odds"] == 5.06, (txt, out, p)
        chat.on_callback(f"annule|sur|{DAY}", 91)
    chat.set_pending("sur", DAY, 91, odds=5.06)                    # « cote mise » corrige la cote ; la garde « 2 000 = cote ou mise ? » ne joue pas ici
    with Fakes():
        out = chat.route("5,10 1 000")[1]
    assert "joué à 5,10 chez 1xBet, mise 1 000 FCFA" in out, out
    chat.on_callback(f"annule|sur|{DAY}", 91)
    # 3. le pari du jour typé avec /joue : la cote tapée est retenue, seule la mise manque
    reply = app.handle_command("/joue pari 1,45")
    assert reply == "Avec quelle mise en FCFA ? Réponds ta mise, par exemple 1 000 (cote 1,45 retenue).", reply
    assert chat.get_pending()["odds"] == 1.45 and "joué à 1,45 chez 1xBet, mise 800 FCFA" in chat.route("800")[1]
    # 4. « ✏️ Autre cote » (pari noté) demande la cote ; la mise notée reste, un « cote mise » la change
    with Fakes():
        assert chat.on_callback(f"joue|pari|{DAY}", 92).startswith("À quelle cote as-tu finalement joué")
        assert "mise 800 FCFA" in chat.route("1,50")[1]
        chat.on_callback(f"joue|pari|{DAY}", 92)
        assert "joué à 1,52 chez 1xBet, mise 300 FCFA" in chat.route("1,52 300")[1]
    # 5. cote de référence d'un autre bookmaker / cote mini : pas de cote 1xBet connue, la cote est demandée
    reset()
    money.set_daily(5000)
    store.save("bets.json", {"picks": [], "combos": [combo(SUR, 5.0, stake=1000, cur=FCFA)]})
    t = played.find("sur", DAY)
    assert not t.known
    with Fakes():
        ask = chat.on_callback(f"joue|sur|{DAY}", 93)
        assert ask.startswith("À quelle cote as-tu joué") and "ta mise sera de 1 000 FCFA" in ask and "odds" not in chat.get_pending(), ask
        assert chat.route("2 000")[1].startswith("Je n'ai pas compris la cote")            # ici la garde d'ambiguïté s'applique
        assert "joué à 5,40 chez 1xBet, mise 1 000 FCFA" in chat.route("5,40")[1]
    print("ok  10 cote 1xBet connue jamais demandée (noté tout de suite avec une mise conseillée, sinon seule la mise ; montant seul = mise ; « cote mise » "
          "corrige) ; cote de référence / mini : la cote est demandée ; « Autre cote » la demande")


# ---------- 11. codes coupon : <code> à copier d'un appui et bouton « 📋 Copier le code » ----------

def copies(msg):
    """Boutons « copier » (copy_text) d'un message ou d'un markup."""
    markup = msg if isinstance(msg, dict) else (getattr(msg, "markup", None) or {"inline_keyboard": []})
    return [b for row in markup.get("inline_keyboard", []) for b in row if "copy_text" in b]


def test_coupon_code_and_copy_button():
    # 1. <code> partout, échappé ; bouton copy_text sans toucher aux anciens appelants
    assert fmt.coupon_block("AB<1>&") == "\n\n🎟️ Code : <code>AB&lt;1&gt;&amp;</code> <i>(1xBet › Ticket › Charger le coupon)</i>"
    assert "<b>" not in fmt.coupon_block("K7Q2X")                 # Telegram ne mélange pas gras et <code>
    assert telegram.inline([[("x", {"copy_text": "C1"}), ("y", "cb"), ("z", None)]]) == {"inline_keyboard": [[
        {"text": "x", "copy_text": {"text": "C1"}}, {"text": "y", "callback_data": "cb"}, {"text": "z", "callback_data": "noop"}]]}
    assert telegram.copy_row("K7Q2X") == [[{"text": "📋 Copier le code K7Q2X", "copy_text": {"text": "K7Q2X"}}]] and telegram.copy_row(None) == []
    # 2. le matin : le pari du jour, le combiné réel et le combiné à cote juste portent leur code en <code> et UN bouton « copier » (dernière ligne)
    reset()
    money.set_daily(5000)
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    legs = [prono(i) for i in range(4)]
    codes = iter(["CODE-PARI", "CODE-SUR", "CODE-FAIR"])
    with Fakes() as f:
        xbet.save_coupon = lambda ls: next(codes)
        msgs, sent_picks, sent_combos = app.build_day(picks, DAY, odds_time="09:30", fair_combos=[(5, (legs, 1.55 ** 4, 0.72 ** 4))],
                                                      fair_coupons={})
    by = {code: next(m for m in msgs if f"<code>{code}</code>" in m) for code in ("CODE-PARI", "CODE-SUR", "CODE-FAIR")}
    assert "PARI DU JOUR" in by["CODE-PARI"] and "SÛR" in by["CODE-SUR"] and "<b>💎 COMBINÉ — cote " in by["CODE-FAIR"]
    for code, m in by.items():
        cp = copies(m)
        assert len(cp) == 1 and cp[0] == {"text": f"📋 Copier le code {code}", "copy_text": {"text": code}}, (code, cp)
        assert m.markup["inline_keyboard"][-1] == [cp[0]], "ligne à part, en dernier"
        assert str(m).count("🎟️ Code : <code>") == 1 and f"<b>{code}</b>" not in m
    assert [b["callback_data"] for row in by["CODE-SUR"].markup["inline_keyboard"] for b in row if "callback_data" in b] == [f"joue|sur|{DAY}", f"xmajr|sur|{DAY}"]
    # sans code (combiné à cote mini, ou enregistrement du ticket en échec) : aucun bouton « copier », aucun <code>
    with Fakes(code=None):
        msgs2, _, _ = app.build_day(picks, DAY, odds_time="09:30", fair_combos=[(5, ([unpriced(i) for i in range(4)], 5.0, 0.2))])
    assert all(not copies(m) for m in msgs2) and all("<code>" not in m for m in msgs2)
    # 3. quand les boutons du message sont remplacés (noté, annulé, ancien « pas joué »), la ligne « copier » reste
    track.record(picks, {c["key"] for c in sent_picks}, DAY, at(4), single_key=app.single_key(sent_picks))
    settle.record_combos(sent_combos, DAY)
    fair.record_combos([(5, (legs, 1.55 ** 4, 0.72 ** 4))], DAY, {5: 500}, FCFA, coupons={5: "CODE-FAIR"}, at="09:30")
    bets = store.load("bets.json", {})
    bets["combos"][0]["coupon"] = "CODE-SUR"
    store.save("bets.json", bets)
    rows = track.load()
    next(r for r in rows if r.get("single"))["coupon"] = "CODE-PARI"
    track.save(rows)
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": pronos(7), "picks_at": "09:30"})
    for what, code in (("sur", "CODE-SUR"), ("pari", "CODE-PARI"), ("v5", "CODE-FAIR")):
        with Fakes() as f:
            chat.on_callback(f"joue|{what}|{DAY}", 70)                       # noté tout de suite
            assert copies(f.edits[-1][1]) == [{"text": f"📋 Copier le code {code}", "copy_text": {"text": code}}], (what, f.edits[-1][1])
            chat.on_callback(f"joue|{what}|{DAY}", 70)                       # « Autre cote » : la question
            chat.route("5,40")
            assert len(copies(f.edits[-1][1])) == 1 and f.edits[-1][1]["inline_keyboard"][-1][0]["copy_text"]["text"] == code
            chat.on_callback(f"annule|{what}|{DAY}", 70)
            assert len(copies(f.edits[-1][1])) == 1 and f.last_texts()[0].startswith("✅ Je l'ai joué")
            chat.on_callback(f"skip|{what}|{DAY}", 70)
            assert len(copies(f.edits[-1][1])) == 1, f.edits[-1][1]
            chat.on_callback(f"unskip|{what}|{DAY}", 70)
            assert len(copies(f.edits[-1][1])) == 1
    # 4. « Mes paris » : plusieurs codes -> <code> seulement (pas de bouton « copier »), message HTML correctement échappé
    todo = played.todo(DAY)
    assert isinstance(todo, telegram.Html) and "<" not in re.sub(r"</?(b|i|code)>", "", str(todo)), todo      # texte échappé : seules les balises HTML voulues
    for code in ("CODE-PARI", "CODE-SUR", "CODE-FAIR"):
        assert f"🎟️ <code>{code}</code>" in todo
    assert not copies(todo) and fmt.plain(todo).count("🎟️ CODE-") == 3 and "<code>" not in fmt.plain(todo)
    print("ok  11 codes coupon en <code> (échappés), « 📋 Copier le code » sur les messages à un seul code, gardé après un changement de boutons, absent sans code ni dans Mes paris")


def test_copy_button_on_refresh_alerts_and_custom():
    # vérifier / mettre à jour : le code du nouveau ticket est en <code> avec son bouton « copier »
    reset()
    money.set_daily(5000)
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    with Fakes(code="MORN2"):
        msgs, sent_picks, sent_combos = app.build_day(picks, DAY, odds_time="09:30")
    track.record(picks, {c["key"] for c in sent_picks}, DAY, at(4), single_key=app.single_key(sent_picks))
    settle.record_combos(sent_combos, DAY)
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": [], "picks_at": "09:30"})
    now_px = {f"e{i}": {("h2h", "HOME", None): 1.45} for i in range(1, 5)}
    with Fakes(code="VER-SUR") as f:
        out = app.refresh_real("sur", DAY, now=at(13), fetch=fake_fetch(now_px))
    assert "Code : <code>VER-SUR</code>" in out and copies(out) == [{"text": "📋 Copier le code VER-SUR", "copy_text": {"text": "VER-SUR"}}], out.markup
    assert [b["callback_data"] for row in out.markup["inline_keyboard"] for b in row if "callback_data" in b] == [f"joue|sur|{DAY}", f"xmajr|sur|{DAY}"]
    with Fakes(code="VER-PARI") as f:
        out = app.refresh_real("pari", DAY, now=at(13, 5), fetch=fake_fetch({"e4": {("h2h", "HOME", None): 1.45}}))
    assert "<code>VER-PARI</code>" in out and len(copies(out)) == 1
    # un pari retiré par 1xBet : plus de code, plus de bouton « copier »
    with Fakes(code="NOPE") as f:
        out = app.refresh_real("sur", DAY, now=at(13, 10), fetch=fake_fetch(now_px | {"e2": {("btts", "Yes", None): 1.8}}))
    assert "plus jouable tel quel" in out and "<code>" not in out and not copies(out) and not f.coupons
    # code coupon en échec : message sans bouton « copier »
    with Fakes(code=None):
        out = app.refresh_real("sur", DAY, now=at(13, 15), fetch=fake_fetch(now_px))
    assert not copies(out) and "<code>" not in out and "🎟️ Code indisponible pour l'instant" in out
    # combiné à cote juste renvoyé / vérifié
    reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos(7)})
    fair.record_combos([(5, ([prono(i) for i in range(4)], 1.55 ** 4, 0.72 ** 4))], DAY, coupons={5: "OLD"}, at="04:12")
    px = {f"m{i}": ({} if i == 1 else {("totals", "Under", 3.5): 1.55}) for i in range(7)}
    with Fakes(code="UPD-1") as f:
        app.run_xbet_refresh(now=at(13), announce=False, fetch=fake_fetch(px))
    upd = next(m for m in f.sent if "COMBINÉ À JOUR" in str(m))
    assert "<code>UPD-1</code>" in upd and copies(upd) == [{"text": "📋 Copier le code UPD-1", "copy_text": {"text": "UPD-1"}}], upd.markup
    # alertes : un seul code dans le message -> « copier » ; deux codes -> aucun bouton « copier » (<code> à toucher)
    reset()
    one = [xalerts._item("rise", "k1", 1.8, "ALERTE 1", coupon={"xref": [9001, 1, 0], "xodds": 1.8}),
           xalerts._item("danger", "k2", 0.0, "ALERTE 2", buttons=[("🎟️ Vérifier ce combiné", f"xmaj|5|{DAY}")])]
    with Fakes(code="AL-1") as f:
        msg = xalerts.send(one, DAY, at(13))
    assert str(msg).count("<code>AL-1</code>") == 1 and copies(msg) == [{"text": "📋 Copier le code AL-1", "copy_text": {"text": "AL-1"}}]
    assert msg.markup["inline_keyboard"][0] == [{"text": "🎟️ Vérifier ce combiné", "callback_data": f"xmaj|5|{DAY}"}] and msg.markup["inline_keyboard"][-1][0]["copy_text"]
    reset()
    two = [xalerts._item("rise", f"k{i}", 1.8, f"ALERTE {i}", coupon={"xref": [9000 + i, 1, 0], "xodds": 1.8}) for i in (1, 2)]
    codes = iter(["AL-A", "AL-B"])
    with Fakes() as f:
        xbet.save_coupon = lambda ls: next(codes)
        msg = xalerts.send(two, DAY, at(13))
    assert "<code>AL-A</code>" in msg and "<code>AL-B</code>" in msg and not copies(msg)
    reset()
    none = [xalerts._item("good", "k9", 1.8, "ALERTE ✅")]
    with Fakes():
        msg = xalerts.send(none, DAY, at(13))
    assert "<code>" not in msg and not copies(msg) and getattr(msg, "markup", None) is None
    # combi sur mesure : « ✅ Je le joue » + « 📋 Copier le code », la ligne « copier » reste après l'appui
    reset()
    money.set_daily(5000)
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos()})
    with Fakes(code="CU-1") as f:
        msg = app.custom_combo([], pronos(), 20.0, DAY)
    assert "<code>CU-1</code>" in msg and [b["text"] for row in msg.markup["inline_keyboard"] for b in row] == ["✅ Je le joue", "🔀 Un autre sans mes matchs", "📋 Copier le code CU-1"]
    with Fakes() as f:
        chat.on_callback(f"cjoue|m1|{DAY}", 60)
    assert f.edits[-1][0] == 60 and copies(f.edits[-1][1]) == [{"text": "📋 Copier le code CU-1", "copy_text": {"text": "CU-1"}}]
    # pas de code (1xBet muet : cotes du dernier relevé) : pas de bouton « copier »
    reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos()})
    with Fakes(code=None):
        msg = app.custom_combo([], pronos(), 5.0, DAY)
    assert not copies(msg) and "<code>" not in msg
    print("ok  11 codes coupon : <code> + « 📋 Copier le code » sur les réponses « Vérifier », les combinés renvoyés, les alertes à un seul code et les combis sur mesure ; rien sans code")


def main():
    test_default_stake_with_budget()
    test_default_stake_without_budget()
    test_only_noted_bets_count()
    test_morning_results_only_noted()
    test_buttons()
    test_custom_is_a_note()
    test_no_units_anywhere()
    test_no_units_refresh_and_alerts()
    test_migration()
    test_names_in_todo()
    test_upgrade_to_xbet_prices()
    test_upgrade_through_refresh()
    test_single_priced_at_refresh()
    test_single_stays_silent()
    test_unnoted_morning_bets_stay_tracked()
    test_known_odds_never_asked()
    test_coupon_code_and_copy_button()
    test_copy_button_on_refresh_alerts_and_custom()
    print("tests notes en FCFA : OK")


if __name__ == "__main__":
    main()
