"""Tout ce que Kelly montre est au prix de 1xBet, avec l'heure de sa lecture (audit de l'interface) : /combi sans prix typique quand 1xBet
a des cotes, gains des combinés aux cotes 1xBet, /top /match /analyse, « Mes paris » (cote, code coupon et bouton « Vérifier » de la
version en cours), vérification des combinés réels et du pari du jour, alertes, résultats du matin, note « joué » d'un combiné renvoyé,
libellés, heures de relevé, code coupon indisponible, double chance. Hors ligne.
Lancer : python -m tests.test_xbet_ui"""
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-xui-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-xui-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-xui-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ["XBET_PROXY_FILE"] = os.path.join(os.environ["DATA_DIR"], "pas-de-fichier.env")      # jamais le vrai proxy de la machine
os.environ["XBET_PROXY"] = "http://user:secret@proxy.test:80"

import inspect  # noqa: E402

from kelly import app, bankroll, calib, chat, combos as CB, fair, fmt, money, monde, played, settle, store, telegram, track, xprice  # noqa: E402
from kelly.sources import http, state, xbet  # noqa: E402

DAY = "2030-01-01"
KO = f"{DAY}T19:00:00Z"
TYP, XB, PROB = 1.6, 1.5, 0.7                       # cote typique des books, cote 1xBet, proba de consensus


def reset():
    """Données du jour vides, fiabilité neutre, pas de budget."""
    for f in ("bets.json", "tracked.json", "fair.json", "pending.json"):
        store.save(f, [] if f in ("tracked.json", "fair.json") else ({"picks": [], "combos": []} if f == "bets.json" else {}))
    for f in (fair.COMBOS_FILE,):
        store.save(f, [])
    for f in ("today.json", app.COMBO_UPDATES, money.FILE, calib.FILE):
        store.save(f, {})
    calib._memo.clear()


def pick(n, market="h2h", name=None, point=None, **kw):
    """Pari réel (The Odds API) du match e<n> à la cote typique TYP."""
    home, away = f"Home{n}", f"Away{n}"
    name = name or home
    c = {"key": f"e{n}|{market}||{point if point is not None else ''}|{name}", "id": f"e{n}", "sport_key": "soccer_spain_la_liga",
         "group": "soccer", "event": f"{home} - {away}", "home": home, "away": away, "league": "Liga", "start": "19:00",
         "commence": KO, "market": market, "who": None, "point": point, "name": name,
         "pick": fmt.label(market, name, point, None, "soccer"), "prob": PROB, "prob_book": PROB, "prob_src": "consensus",
         "n_sharp": 1, "odds": TYP, "best_odds": 1.8, "book": "Coolbet", "edge": PROB * TYP - 1, "spread": 0.02, "steam": 0.0,
         "n_books": 10, "weight": 1.0, "score": 0.12 + n / 100, "why": "cote plus haute qu'ailleurs (1,60 contre 1,43 en moyenne)"}
    return c | kw


def prono(i, **kw):
    """Pronostic 💎 : moins de 3,5 buts à 1,55 chez 1xBet, match m<i>."""
    c = {"id": f"m{i}", "sport_key": "soccer_epl", "home": f"H{i}", "away": f"A{i}", "commence": KO, "start": "19:00", "league": "L",
         "event": f"H{i} - A{i}", "market": "totals", "name": "Under", "point": 3.5, "who": None, "prob": 0.72, "p0": 0.72,
         "min_odds": 1.46, "conf": 0.6, "n_min": 8, "score": 0.2 - i / 1000, "key": f"m{i}|totals||3.5|Under",
         "pick": fmt.label("totals", "Under", 3.5, None, "soccer"), "xodds": 1.55, "xev": 0.12, "xgid": 900 + i,
         "xref": [900 + i, 10, 3.5],
         "psh": {"under|3.5": [0.70, "pinnacle", 1]}}      # Pinnacle voit 70 % : sans avis du marché, l'ensemble (buts) ne validerait jamais ce pari
    return c | kw


def fake_fetch(priced, calls=None):
    """1xBet vu par les tests : `priced` = {id Kelly: {(marché, côté, ligne): cote}} ; les autres matchs sont introuvables. « HOME » = l'équipe
    à domicile ; les noms sont sinon pris tels quels."""
    def fetch(events, deadline=None, wanted=None, live=False, fresh_min=None, max_matches=None):
        if calls is not None:
            calls.append(events)
        out = {}
        for k, ev in events:
            px = priced.get(ev["id"])
            if px is not None:
                gid = 9000 + int(ev["id"][1:])
                px = {(m, ev["home_team"] if n == "HOME" else n, p): v for (m, n, p), v in px.items()}
                out[ev["id"]] = {"gid": gid, "px": px, "refs": {k_: [gid, 1 + i, 0] for i, k_ in enumerate(px)}}
        return out
    return fetch


def world(n=6, extra=()):
    """n matchs de 1X2 à la cote typique 1,60 ; 1xBet paie 1,50 sur les 4 premiers ; e5 : match trouvé mais pari absent ; e6 : introuvable."""
    picks = [pick(i) for i in range(1, n + 1)] + list(extra)
    priced = {f"e{i}": {("h2h", "HOME", None): XB} for i in range(1, 5)} | {"e5": {("btts", "Yes", None): 1.8}}
    return picks, priced


class Fakes:
    """Envoi capturé, code coupon simulé (`code`, None = échec), vérification en direct des pronostics simulée : aucun réseau."""

    def __init__(self, code="K7Q2X", drop=(), got=True):
        self.sent, self.coupons, self.code, self.drop, self.got = [], [], code, drop, got

    def __enter__(self):
        self.old = (xbet.save_coupon, telegram.send, xprice.live_check)
        real_check = xprice.live_check
        xbet.save_coupon = lambda ls: (self.coupons.append([c["id"] for c in ls]), self.code)[1]
        telegram.send = lambda text, *a, **k: (self.sent.append(text), True)[1]
        px = {f"m{i}": {"gid": 900 + i, "px": {} if f"m{i}" in self.drop else {("totals", "Under", 3.5): 1.55},
                        "refs": {} if f"m{i}" in self.drop else {("totals", "Under", 3.5): [900 + i, 10, 3.5]}} for i in range(7)}
        xprice.live_check = lambda ids, now=None, fetch=None, fresh_min=None: real_check(
            ids, now, fetch=lambda ev, deadline=None, wanted=None, live=True, fresh_min=None:
            {k: v for k, v in px.items() if k in {e["id"] for _, e in ev}} if self.got else {})
        return self

    def __exit__(self, *a):
        xbet.save_coupon, telegram.send, xprice.live_check = self.old


def buttons(msg):
    return [b["callback_data"] for row in msg.markup["inline_keyboard"] for b in row if "callback_data" in b]


def at(h, m=0):
    return datetime(2030, 1, 1, h, m, tzinfo=timezone.utc)


def morning(odds_time="09:30"):
    """Le matin complet sur des paris réels remis à la cote 1xBet : messages, suivi, today.json. -> (messages, picks)."""
    reset()
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    codes = iter(f"CODE{n}" for n in range(1, 20))
    with Fakes() as f:
        xbet.save_coupon = lambda legs: next(codes)
        msgs, sent_picks, sent_combos = app.build_day(picks, DAY, odds_time=odds_time)
    track.record(picks, {c["key"] for c in sent_picks}, DAY, at(4), single_key=app.single_key(sent_picks))
    settle.record_combos(sent_combos, DAY)
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": [], "picks_at": odds_time})
    return msgs, picks


# ---------- E1 : /combi ----------

def test_combi_fallthrough():
    reset()
    picks, priced = world(extra=[pick(7, odds=3.3, edge=PROB * 3.3 - 1), pick(8, odds=9.1, edge=PROB * 9.1 - 1)])
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    assert picks[6].get("xmiss") and picks[7].get("xmiss"), "paris sans cote 1xBet"
    pronos = [prono(i) for i in range(7)]                                   # 1,55 ^ 7 = 21,5 : la cote 30 est hors de portée
    assert CB.fair_custom(pronos, 30) is None
    with Fakes() as f:
        res = app.custom_combo(picks, pronos, 30.0, DAY)
    out = str(res)
    assert out.startswith("❌ Pas de combiné à la cote 30.\n📉 Avec les bons paris du jour, la cote monte au plus à 21,5 (7 matchs)."), out
    assert "SUR MESURE" not in out and "cote de référence" not in out and "3,3" not in out and "9,1" not in out, out
    cbs = [b["callback_data"] for row in res.markup["inline_keyboard"] for b in row]
    assert cbs[0] == "cgo|21,4||" and res.markup["inline_keyboard"][0][0]["text"] == "🎯 Cote 21,4", cbs      # la plus haute cote atteignable aux cotes 1xBet
    assert not f.coupons
    # aucune cote 1xBet : combiné à cote mini (à vérifier) AVANT les cotes de référence des autres books
    reset()
    typ = [pick(i) for i in range(1, 5)]
    flat = [prono(i, xodds=None, xref=None, xev=None, xgid=None) for i in range(7)]
    with Fakes() as f:
        out = str(app.custom_combo(typ, flat, 4.5, DAY))
    assert out.startswith("Kelly n'a vu aucune cote de 1xBet aujourd'hui") and "À VÉRIFIER CHEZ 1XBET" in out and "SUR MESURE" not in out, out
    assert "Mise indicative" not in out and "Mise : règle ton budget du jour avec « 💰 Mon budget »" in out, out    # sans budget : jamais d'unités
    # budget du jour : la mise du combiné demandé est prise dessus
    money.set_daily(5000)
    with Fakes():
        out = str(app.custom_combo(typ, flat, 4.5, DAY))
    assert "sur ton budget du jour" in out and "Mise indicative" not in out, out
    # ni 1xBet ni pronostics : cotes de référence, annoncées comme telles, sans code
    store.save(money.FILE, {})
    with Fakes() as f:
        out = str(app.custom_combo(typ, [], 6.0, DAY))
    assert "SUR MESURE" in out and "cotes de référence (pas celles de 1xBet) : vérifie chaque cote chez 1xBet" in out, out
    assert "🎟️ Code" not in out and not f.coupons
    # paris réels chiffrés par 1xBet : leur prix 1xBet et leur code, jamais le prix typique
    reset()
    xp, pr = world()
    xprice.price_picks(xp, fetch=fake_fetch(pr))
    with Fakes() as f:
        out = str(app.custom_combo(xp, [], 5.0, DAY, when="09:30"))
    assert "SUR MESURE" in out and "Code : <code>K7Q2X</code>" in out and "Cotes 1xBet de 09:30" in out, out
    assert "cotes de référence" not in out and "Home5" not in out and "Home6" not in out
    print("ok  E1/E10 /combi : jamais de prix typique quand 1xBet a des cotes, cotes de référence annoncées, mise sur le budget")


# ---------- E2 : gains des combinés aux cotes 1xBet ----------

def fair_row(xodds=2.0, mins=(1.6, 1.55, 1.55), results=("win", "win", "win"), **kw):
    legs = [{"key": f"k{i}", "min_odds": m, "xodds": xodds, "odds": xodds, "result": r, "home": f"H{i}", "away": f"A{i}", "market": "totals",
             "name": "Under", "pick": "Moins de 3,5 buts"} for i, (m, r) in enumerate(zip(mins, results))]
    total = round(xodds ** 3, 2) if xodds else round(mins[0] * mins[1] * mins[2], 2)
    if not xodds:
        for leg in legs:
            leg.pop("xodds"), leg.pop("odds")
    return {"date": DAY, "tier": 5, "min_odds": total, "prob": 0.2, "keys": [l["key"] for l in legs], "legs": legs,
            "result": "win", "stake": 1000, "cur": "FCFA"} | kw


def test_real_combo_gain():
    row = fair_row()
    assert played.is_real(row) and row["min_odds"] == 8.0
    assert money.gain(row) is None, "mise conseillée seule (stake + cur) : pas noté, pas compté"
    row["played"] = {"odds": 8.0, "stake": 1000}                          # noté au total des cotes 1xBet
    assert money.gain(row) == 7000, money.gain(row)                       # 1 000 x (2 x 2 x 2 - 1), pas à la cote mini 1,6 x 1,55 x 1,55
    # noté à 7,5 chez 1xBet : toutes les jambes gagnées, le ratio vaut 1
    row["played"] = {"odds": 7.5, "stake": 1000, "at": "x"}
    assert money.gain(row) == 6500 and bankroll.played_gain(row) == 6500
    # une jambe remboursée : la cote 1xBet baisse dans la même proportion (4 / 8)
    half = fair_row(results=("win", "win", "void"), played={"odds": 8.0, "stake": 1000})
    assert bankroll.played_gain(half) == 1000 * (8.0 * 4 / 8 - 1) and money.gain(half) == 1000 * (8.0 * 4 / 8 - 1)
    # combiné à vérifier (cotes mini) : inchangé
    mini = fair_row(xodds=None)
    assert not played.is_real(mini) and money.gain(mini) is None
    mini["played"] = {"odds": mini["min_odds"], "stake": 1000}
    assert abs(money.gain(mini) - 1000 * (1.6 * 1.55 * 1.55 - 1)) < 1e-6
    mini["played"] = {"odds": 4.5, "stake": 1000}
    assert abs(bankroll.played_gain(mini) - 1000 * (4.5 * (1.6 * 1.55 * 1.55) / mini["min_odds"] - 1)) < 1e-6
    # bilan en unités d'un combiné de bets.json noté : à la cote notée (E12)
    assert settle._gain({"stake": 2, "result": "win", "odds": 6.0, "played": {"odds": 5.0, "stake": 4}}) == 16.0
    assert settle._gain({"stake": 2, "result": "win", "odds": 6.0}) == 10.0
    assert settle._gain({"stake": 2, "result": "loss", "odds": 6.0, "played": {"odds": 5.0, "stake": 4}}) == -4
    print("ok  E2/E12 gains à la cote 1xBet (+7 000 sur 1 000 à 8,00), cote mini seulement pour les combinés à vérifier, bilan en unités à la cote notée")


# ---------- E3 : /top, /match, /analyse ----------

def test_top_match_analyse():
    reset()
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    pronos = [prono(i) for i in range(7, 10)]
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": pronos, "xbet_at": "10:15", "picks_at": "09:30"})
    top = str(app.handle_command("/top"))
    assert "cote 1xBet <b>1,50</b>" in top and "Cotes 1xBet de 09:30" in top, top
    assert "1,60" not in top and "Home5" not in top and "Home6" not in top, "paris absents de 1xBet : pas montrés comme jouables"
    assert "💎 Moins de 3,5 buts · <b>1,55</b>" in top and "💎 = cote 1xBet de 10:15" in top, "pronostics 💎 avec les paris réels"
    # /match : prix 1xBet, ou « pas proposé chez 1xBet »
    m1 = str(app.handle_command("/match Home1"))
    assert "cote 1xBet <b>1,50</b> (09:30)" in m1 and "1,60" not in m1, m1
    m5 = str(app.handle_command("/match Home5"))
    assert "pas proposé chez 1xBet" in m5 and "1,60" not in m5 and "1,8" not in m5, m5
    m7 = str(app.handle_command("/match H7"))
    assert "💎 Moins de 3,5 buts · <b>1,55</b>" in m7 and "💎 = cote 1xBet de 10:15" in m7, m7
    a1 = str(app.handle_command("/analyse Home1"))
    assert "cote 1xBet <b>1,50</b> (09:30)" in a1, a1
    a5 = str(app.handle_command("/analyse Home5"))
    assert "pas proposé chez 1xBet" in a5 and "1,60" not in a5, a5
    # 1xBet n'a rien donné : cotes de référence, dites comme telles
    plain = [pick(i) for i in range(1, 5)]
    store.save("today.json", {"day": DAY, "picks": plain, "pronos": []})
    top = str(app.handle_command("/top"))
    assert "pas celles de 1xBet" in top and "cote de référence <b>1,60</b>" in top, top
    # le relevé de l'après-midi note l'heure des cotes ; « Nouvelles cotes 1xBet » la rappelle
    store.save("today.json", {"day": DAY, "picks": [], "pronos": [prono(i) for i in range(7)], "announced": []})
    with Fakes() as f:
        fx = lambda ev, deadline=None, wanted=None, live=False, fresh_min=None: {  # noqa: E731
            e["id"]: {"gid": 900 + int(e["id"][1:]), "px": {("totals", "Under", 3.5): 1.55},
                      "refs": {("totals", "Under", 3.5): [900 + int(e["id"][1:]), 10, 3.5]}} for _, e in ev}
        app.run_xbet_refresh(now=at(13), announce=True, fetch=fx)
    assert store.load("today.json", {})["xbet_at"] == "13:00"
    assert any("Nouvelles cotes 1xBet" in str(m) and "Cotes 1xBet de 13:00" in str(m) for m in f.sent), f.sent
    print("ok  E3/E9 /top /match /analyse : prix 1xBet + heure, 💎 avec les paris réels, paris absents de 1xBet jamais jouables")


# ---------- E4 : Mes paris et vérification des combinés réels ----------

def test_mes_paris():
    msgs, picks = morning()
    pari = next(m for m in msgs if "PARI DU JOUR" in m)
    combo = next(m for m in msgs if "SÛR" in m and "🎟️ Code : <code>" in m)
    assert f"xmajr|pari|{DAY}" in buttons(pari) and f"xmajr|sur|{DAY}" in buttons(combo), (buttons(pari), buttons(combo))
    todo = played.todo(DAY)
    text = str(todo)
    assert "Pari du jour · cote 1xBet 1,50 (09:30)" in text and "Sûr · cote 1xBet 5,06 (09:30)" in text, text
    assert "🎟️ <code>CODE1</code>" in text and "🎟️ <code>CODE2</code>" in text, text
    cb = [b["callback_data"] for row in todo.markup["inline_keyboard"] for b in row]
    assert f"xmajr|pari|{DAY}" in cb and f"xmajr|sur|{DAY}" in cb and f"joue|sur|{DAY}|l0" in cb, cb
    # « Autre cote » : l'exemple est le prix 1xBet (pas la cote typique 1,60)
    ask = chat.on_callback(f"joue|pari|{DAY}", 5)
    assert "1,50" in ask and "1,60" not in ask, ask
    assert "5,06" in chat.on_callback(f"joue|sur|{DAY}", 5)
    # combiné à cote juste aux cotes 1xBet : cote de la version en cours + code + bouton, y compris après un renvoi
    reset()
    legs = [prono(i) for i in range(4)]
    fair.record_combos([(5, (legs, 1.55 ** 4, 0.72 ** 4))], DAY, coupons={5: "FAIRC"}, at="04:12")
    store.save("today.json", {"day": DAY, "picks": [], "pronos": [prono(i) for i in range(7)]})
    todo = played.todo(DAY)
    assert "Aux cotes 1xBet (cote 5,8) · cote 1xBet 5,77 (04:12)" in str(todo) and "🎟️ <code>FAIRC</code>" in str(todo), str(todo)
    assert f"xmaj|5|{DAY}" in [b["callback_data"] for row in todo.markup["inline_keyboard"] for b in row]
    with Fakes(code="NEWC1", drop=("m1",)) as f:
        out = app.refresh_combo("5", DAY, now=at(13, 30))
    assert "Code : <code>NEWC1</code>" in out, out
    row = fair.load_combos()[0]
    assert row["coupon"] == "NEWC1" and row["at"] == "13:30" and "m1|totals||3.5|Under" not in row["keys"]
    todo = str(played.todo(DAY))
    assert "(13:30)" in todo and "🎟️ <code>NEWC1</code>" in todo and "FAIRC" not in todo, todo
    # un combiné à cote mini : « cote mini », jamais de bouton ni de code
    row["legs"][0]["xodds"] = None
    store.save(fair.COMBOS_FILE, [row])
    todo = played.todo(DAY)
    assert "À vérifier (cote mini 5) · cote mini" in str(todo) and "🎟️" not in str(todo) and "<code>" not in str(todo) and "xmaj" not in str(todo.markup), str(todo)
    # budget : la liste montre le prix 1xBet de la version en cours, son code et le bouton
    reset()
    money.set_daily(5000)
    fair.record_combos([(5, (legs, 1.55 ** 4, 0.72 ** 4))], DAY, {5: 500}, "FCFA", coupons={5: "FAIRC"}, at="04:12")
    todo = played.todo(DAY)
    assert "cote 1xBet 5,77 (04:12)" in str(todo) and "FAIRC" in str(todo), str(todo)
    assert f"xmaj|5|{DAY}" in [b["callback_data"] for row in todo.markup["inline_keyboard"] for b in row]
    assert "5,77" in chat.on_callback(f"joue|v5|{DAY}", 6)
    print("ok  E4 « Mes paris » : cote 1xBet de la version en cours, code coupon, bouton « Vérifier », exemple « Autre cote » au prix 1xBet")


def test_verify_real():
    msgs, picks = morning()
    sure = next(c for c in store.load("bets.json", {})["combos"])
    assert sure["xlegs"] and sure["coupon"] == "CODE2" and sure["at"] == "09:30"
    by_key = {c["id"]: c for c in picks}
    now = {"e1": {("h2h", "HOME", None): 1.2}, "e2": {("h2h", "HOME", None): 1.45}, "e3": {("h2h", "HOME", None): XB},
           "e4": {("h2h", "HOME", None): XB}}
    asked = []
    with Fakes(code="VERIF1") as f:
        out = app.refresh_real("sur", DAY, now=at(13), fetch=fake_fetch(now, asked))
    assert f.sent[0].startswith("⏳ Je vérifie ce combiné chez 1xBet"), f.sent
    assert {ev["id"] for _, ev in asked[0]} == {"e1", "e2", "e3", "e4"} and asked[0][0][1]["xgid"], "matchs du combiné relus (id 1xBet connu)"
    assert "Moins bien payé" in out and "📉" in out and "Code : <code>VERIF1</code>" in out, out
    assert "Cotes 1xBet de 13:00" in out and "cote 3,91" in out, out                    # 1,2 x 1,45 x 1,5 x 1,5
    assert f"xmajr|sur|{DAY}" in buttons(out), "le bouton reste sous la version vérifiée"
    row = store.load("bets.json", {})["combos"][0]
    assert row["keys"] == sure["keys"] and row["odds"] == sure["odds"], "rien n'est remplacé : le suivi garde ses paris"
    assert row["xnow"] == {"at": "13:00", "odds": 3.915, "coupon": "VERIF1"}, row["xnow"]
    todo = str(played.todo(DAY))
    assert "Sûr · cote 1xBet 3,92 (13:00)" in todo or "Sûr · cote 1xBet 3,91 (13:00)" in todo, todo
    assert "🎟️ <code>VERIF1</code>" in todo and "CODE2" not in todo, todo
    # un pari retiré par 1xBet : plus de combiné jouable tel quel, pas de nouveau code
    gone = dict(now) | {"e3": {("btts", "Yes", None): 1.8}}
    with Fakes(code="VERIF2") as f:
        out = app.refresh_real("sur", DAY, now=at(13, 30), fetch=fake_fetch(gone))
    assert "Plus chez 1xBet : Home3 – Away3" in out and "plus jouable tel quel" in out and "🎟️ Code" not in out and not f.coupons, out
    assert store.load("bets.json", {})["combos"][0]["xnow"] == {"at": "13:30", "dead": True}
    assert "plus jouable chez 1xBet (vérifié à 13:30)" in str(played.todo(DAY))
    # le pari du jour : même bouton, même vérification
    top = by_key["e4"]
    with Fakes(code="VERIF3") as f:
        out = app.refresh_real("pari", DAY, now=at(14), fetch=fake_fetch({"e4": {("h2h", "HOME", None): 1.42}}))
    assert "PARI DU JOUR À JOUR 14:00" in out and "— cote 1,42" in out and "Valeur +5 %" not in out and "Code : <code>VERIF3</code>" in out, out
    assert f"xmajr|pari|{DAY}" in buttons(out) and top["key"] in {r["key"] for r in track.load()}
    # les garde-fous : match commencé, 1xBet muet, autre jour, callback du bouton
    assert "en jeu" in app.refresh_real("sur", DAY, now=datetime(2030, 1, 1, 20, tzinfo=timezone.utc), fetch=fake_fetch(now))
    with Fakes() as f:
        assert "1xBet ne répond pas" in app.refresh_real("sur", DAY, now=at(13), fetch=fake_fetch({}))
    assert "plus en mémoire" in app.refresh_real("sur", "2030-01-02", now=at(13), fetch=fake_fetch(now))
    old = xbet.fetch_day
    xbet.fetch_day = fake_fetch(now)
    try:
        with Fakes(code="VERIF4") as f:
            out = chat.on_callback(f"xmajr|sur|{DAY}")
    finally:
        xbet.fetch_day = old
    assert "Code : <code>VERIF4</code>" in out, out
    print("ok  E4 « Vérifier ce combiné » d'un combiné réel et du pari du jour : cotes du moment, pari retiré / moins bien payé, nouveau code")


# ---------- E5 : alertes ----------

def test_alerts():
    # l'ancien message générique « La cote chute » n'existe plus : ses cas sont couverts par les alertes 1xBet (tests/test_alerts.py)
    assert not hasattr(app, "alert_text") and not hasattr(app, "alert_xbet")
    # cote 1xBet relue dans le cache du disque, sans réseau (base des alertes ⚠️ et 📈)
    u = xbet._events_url(77701)
    body = b'{"eventGroups": [{"events": [[{"type": 1, "cf": 1.52, "parameter": 0}, {"type": 2, "cf": 3.4}]]}]}'
    http._write(http._key(u, xbet.JSON_H), {"url": u, "fetched_at": state.clock(), "status": 200}, body)
    got = xbet.cached_cell([77701, 1, 0])
    assert got[0] == 1.52 and got[1] > 0, got
    assert xbet.cached_cell([77701, 9, 2.5])[0] is None, "match en cache, pari absent"
    assert xbet.cached_cell([77702, 1, 0]) is None, "match absent du cache"
    print("ok  E5 alertes : cote 1xBet du cache du disque (pari présent, pari absent, match inconnu)")


# ---------- E6 : résultats du matin ----------

def test_morning_results():
    reset()
    base = {"date": DAY, "sent": True, "single": True, "result": "win", "home": "H", "away": "A", "market": "h2h", "name": "H", "pick": "H gagne",
            "final": "2-0", "key": "k1", "odds": 1.6, "stake": 0}

    def text(row, combos=()):
        bets = {"picks": [], "combos": list(combos)}
        return fmt.plain(app.results_message(bets, [row], [row]) or "")
    assert "Pari du jour — cote de référence 1,60 (pas 1xBet)" in text(base), text(base)
    assert "Pari du jour — cote 1xBet 1,50" in text(base | {"xodds": 1.5})
    played_ = base | {"xodds": 1.5, "played": {"odds": 1.55, "stake": 10}}
    t = text(played_)
    assert "Pari du jour — cote 1,55 chez 1xBet" in t and "+5,5 FCFA" in t and " u" not in t, t       # 10 x (1,55 - 1), tout en FCFA
    budget = base | {"xodds": 1.5, "stake": 2000, "cur": "FCFA"}
    assert "cote 1xBet 1,50" in text(budget) and "FCFA" not in text(budget), text(budget)            # mise conseillée seule : pas noté, aucun montant
    assert "+1 000 FCFA" in text(budget | {"played": {"odds": 1.5, "stake": 2000}}), text(budget | {"played": {"odds": 1.5, "stake": 2000}})
    assert "perdu" not in text(base | {"result": "loss", "xodds": 1.5}) and "❌ Pari du jour — cote 1xBet 1,50" in text(base | {"result": "loss", "xodds": 1.5})
    assert "pas joué" in text(budget | {"skipped": True})                                           # ancien « ❌ Pas joué »
    # combiné non joué : le prix utilisé est dit
    leg = base | {"key": "k1"}
    combo = {"tier": "🛡️ SÛR (cote 5 à 7)", "keys": ["k1"], "odds": 5.06, "stake": 2.0, "result": "win", "date": DAY}
    assert "gagné — cote 5,06 aux cotes 1xBet" in text(leg, [combo | {"xlegs": [{"key": "k1", "xodds": 1.5, "xref": None}]}])
    assert "gagné — cote 5,06 (cotes de référence, pas 1xBet)" in text(leg, [dict(combo)])
    assert "chez 1xBet" in text(leg, [combo | {"played": {"odds": 5.0, "stake": 2.0}}])
    print("ok  E6 résultats du matin : prix utilisé dit (joué, 1xBet, référence) et gain du pari du jour")


# ---------- E7 / E8 : note « joué » d'un combiné renvoyé, libellés ----------

def test_resend_played_and_labels():
    legs = [prono(i) for i in range(4)]
    row = fair.combo_row(5, (legs, 1.55 ** 4, 0.72 ** 4), DAY)
    row["played"] = {"odds": 5.5, "stake": 1}
    fair.update_combo(row, (legs, 1.55 ** 4, 0.72 ** 4), "13:00")
    assert row.get("played"), "mêmes paris, même cote : la note reste"
    new = legs[:3] + [prono(5)]
    # lot I : un combiné du matin NOTÉ (le ticket est placé) n'est jamais remplacé : la ligne est rendue telle quelle
    snap = dict(row)
    fair.update_combo(row, (new, 1.55 ** 4, 0.72 ** 4), "14:00", "NEW")
    assert row == snap and row["played"] and "coupon" not in row, "autres paris : un combiné noté reste le ticket joué"
    # combiné sur mesure noté (son bouton « Vérifier » est une action explicite) : la note tombe avec la nouvelle version
    row = fair.combo_row(5, (legs, 1.55 ** 4, 0.72 ** 4), DAY) | {"custom": True, "played": {"odds": 5.5, "stake": 1}}
    fair.update_combo(row, (new, 1.55 ** 4, 0.72 ** 4), "14:00", "NEW")
    assert "played" not in row and row["coupon"] == "NEW" and row["at"] == "14:00", "autres paris : l'utilisateur note la nouvelle version"
    row["played"] = {"odds": 5.5, "stake": 1}
    fair.update_combo(row, (new, 1.55 ** 4 * 1.2, 0.72 ** 4), "15:00")
    assert "played" not in row and "coupon" not in row, "cote totale changée : la note tombe, l'ancien code aussi"
    # le relevé du matin ne remplace pas un combiné noté : il prévient (un seul message ⚠️), la note et les paris restent
    reset()
    fair.record_combos([(5, (legs, 1.55 ** 4, 0.72 ** 4))], DAY)
    rows = fair.load_combos()
    rows[0]["played"] = {"odds": 5.5, "stake": 1}
    store.save(fair.COMBOS_FILE, rows)
    store.save("today.json", {"day": DAY, "picks": [], "pronos": [prono(i) for i in range(7)]})
    with Fakes(drop=("m1",)) as f:
        app.run_xbet_refresh(now=at(13), announce=False, fetch=lambda ev, deadline=None, wanted=None, live=False, fresh_min=None: {
            e["id"]: {"gid": 900 + int(e["id"][1:]), "px": {} if e["id"] == "m1" else {("totals", "Under", 3.5): 1.55},
                      "refs": {} if e["id"] == "m1" else {("totals", "Under", 3.5): [900 + int(e["id"][1:]), 10, 3.5]}} for _, e in ev})
    assert "ton ticket" in str(f.sent[0]) and "retirée" not in str(f.sent[0]) and "COMBINÉ À JOUR" not in str(f.sent[0]), f.sent
    assert fair.load_combos()[0]["played"] == {"odds": 5.5, "stake": 1} and fair.load_combos()[0]["keys"] == [c["key"] for c in legs]
    # E8 : « cote 1xBet » pour un combiné réel, « cote mini » sinon (rapport du matin et bilan)
    real = fair_row(results=("win", "win", "win"), played={"odds": 7.0, "stake": 1}) | {"cur": None, "stake": 1}
    mini = fair_row(xodds=None, results=("win", "win", "win"), played={"odds": 4.0, "stake": 1}) | {"cur": None, "stake": 1}
    lines = played.summary_lines({"combos": []}, [], [real, mini])
    assert any(x.startswith("Combinés aux cotes 1xBet joués : cote obtenue") and x.endswith("(1)") for x in lines), lines
    assert any(x.startswith("Combinés à vérifier joués") and "face à la cote mini (1)" in x for x in lines), lines
    old = settle.settle_fair_combos
    rows = [real | {"result": "win", "reported": False, "min_odds": 5.0}, mini | {"result": "loss", "reported": False, "min_odds": 5.0}]
    settle.settle_fair_combos = lambda now=None, stats_for=None: (rows, rows)
    try:
        rep = str(app.fair_combos_report(at(8))[0])
    finally:
        settle.settle_fair_combos = old
    assert "Combiné aux cotes 1xBet (cote 5,00) gagné" in rep and "Combiné à cote mini 5,00 perdu" in rep, rep
    print("ok  E7/E8 note « joué » retirée à la nouvelle version, libellés cote 1xBet / cote mini")


# ---------- E9 / E11 : heures de relevé, code coupon indisponible ----------

def test_times_and_coupon_failure():
    reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": [], "xbet_at": "12:34"})
    pr = [prono(i) for i in range(3)]
    assert "💎 = cote 1xBet de 04:12" in fmt.pronos(pr, when="04:12") and "Cotes 1xBet de 13:00" in fmt.pronos(pr, intro="Hop.", when="13:00")
    assert "💎 = cote 1xBet du jour" in fmt.pronos(pr)
    msgs, _, _ = app.build_day([pick(1)], DAY, pronos=pr, odds_time="04:12")
    assert any("💎 = cote 1xBet de 04:12" in str(m) for m in msgs)
    # 1xBet injoignable : les cotes du dernier relevé, avec leur heure, jamais le titre « aux cotes 1xBet » sans heure
    legs = [prono(i) for i in range(4)]
    with Fakes() as f:
        out = str(app._live_reply((legs, 5.77, 0.27), 5.0, 6.25, now=at(13), check=lambda ids, now, fresh_min=None: ([], set(), "13:00")))
    assert "<b>💎 COMBINÉ — cote 5,77</b>" not in out and "<b>💎 COMBINÉ — cote 5,77 · COTES 1XBET DU DERNIER RELEVÉ (12:34)</b>" in out, out
    assert "🕒 Cotes du dernier relevé (12:34) : 1xBet ne répond pas, vérifie-les avant de valider." in out and "sans code coupon" in out and not f.coupons, out
    # code coupon indisponible : une ligne le dit
    with Fakes(code=None):
        out = str(app._fair_reply((legs, 5.77, 0.27), "Voici.", when="13:00", retry="redemande le combi"))
        assert "🎟️ Code indisponible pour l'instant : redemande le combi" in out and "Charger le coupon" not in out, out
        out = str(app._fair_reply((legs, 5.77, 0.27), "Voici.", when="13:00", retry=app.RETRY_BUTTON))
        assert "touche « 🎟️ Vérifier ce combiné »" in out
    with Fakes(code="OK1"):
        out = str(app._fair_reply((legs, 5.77, 0.27), "Voici.", when="13:00", retry="redemande le combi"))
        assert "Code : <code>OK1</code>" in out and "indisponible" not in out
    assert "indisponible" not in str(fmt.fair_combo(5.77, legs, 0.27, 0.5)) and not hasattr(fmt, "played_hint")
    assert all("hint" not in inspect.signature(f).parameters for f in (fmt.combo, fmt.single, fmt.fair_combo))
    print("ok  E9/E11/E13 heures de relevé, repli « dernier relevé », code coupon indisponible, code mort retiré")


# ---------- E14 : noms de double chance ----------

def test_double_chance_names():
    px = {("double_chance", "Home1 or Draw", None): 1.3, ("double_chance", "Draw or Away1", None): 1.9,
          ("double_chance", "Home1 or Away1", None): 1.25, ("h2h", "Draw", None): 3.4, ("h2h", "Home1", None): 2.2}
    assert xbet.lookup(px, "double_chance", "Draw or Home1", None) == 1.3 and xbet.lookup(px, "double_chance", "Away1 or Draw", None) == 1.9
    assert xbet.lookup(px, "double_chance", "away1 or home1", None) == 1.25 and xbet.lookup(px, "h2h", "draw", None) == 3.4
    assert xbet.lookup(px, "double_chance", "Home1 or Draw", None) == 1.3 and xbet.lookup(px, "double_chance", "Home2 or Draw", None) is None
    assert xbet.resolve(px, "h2h", "Home1", None) == ("h2h", "Home1", None) and xbet.lookup({}, "h2h", "Draw", None) is None
    picks = [pick(1, "double_chance", "Draw or Home1"), pick(2, "double_chance", "Away2 or Draw"), pick(3, "h2h", "Draw")]
    priced = {"e1": {("double_chance", "Home1 or Draw", None): 1.3}, "e2": {("double_chance", "Draw or Away2", None): 1.9},
              "e3": {("h2h", "Draw", None): 3.4}}
    st = xprice.price_picks(picks, fetch=fake_fetch(priced))
    assert st["priced"] == 3, st
    assert [c["xodds"] for c in picks] == [1.3, 1.9, 3.4] and all(c["xref"] and c["xref"][0] > 9000 for c in picks), picks
    assert picks[0]["name"] == "Draw or Home1", "le nom du pari n'est pas réécrit"
    print("ok  E14 double chance et nul : noms retrouvés quel que soit l'ordre des équipes")


# ---------- E15 : /monde après un rattrapage ----------

def test_monde_after_catchup():
    reset()
    store.save("today.json", {"day": "2030-01-02", "picks": [], "pronos": [prono(1)]})
    monde.save({"day": "2030-01-02", "leagues": {}, "africa": [], "paid": [], "at": "01:00", "complete": 1})
    old = app.now_utc
    app.now_utc = lambda: datetime(2030, 1, 2, 2, 0, tzinfo=timezone.utc)      # avant 04:00 : le jour Kelly est la veille, mais le rattrapage a écrit le 02
    try:
        out = str(app.handle_command("/monde"))
    finally:
        app.now_utc = old
    assert "Pas encore de relevé" not in out and "MATCHS DU" in out, out
    print("ok  E15 /monde après un rattrapage d'avant 04:00")


def main():
    test_combi_fallthrough()
    test_real_combo_gain()
    test_top_match_analyse()
    test_mes_paris()
    test_verify_real()
    test_alerts()
    test_morning_results()
    test_resend_played_and_labels()
    test_times_and_coupon_failure()
    test_double_chance_names()
    test_monde_after_catchup()
    print("tests interface 1xBet : OK")


if __name__ == "__main__":
    main()
