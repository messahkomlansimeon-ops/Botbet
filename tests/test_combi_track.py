"""« ✅ Je le joue » sous un combiné sur mesure (« combi 20 ») : le combiné n'est suivi que si l'utilisateur le dit (kelly/custom.py) ; l'appui
EST une note (mise conseillée en FCFA : noté tout de suite, sinon la cote et la mise sont demandées), puis il se comporte comme un combiné du
matin (« Mes paris », « ✏️ Autre cote » / « ↩️ Annuler », règlement, bilan, argent, « 🎟️ Vérifier ce combiné », alertes 1xBet), sans jamais être
remplacé tout seul ni compter dans la fiabilité des combinés du matin. Hors ligne (1xBet, Telegram et coupons simulés).
Lancer : python -m tests.test_combi_track"""
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-ctrack-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-ctrack-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-ctrack-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("STATS_API_KEY", None)
os.environ["XBET_PROXY_FILE"] = os.path.join(os.environ["DATA_DIR"], "pas-de-fichier.env")      # jamais le vrai proxy de la machine
os.environ["XBET_PROXY"] = "http://user:secret@proxy.test:80"

from kelly import app, bankroll, calib, chat, combos as CB, config as C, custom, fair, fmt, money, played, settle, store, telegram, track, xalerts, xprice  # noqa: E402
from kelly.sources import xbet  # noqa: E402

DAY = "2030-01-01"
KO = f"{DAY}T19:00:00Z"
TYP, XB, PROB = 1.6, 1.5, 0.7                       # cote typique des books, cote 1xBet, proba de consensus
UTC = timezone.utc


def at(h, m=0, day=1):
    return datetime(2030, 1, day, h, m, tzinfo=UTC)


def reset():
    """Données du jour vides, fiabilité neutre, pas de budget, aucune alerte envoyée, aucune question en attente."""
    for f in ("tracked.json", "fair.json", fair.COMBOS_FILE):
        store.save(f, [])
    store.save("bets.json", {"picks": [], "combos": []})
    store.save("bankroll.json", {"bankroll": 100.0, "initial": 100.0})
    for f in ("today.json", app.COMBO_UPDATES, money.FILE, calib.FILE, custom.FILE, xalerts.FILE, chat.PENDING_FILE, "odds_history.json"):
        store.save(f, {})
    calib._memo.clear()
    C.XALERTS_ENABLED, C.XALERT_LIVE_MAX = True, 3
    C.QUIET_HOURS = "off"


def pick(n, **kw):
    """Pari réel (The Odds API) du match e<n> : 1X2 de l'équipe à domicile à la cote typique TYP."""
    home, away = f"Home{n}", f"Away{n}"
    c = {"key": f"e{n}|h2h||{home}", "id": f"e{n}", "sport_key": "soccer_spain_la_liga", "group": "soccer", "event": f"{home} - {away}",
         "home": home, "away": away, "league": "Liga", "start": "19:00", "commence": KO, "market": "h2h", "who": None, "point": None,
         "name": home, "pick": fmt.label("h2h", home, None, None, "soccer"), "prob": PROB, "prob_book": PROB, "prob_src": "consensus",
         "n_sharp": 1, "odds": TYP, "best_odds": 1.8, "book": "Coolbet", "edge": PROB * TYP - 1, "spread": 0.02, "steam": 0.0,
         "n_books": 10, "weight": 1.0, "score": 0.12 + n / 100, "why": ""}
    return c | kw


def prono(i, **kw):
    """Pronostic 💎 : moins de 3,5 buts à 1,55 chez 1xBet (cote du matin 1,55), match m<i>."""
    c = {"id": f"m{i}", "sport_key": "soccer_epl", "home": f"H{i}", "away": f"A{i}", "commence": KO, "start": "19:00", "league": "L",
         "event": f"H{i} - A{i}", "market": "totals", "name": "Under", "point": 3.5, "who": None, "prob": 0.72, "p0": 0.72,
         "min_odds": 1.46, "conf": 0.6, "n_min": 8, "score": 0.2 - i / 1000, "key": f"m{i}|totals||3.5|Under",
         "pick": fmt.label("totals", "Under", 3.5, None, "soccer"), "xodds": 1.55, "xodds0": 1.55, "xev": 0.12, "xgid": 900 + i,
         "xref": [900 + i, 10, 3.5],
         "psh": {"under|3.5": [0.70, "pinnacle", 1]}}      # Pinnacle voit 70 % : sans avis du marché, l'ensemble (buts) ne validerait jamais ce pari
    return c | kw


def pronos(n=7, **kw):
    return [prono(i, **kw) for i in range(n)]


def world():
    """6 matchs de 1X2 à la cote typique 1,60 ; 1xBet paie 1,50 sur les 4 premiers ; e5 : pari absent de 1xBet ; e6 : match introuvable."""
    picks = [pick(i) for i in range(1, 7)]
    return picks, {f"e{i}": {("h2h", "HOME", None): XB} for i in range(1, 5)} | {"e5": {("btts", "Yes", None): 1.8}}


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


class Fakes:
    """Messages et boutons capturés, code coupon simulé (None : échec), relevé en direct des pronostics simulé (1,55 sur « moins de 3,5 buts » ;
    `drop` : matchs que 1xBet ne propose plus ; `got` faux : 1xBet muet) : aucun réseau."""

    def __init__(self, code="K7Q2X", drop=(), got=True):
        self.sent, self.edits, self.coupons, self.code, self.drop, self.got = [], [], [], code, drop, got

    def __enter__(self):
        self.old = (xbet.save_coupon, telegram.send, telegram.edit_buttons, xprice.live_check)
        real_check = xprice.live_check
        xbet.save_coupon = lambda ls: (self.coupons.append([c["id"] for c in ls]), self.code)[1]
        telegram.send = lambda text, *a, **k: (self.sent.append(text), True)[1]
        telegram.edit_buttons = lambda mid, markup, chat_id=None: (self.edits.append((mid, markup)), True)[1]
        px = {f"m{i}": {"gid": 900 + i, "px": {} if f"m{i}" in self.drop else {("totals", "Under", 3.5): 1.55},
                        "refs": {} if f"m{i}" in self.drop else {("totals", "Under", 3.5): [900 + i, 10, 3.5]}} for i in range(9)}
        xprice.live_check = lambda ids, now=None, fetch=None, fresh_min=None: real_check(
            ids, now, fetch=lambda ev, deadline=None, wanted=None, live=True, fresh_min=None:
            {k: v for k, v in px.items() if k in {e["id"] for _, e in ev}} if self.got else {})
        return self

    def __exit__(self, *a):
        xbet.save_coupon, telegram.send, telegram.edit_buttons, xprice.live_check = self.old

    def last_buttons(self):
        return [b["callback_data"] for row in self.edits[-1][1]["inline_keyboard"] for b in row if "callback_data" in b]

    def last_texts(self):
        return [b["text"] for row in self.edits[-1][1]["inline_keyboard"] for b in row if "callback_data" in b]


def buttons(msg):
    return [b["callback_data"] for row in (getattr(msg, "markup", None) or {"inline_keyboard": []})["inline_keyboard"] for b in row if "callback_data" in b]


def morning_fair(legs, tier=5, stake=None, cur=None):
    """Combiné 💎 du matin (fair_combos.json) sur les jambes `legs`."""
    fair.record_combos([(tier, (legs, round(1.55 ** len(legs), 3), 0.72 ** len(legs)))], DAY, {tier: stake} if stake is not None else None,
                       cur, coupons={tier: "FAIRC"}, at="04:12")


def keep_custom(legs, stake=None, cur=None, src="pronos", target=5.0, lo=5.0, hi=6.25, coupon="C1", at="09:30"):
    """Un combiné sur mesure gardé en mémoire, puis suivi (appui sur « ✅ Je le joue ») -> son identifiant « m<n> »."""
    total = round(math.prod(c.get("xodds") or c["min_odds"] for c in legs), 3)
    cid = custom.remember(DAY, target, lo, hi, (legs, total, 0.72 ** len(legs)), stake, cur, coupon, at, src)
    custom.track(cid, DAY)
    return cid


# ---------- 1. le bouton ----------

def test_button():
    reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos()})
    with Fakes() as f:
        msg = app.custom_combo([], pronos(), 5.0, DAY)
        assert buttons(msg) == [f"cjoue|m1|{DAY}", f"cnew|m1|{DAY}"] and msg.markup["inline_keyboard"][0][0]["text"] == "✅ Je le joue", buttons(msg)
        assert "Code : <code>K7Q2X</code>" in msg and isinstance(msg, telegram.Html)
    item = store.load(custom.FILE, {})["items"]["m1"]
    assert store.load(custom.FILE, {})["day"] == DAY and len(item["legs"]) == 4 and item["src"] == "pronos", item
    assert item["total"] == round(1.55 ** 4, 3) and item["target"] == 5.0 and (item["lo"], item["hi"]) == (5.0, 6.25), item
    assert item["coupon"] == "K7Q2X" and re.fullmatch(r"\d\d:\d\d", item["at"]), item
    assert item["stake"] is None and item["cur"] is None, "sans budget : aucune mise conseillée (jamais d'unités)"
    assert "Mise : règle ton budget du jour avec « 💰 Mon budget »" in msg and "Mise indicative" not in msg and " u</b>" not in msg, msg
    need = {"key", "id", "sport_key", "home", "away", "commence", "market", "name", "point", "prob", "xodds", "pick", "start", "league", "odds",
            "xref", "xgid", "min_odds"}
    assert all(need <= set(l) for l in item["legs"]), item["legs"][0]       # de quoi régler seul, refaire le coupon, relire chez 1xBet
    assert fair.load_combos() == [], "rien n'est suivi tant que l'utilisateur ne l'a pas dit"
    assert len(f"cjoue|m30|{DAY}".encode()) <= 64
    # un deuxième combi : un autre identifiant, un autre message
    with Fakes():
        assert buttons(app.custom_combo([], pronos(), 5.0, DAY)) == [f"cjoue|m2|{DAY}", f"cnew|m2|{DAY}"]
    # budget du jour : la mise (devise) du message est celle gardée
    money.set_daily(5000)
    with Fakes():
        msg = app.custom_combo([], pronos(), 20.0, DAY)
    item = store.load(custom.FILE, {})["items"]["m3"]
    assert item["cur"] == "FCFA" and item["stake"] > 0 and f"{item['stake']:,.0f}".replace(",", " ") + " FCFA (sur ton budget du jour)" in msg, (item, msg)
    # 1xBet muet : combiné du dernier relevé, sans code : le bouton est là, le code est vide
    reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos()})
    with Fakes(got=False):
        msg = app.custom_combo([], pronos(), 5.0, DAY)
    assert buttons(msg) == [f"cjoue|m1|{DAY}", f"cnew|m1|{DAY}"] and "1xBet ne répond pas" in msg and store.load(custom.FILE, {})["items"]["m1"]["coupon"] is None
    print("ok  1 bouton « ✅ Je le joue » sous le combiné 1xBet (en direct ou du dernier relevé), mémoire du jour, rien de suivi sans appui")


def test_no_button():
    reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos()})
    typ = [pick(i) for i in range(1, 5)]
    flat = pronos(7, xodds=None, xref=None, xev=None, xgid=None, xodds0=None)
    with Fakes():
        outs = {
            "impossible": app.custom_combo([], pronos(), 30.0, DAY),                       # 1,55 ^ 7 = 21,5 : hors de portée
            "cotes de référence": app.custom_combo(typ, [], 6.0, DAY),                      # jamais chiffré par 1xBet
            "rien": app.custom_combo([], [], 6.0, DAY, started=True),
        }
        soon = [prono(i, commence="2030-01-01T13:05:00Z") for i in range(4)]
        outs["imminent"] = app._live_reply((soon, 5.77, 0.27), 5.0, 6.25, now=at(13), keep={"day": DAY, "target": 5.0, "lo": 5.0, "hi": 6.25},
                                           check=lambda ids, now, fresh_min=None: ([], set(), "13:00"))
    assert "SUR MESURE" in str(outs["cotes de référence"]) and "cotes de référence (pas celles de 1xBet)" in outs["cotes de référence"]
    assert "commencent dans moins de" in outs["imminent"] and outs["impossible"].startswith("❌ Pas de combiné à la cote 30.\n📉 ")
    for name, o in outs.items():                    # une réponse « impossible » peut proposer une autre cote (cgo), jamais « ✅ Je le joue »
        assert not any(d.startswith(("cjoue|", "cnew|")) for d in buttons(o)) and not store.load(custom.FILE, {}).get("items"), (name, o)
    with Fakes():
        out = app.custom_combo(typ, flat, 4.5, DAY)                                         # pronostics à cote mini : suivi possible
    assert buttons(out) == [f"cjoue|m1|{DAY}", f"cnew|m1|{DAY}"] and "À VÉRIFIER CHEZ 1XBET" in out, out                # cote mini : bouton aussi
    item = store.load(custom.FILE, {})["items"]["m1"]
    assert not any(l["xodds"] for l in item["legs"]) and item["coupon"] is None and item["at"] is None, item
    # paris réels : leur prix 1xBet -> bouton ; aucun prix 1xBet -> cotes de référence, pas de bouton
    reset()
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    with Fakes():
        msg = app.custom_combo(picks, [], 5.0, DAY, when="09:30")
    assert buttons(msg) == [f"cjoue|m1|{DAY}", f"cnew|m1|{DAY}"] and "Code : <code>K7Q2X</code>" in msg and "SUR MESURE" in msg, msg
    item = store.load(custom.FILE, {})["items"]["m1"]
    assert item["src"] == "picks" and item["at"] == "09:30" and (item["lo"], item["hi"]) == (3.75, 6.25), item
    assert all(l["xodds"] == XB and l["xgid"] and l["xref"] for l in item["legs"]), item["legs"]
    print("ok  1 pas de bouton sous « impossible », cotes de référence, matchs imminents ; bouton sous cote mini et paris réels au prix 1xBet")


# ---------- 2. l'appui ----------

def test_tap_with_budget():
    reset()
    money.set_daily(5000)
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos()})
    with Fakes() as f:
        msg = app.custom_combo([], pronos(), 20.0, DAY)
        assert buttons(msg) == [f"cjoue|m1|{DAY}", f"cnew|m1|{DAY}"], buttons(msg)
        item = store.load(custom.FILE, {})["items"]["m1"]
        reply = chat.on_callback(f"cjoue|m1|{DAY}", 77)
    rows = fair.load_combos()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["tier"] == "m1" and row["custom"] is True and row["target"] == 20.0 and (row["lo"], row["hi"]) == (20.0, 25.0), row
    assert row["stake"] == item["stake"] > 0 and row["cur"] == "FCFA" and row["coupon"] == "K7Q2X" and row["at"] == item["at"], row
    assert row["min_odds"] == round(1.55 ** 7, 2) and row["result"] is None and len(row["legs"]) == 7 and row["date"] == DAY, row
    assert all(l["result"] is None and l["xodds"] == 1.55 for l in row["legs"]) and "skipped" not in row
    # « Je le joue » EST une note : à la cote du combiné affiché et à la mise conseillée, en FCFA
    assert row["played"]["odds"] == row["min_odds"] == 21.49 and row["played"]["stake"] == 250, row
    assert reply.startswith("✅ Noté : combi sur mesure (cote 20)") and "joué à 21,49 chez 1xBet, mise 250 FCFA" in reply, reply
    assert "Mes paris" in reply and "compté dans tes résultats" in reply and "Kelly te prévient" in reply, reply
    assert f.edits[-1][0] == 77 and f.last_buttons() == ["noop", f"joue|vm1|{DAY}", f"annule|vm1|{DAY}", f"xmaj|m1|{DAY}"], f.last_buttons()
    assert f.edits[-1][1]["inline_keyboard"][1][0]["text"] == "✏️ Autre cote" and f.edits[-1][1]["inline_keyboard"][1][1]["text"] == "↩️ Annuler"
    # « Mes paris » : nom, cote et mise notées, code coupon, boutons
    todo = played.todo(DAY)
    text = str(todo)
    assert "✅ Combi sur mesure (cote 20) · joué à 21,49 · 250 FCFA" in text and "🎟️ <code>K7Q2X</code>" in text, text
    assert "À vérifier" not in text and "Aux cotes 1xBet" not in text, text
    cb = buttons(todo)
    assert f"annule|vm1|{DAY}|l0" in cb and f"joue|vm1|{DAY}|l0" in cb and f"xmaj|m1|{DAY}" in cb and not any(b.startswith(("skip", "unskip")) for b in cb), cb   # la liste porte son numéro de page
    assert "🎟️ Vérifier : Combi sur mesure (cote 20)" in json.dumps(todo.markup, ensure_ascii=False)
    # deuxième appui : aucun doublon, boutons remis
    with Fakes() as f2:
        again = chat.on_callback(f"cjoue|m1|{DAY}", 77)
    assert "déjà dans « 🧾 Mes paris »" in again and "joué à 21,49" in again and len(fair.load_combos()) == 1 and f2.last_buttons()[0] == "noop", again
    # le sélecteur « Autre cote » connaît le combi sur mesure (et garde la mise notée si seule la cote change) ; touché après lecture (pas un double appui)
    chat.forget_taps()
    assert "À quelle cote as-tu finalement joué le combi sur mesure (cote 20)" in chat.on_callback(f"joue|vm1|{DAY}", 77)
    assert "Note annulée" in chat.on_callback(f"annule|vm1|{DAY}", 77) and "played" not in fair.load_combos()[0]
    print("ok  2 appui avec budget : noté tout de suite (cote du combiné, mise conseillée), ligne de suivi (devise, code, heure), « Autre cote » / « Annuler » + vérifier, « Mes paris », pas de doublon")


def test_tap_without_budget():
    """Cote 1xBet CONNUE (combiné aux cotes 1xBet) : jamais demandée. Sans mise conseillée, seule la mise est demandée (un montant seul, « 1000 »,
    « 1 000 » ou « 2 000 », est la mise ; « cote mise » corrige aussi la cote) ; « ✏️ Autre cote » demande la cote."""
    reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos()})
    with Fakes() as f:
        app.custom_combo([], pronos(), 5.0, DAY)
        reply = chat.on_callback(f"cjoue|m1|{DAY}", 78)
    row = fair.load_combos()[0]
    assert row["custom"] and "stake" not in row and "cur" not in row and "played" not in row and row["coupon"] == "K7Q2X", row
    assert reply.startswith("✅ Combiné suivi : Combi sur mesure (cote 5), cote 5,77") and "Avec quelle mise en FCFA ?" in reply, reply
    assert "À quelle cote" not in reply and "noté à la cote 1xBet 5,77" in reply, reply          # la cote est connue : jamais demandée
    p = chat.get_pending()
    assert p["what"] == "vm1" and p["msg"] == 78 and p["day"] == DAY and p["odds"] == 5.77, p
    assert f.last_buttons() == [f"joue|vm1|{DAY}", f"xmaj|m1|{DAY}"], "« ✅ Je l'ai joué » + vérifier"
    todo = played.todo(DAY)
    assert "▫️ Combi sur mesure (cote 5) · cote 1xBet 5,77" in str(todo), str(todo)          # pas noté (son bouton « ✅ Je l'ai joué » : ci-dessous)
    assert "Mise : règle ton budget du jour avec « 💰 Mon budget »" in str(todo) and f"joue|vm1|{DAY}|l0" in buttons(todo), str(todo)
    assert "🎟️ <code>K7Q2X</code>" in str(todo)
    # un montant seul est la mise (« 2 000 » n'est PAS ambigu ici : ce n'est pas une cote) ; la cote notée est celle de 1xBet
    for txt, stake in (("1000", 1000), ("1 000", 1000), ("2 000", 2000), ("2000 FCFA", 2000), ("1.500", 1500)):
        chat.set_pending("vm1", DAY, 78, odds=5.77)
        with Fakes() as f:
            out = chat.route(txt)[1]
        row = fair.load_combos()[0]
        assert out.startswith("✅ Noté : combi sur mesure (cote 5)") and "joué à 5,77 chez 1xBet" in out and chat.get_pending() is None, (txt, out)
        assert row["played"]["odds"] == 5.77 and row["played"]["stake"] == stake and row["cur"] == "FCFA", (txt, row)
        assert f.edits[-1][0] == 78 and f.last_texts()[0].startswith("✅ Joué à 5,77")
        assert "Note annulée" in played.command(["vm1", "0"], DAY)
    # « cote mise » corrige aussi la cote ; un décimal seul est une cote (la mise est alors redemandée avec cette cote)
    chat.set_pending("vm1", DAY, 78, odds=5.77)
    with Fakes():
        out = chat.route("5,40 1 000")[1]
    row = fair.load_combos()[0]
    assert "joué à 5,40 chez 1xBet, mise 1 000 FCFA" in out and row["played"]["odds"] == 5.4, out
    played.command(["vm1", "0"], DAY)
    chat.set_pending("vm1", DAY, 78, odds=5.77)
    with Fakes():
        ask = chat.route("5,40")[1]
        assert ask.startswith("Avec quelle mise en FCFA ?") and "cote 5,40 retenue" in ask and chat.get_pending()["odds"] == 5.4, ask
        out = chat.route("750")[1]
    assert "joué à 5,40 chez 1xBet, mise 750 FCFA" in out and fair.load_combos()[0]["played"]["stake"] == 750, out
    # incompréhensible : on redemande la mise, la question reste ouverte
    played.command(["vm1", "0"], DAY)
    chat.set_pending("vm1", DAY, 78, odds=5.77)
    with Fakes():
        for txt in ("0", "5,40 2 00"):
            assert chat.route(txt)[1].startswith("Je n'ai pas compris. Réponds ta mise, par exemple 1 000 (la cote 5,77 est retenue)"), txt
    assert chat.get_pending()["odds"] == 5.77 and "played" not in fair.load_combos()[0]
    # « ✏️ Autre cote » (pari noté) demande la cote ; la mise notée reste
    chat.clear_pending()
    played.command(["vm1", "5,77", "1000"], DAY)
    with Fakes():
        assert "À quelle cote as-tu finalement joué" in chat.on_callback(f"joue|vm1|{DAY}", 78)
        out = chat.route("5,40")[1]
    assert "joué à 5,40 chez 1xBet, mise 1 000 FCFA" in out, out
    lines = played.summary_lines({"combos": []}, [], fair.load_combos())
    assert any("Combinés aux cotes 1xBet joués" in x for x in lines) and "1 noté" in lines[0], lines
    # annuler la note
    assert "Note annulée" in played.command(["vm1", "0"], DAY)
    # gros combiné (« combi 10000 » à 11 061,90) : sa cote est connue, seule la mise est demandée ; « 11000 1000 » corrige la cote
    legs = [prono(i, xodds=x) for i, x in enumerate((9.5, 9.7, 12.0, 10.0))]
    with Fakes():
        cid = custom.remember(DAY, 10000.0, 10000.0, 12500.0, (legs, 11061.9, 0.0001), None, None, "BIG", "16:27")
        reply = chat.on_callback(f"cjoue|{cid}|{DAY}", 79)
        assert "Avec quelle mise en FCFA ?" in reply and "À quelle cote" not in reply and "cote 1xBet 11061,9" in reply, reply
        out = chat.route("1000")[1]
        assert out.startswith("✅ Noté : combi sur mesure (cote 10000)") and "joué à 11061,9 chez 1xBet, mise 1 000 FCFA" in out, out
        assert "Note annulée" in chat.on_callback(f"annule|v{cid}|{DAY}", 79)
        chat.on_callback(f"joue|v{cid}|{DAY}", 79)
        out = chat.route("11000 1000")[1]
        assert "joué à 11000,0 chez 1xBet, mise 1 000 FCFA" in out, out
        chat.on_callback(f"joue|v{cid}|{DAY}", 79)                      # noté : « Autre cote » demande la cote
        assert "par exemple 11061,90" in chat.route("99999")[1]           # au-delà du double de sa cote : redemandée
    print("ok  2 cote 1xBet connue jamais demandée : sans mise conseillée seule la mise est demandée (« 1000 », « 2 000 » = mise), « cote mise » corrige "
          "la cote, « Autre cote » la demande ; gros combiné à 11 061,90")


def test_tap_unfunded_and_gone():
    reset()
    money.set_daily(1)                                               # budget minuscule : mise arrondie à 0
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos()})
    with Fakes() as f:
        app.custom_combo([], pronos(), 5.0, DAY)
        reply = custom.track("m1", DAY, 5)
    row = fair.load_combos()[0]
    assert row["stake"] == 0 and "cur" not in row and "played" not in row, row
    assert "Avec quelle mise en FCFA ?" in reply and "À quelle cote" not in reply, reply     # pas de mise conseillée : Kelly demande la mise (la cote 1xBet est connue)
    assert f.last_buttons() == [f"joue|vm1|{DAY}", f"xmaj|m1|{DAY}"] and f.edits[-1][1]["inline_keyboard"][0][0]["text"] == "✅ Je l'ai joué"
    todo = str(played.todo(DAY))                     # mise trop petite pour le budget : aucune mise affichée (ni « règle ton budget » : il est réglé)
    assert "▫️ Combi sur mesure (cote 5) · " in todo and " · mise " not in todo and "règle ton budget" not in todo, todo
    assert money.gain(row | {"result": "win"}) is None
    # identifiant d'un autre jour, inconnu ou mal formé
    reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos()})
    with Fakes():
        app.custom_combo([], pronos(), 5.0, DAY)
        assert custom.track("m1", "2030-01-02") == custom.GONE == "Ce combiné n'est plus en mémoire (il date d'un autre jour)."
        assert custom.track("m9", DAY) == custom.GONE and chat.on_callback(f"cjoue|zz|{DAY}", 1) == custom.GONE
        assert fair.load_combos() == []
        # le lendemain : la numérotation repart de 1, l'ancien identifiant n'existe plus
        assert custom.remember("2030-01-02", 5.0, 5.0, 6.25, ([prono(0), prono(1)], 2.4, 0.5)) == "m1"
        assert custom.track("m1", DAY) == custom.GONE
    # mémoire bornée
    reset()
    ids = [custom.remember(DAY, 5.0, 5.0, 6.25, ([prono(0), prono(1)], 2.4, 0.5)) for _ in range(custom.KEEP + 5)]
    doc = store.load(custom.FILE, {})
    assert len(doc["items"]) == custom.KEEP and ids[-1] == f"m{custom.KEEP + 5}" and "m1" not in doc["items"] and "m6" in doc["items"]
    assert custom.remember("", 5.0, 5.0, 6.25, ([prono(0)], 1.5, 0.7)) is None, "pas de jour : rien à suivre"
    # /joue vm3 sans ligne : un vrai message
    assert played.find("vm3", DAY) == "Pas de combi sur mesure « vm3 » à noter (ni aujourd'hui ni en attente de résultat)."
    assert played.command(["vm3", "5"], DAY).startswith("Pas de combi sur mesure « vm3 »")
    assert "vm1 (combi sur mesure)" in played.command(["zzz", "5"], DAY) and played.find("vzzz", DAY) is None
    print("ok  2 mise de 0 : suivi sans mise conseillée (la mise est demandée), autre jour / inconnu / mal formé : message, numérotation du jour, mémoire bornée, /joue vm3")


def test_record_custom():
    reset()
    old = {"date": "2029-12-01", "tier": 5, "min_odds": 5.0, "prob": 0.2, "keys": [], "legs": [], "result": "win"}
    recent = old | {"date": "2029-12-25", "tier": 12}
    store.save(fair.COMBOS_FILE, [old, recent])
    row = {"date": DAY, "tier": "m1", "custom": True, "min_odds": 5.0, "keys": [], "legs": [], "result": None}
    assert fair.record_custom(row, DAY) is True and fair.record_custom(row | {"min_odds": 9.9}, DAY) is False, "un seul par jour et par identifiant"
    rows = fair.load_combos()
    assert [r["tier"] for r in rows] == [12, "m1"] and rows[1]["min_odds"] == 5.0, rows        # C.FAIR_COMBO_KEEP_DAYS jours gardés
    assert C.FAIR_COMBO_KEEP_DAYS == 14
    print("ok  2 fair.record_custom : un seul par jour et par identifiant, anciens jours retirés")


def test_command_route():
    reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos()})
    with Fakes() as f:
        out = app.handle_command("/combi 5")
        picker = chat.on_callback("combi|5", 3)            # la cote, puis les types de paris à cocher (tests/test_combi_kinds.py)
        via_button = chat.on_callback("cgo|5|", 3)          # « 🎯 Construire le combiné », rien de coché : tous les paris
        typed = chat.route("combi 5")
    assert buttons(picker)[-1] == "cgo|5||" and "quels paris" in picker, picker
    assert buttons(out) == [f"cjoue|m1|{DAY}", f"cnew|m1|{DAY}"] and buttons(via_button) == [f"cjoue|m2|{DAY}", f"cnew|m2|{DAY}"] and typed == ("/combi 5", None), (out, via_button)
    print("ok  2 /combi 5 et le sélecteur de cote : le bouton « ✅ Je le joue » arrive jusqu'à Telegram")


def test_skip():
    """Ancien bouton « ❌ Pas joué » (anciens messages) : le combi sur mesure noté en sort, « ↩️ Finalement joué » l'y remet."""
    reset()
    money.set_daily(5000)
    cid = keep_custom(pronos(4), stake=500, cur="FCFA")
    assert cid == "m1" and fair.load_combos()[0]["played"]["stake"] == 500
    reply = money.skip("vm1", DAY)
    row = fair.load_combos()[0]
    assert "retiré de ton bilan" in reply and "combi sur mesure (cote 5)" in reply and row["skipped"] is True, reply
    assert [x for x in played.items({"combos": []}, [], fair.load_combos())] == [] and money.gain(row | {"result": "win"}) is None
    assert "❌ Combi sur mesure (cote 5) : pas joué" in str(played.todo(DAY))
    assert f"unskip|vm1|{DAY}|l0" in buttons(played.todo(DAY))
    assert "compté comme joué" in money.skip("vm1", DAY, undo=True) and "skipped" not in fair.load_combos()[0]
    print("ok  2 ancien « ❌ Pas joué » / « ↩️ Finalement joué » sur un combi sur mesure noté")


# ---------- 3. règlement, bilan, argent ----------

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


def test_settle_and_bilan():
    reset()
    money.set_daily(5000)
    keep_custom(pronos(4), stake=500, cur="FCFA")
    morning_fair(pronos(4), tier=5, stake=500, cur="FCFA")
    after = at(8, day=2)
    with Scores({}):                                                 # 1-0 partout : « moins de 3,5 buts » gagne ; réglé et annoncé le lendemain matin
        msgs = app.fair_combos_report(after)
    rows = fair.load_combos()
    assert [c["result"] for c in rows] == ["win", "win"], rows
    mine = next(c for c in rows if c.get("custom"))
    assert abs(money.gain(mine) - 500 * (1.55 ** 4 - 1)) < 1e-6, money.gain(mine)
    n, total, staked = money.result({"combos": []}, [], rows)
    assert n == 1 and abs(total - 500 * (1.55 ** 4 - 1)) < 1e-6 and staked == 500, (n, total, staked)          # le combiné du matin n'est pas noté : hors compte
    # le résultat du matin nomme le combi sur mesure et donne son gain ; la fiabilité des combinés du matin ne le compte pas
    text = str(msgs[0])
    assert "✅ Combi sur mesure (cote 5) gagné</b> · +2 386 FCFA" in text and "✅ Combiné aux cotes 1xBet (cote 5,77) gagné</b>" in text, text
    assert "(cote 5,77) gagné</b> ·" not in text, "le combiné du matin n'est pas noté : aucun montant"
    assert "Combinés à vérifier chez 1xBet : 1 gagné sur 1 (le résultat en argent ne compte que ceux que tu as notés)." in text, text
    summary = "Combinés à vérifier chez 1xBet : 1 gagné sur 1 (le résultat en argent ne compte que ceux que tu as notés)."
    assert settle.fair_combo_line(rows) == summary and settle.fair_combo_line([mine]) == ""
    assert settle.fair_combo_line([mine | {"result": "loss"}, rows[1]]) == summary, "perdu ou gagné : le combi sur mesure est hors de cette ligne"
    # le bilan compte le combi sur mesure (argent) même quand il est le seul réglé
    store.save(fair.COMBOS_FILE, [mine])
    bilan = str(settle.bilan({"picks": [], "combos": []}, rows=[]))
    assert "Aucun pari réglé" not in bilan and "Tes paris notés" in bilan and "+2 386 FCFA sur 1 pari" in bilan, bilan
    assert "Combinés à vérifier chez 1xBet" not in bilan
    # perdu : une jambe à 3-1
    reset()
    money.set_daily(5000)
    keep_custom(pronos(4), stake=500, cur="FCFA")
    with Scores({"m2": (3, 1)}):
        text = str(app.fair_combos_report(after)[0])
    row = fair.load_combos()[0]
    assert row["result"] == "loss" and money.gain(row) == -500 and [l["result"] for l in row["legs"]] == ["win", "win", "loss", "win"], row
    assert settle.fair_combo_line([row]) == "" and money.result({"combos": []}, [], [row])[:2] == (1, -500)
    assert "❌ Combi sur mesure (cote 5) perdu — 3 sur 4</b> · −500 FCFA" in text and "Combinés à vérifier chez 1xBet :" not in text, text
    # noté joué sans budget : sa cote et sa mise (FCFA) comptent dans le bilan en argent ; la bankroll en unités ne bouge plus
    reset()
    keep_custom(pronos(4))
    assert "✅ Noté" in played.command(["vm1", "6,00", "2000"], DAY)
    with Scores({}):
        app.fair_combos_report(after)
    row = fair.load_combos()[0]
    gain = 2000 * (6.0 * (1.55 ** 4) / row["min_odds"] - 1)
    assert row["result"] == "win" and abs(bankroll.played_gain(row) - gain) < 1e-6 and abs(money.gain(row) - gain) < 1e-6 and row["counted"], row
    assert bankroll.load()["bankroll"] == 100.0 and row["cur"] == "FCFA", bankroll.load()
    assert "1 gagné sur 1" in played.summary_lines({"combos": []}, [], [row])[0]
    print("ok  3 règlement gagné / perdu, gain en argent des seuls paris notés, bilan, « Combi sur mesure » dans les résultats, hors fiabilité des combinés du matin")


def test_settle_real_picks_and_unknown():
    reset()
    money.set_daily(5000)
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": [], "picks_at": "09:30"})
    with Fakes():
        app.custom_combo(picks, [], 5.0, DAY, when="09:30")
        custom.track("m1", DAY)
    row = fair.load_combos()[0]
    assert row["src"] == "picks" and played.is_real(row) and [l["market"] for l in row["legs"]] == ["h2h"] * 4, row
    with Scores({}, default=(2, 0)):                                 # l'équipe à domicile gagne
        rows, _ = settle.settle_fair_combos(at(8, day=2))
    assert rows[0]["result"] == "win" and abs(money.gain(rows[0]) - rows[0]["stake"] * (XB ** 4 - 1)) < 1e-6, rows[0]
    # un pari que le règlement gratuit ne connaît pas : « non vérifiable », jamais gagné ni plantage
    reset()
    money.set_daily(5000)
    odd = [prono(0), prono(1, market="draw_no_bet", name="Alpha", key="m1|draw_no_bet||Alpha", point=None, pick="Alpha, remboursé si nul")]
    keep_custom(odd, stake=500, cur="FCFA")
    with Scores({}):
        report = app.fair_combos_report(at(8, day=10))
    row = fair.load_combos()[0]
    assert row["result"] == "unknown" and [l["result"] for l in row["legs"]] == ["win", "unknown"], row
    assert money.gain(row) is None and money.result({"combos": []}, [], [row])[0] == 0 and settle.fair_combo_line([row]) == ""
    assert "❔ Combi sur mesure (cote 5) non vérifiable" in str(report[0]) and "gagné" not in str(report[0]).replace("non vérifiable", ""), report
    print("ok  3 paris réels au prix 1xBet réglés comme un combiné du matin ; pari inconnu du règlement gratuit : « non vérifiable »")


# ---------- 4. jamais renvoyé tout seul, bouton « Vérifier » ----------

def test_no_auto_resend():
    reset()
    money.set_daily(5000)
    legs = pronos(4)
    morning_fair(legs, tier=5, stake=500, cur="FCFA")
    keep_custom(legs, stake=500, cur="FCFA")
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos(7)})
    gone = [prono(0), prono(1, xodds=None, xref=None, xev=None)] + [prono(i) for i in range(2, 7)]          # 1xBet ne propose plus m1
    before = next(r for r in fair.load_combos() if r.get("custom"))
    with Fakes() as f:
        n = app._update_combos(DAY, gone, at(13), {"m0", "m1", "m2", "m3", "m4"})
    assert n == 1 and len(f.sent) == 1 and "COMBINÉ À JOUR" in f.sent[0], f.sent                             # le combiné du matin est renvoyé...
    rows = fair.load_combos()
    assert next(r for r in rows if r.get("custom")) == before, "...pas le combi sur mesure"
    assert "m1" not in store.load(app.COMBO_UPDATES, {})["tiers"] and "5" in store.load(app.COMBO_UPDATES, {})["tiers"]
    # le relevé complet : même résultat, et les matchs du combi sur mesure sont relus en premier
    store.save("today.json", {"day": DAY, "picks": [], "pronos": gone, "announced": []})
    assert {"m0", "m1", "m2", "m3"} <= app._priority_ids(DAY, gone)
    print("ok  4 _update_combos laisse le combi sur mesure intact (le combiné du matin est bien renvoyé)")


def test_refresh_custom():
    reset()
    money.set_daily(5000)
    keep_custom(pronos(4), stake=250, cur="FCFA", at="09:30")
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pronos(7)})
    with Fakes(code="NEWC1", drop=("m1",)) as f:
        out = app.refresh_combo("m1", DAY, now=at(13, 30))
    assert f.sent[0].startswith("⏳ Je vérifie ce combiné chez 1xBet"), f.sent
    assert "🔄 COMBI SUR MESURE À JOUR 13:30" in out and "Combi sur mesure remis aux cotes 1xBet de 13:30" in out, out
    assert "Combiné du matin" not in out and "Code : <code>NEWC1</code>" in out and "❌ Retiré : H1 – A1" in out, out
    row = fair.load_combos()[0]
    assert row["coupon"] == "NEWC1" and row["at"] == "13:30" and row["updated"] == "13:30" and "m1|totals||3.5|Under" not in row["keys"], row
    assert 5.0 <= row["min_odds"] <= 6.25 and row["custom"] and row["stake"] == 250 and row["cur"] == "FCFA" and row["target"] == 5.0, row
    assert store.load(app.COMBO_UPDATES, {})["tiers"]["m1"]["keys"] == row["keys"]
    # un pari remplacé = un autre ticket : la note « joué à … » de l'ancienne version tombe, « ✅ Je l'ai joué » (mise conseillée) est remis
    assert "played" not in row and "Ta note « joué à … » de l'ancienne version est retirée" in out, (row, out)
    assert buttons(out) == [f"joue|vm1|{DAY}", f"xmaj|m1|{DAY}"] and out.markup["inline_keyboard"][0][0]["text"] == "✅ Je l'ai joué — 250 FCFA", buttons(out)
    assert "250 FCFA" in out and "🎟️ <code>NEWC1</code>" in str(played.todo(DAY))
    # un match a commencé : plus touché
    with Fakes() as f:
        assert "en jeu" in app.refresh_combo("m1", DAY, now=at(19, 30)) and not f.sent
    # un combi sur mesure à la cote mini de Kelly (pas chez 1xBet) : rien à revérifier
    reset()
    keep_custom(pronos(4, xodds=None, xref=None, xev=None), stake=1.0)
    with Fakes() as f:
        assert "rien à revérifier" in app.refresh_combo("m1", DAY, now=at(13)) and not f.sent
    assert chat.refresh_button("vm1", DAY) is None, "pas de bouton « Vérifier » sous une cote mini"
    # un autre jour / inconnu
    assert app.refresh_combo("m7", DAY, now=at(13)) == "Ce combiné n'est plus en mémoire (il date d'un autre jour)."
    print("ok  4 « Vérifier ce combiné » : sa fourchette de cote, titre « COMBI SUR MESURE À JOUR », code et version suivis, mise gardée")


def test_refresh_real_picks():
    reset()
    money.set_daily(5000)
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": [], "picks_at": "09:30"})
    with Fakes():
        app.custom_combo(picks, [], 5.0, DAY, when="09:30")
        custom.track("m1", DAY)
    before = fair.load_combos()[0]
    keys = list(before["keys"])
    assert chat.refresh_button("vm1", DAY) == ("🎟️ Vérifier ce combiné", f"xmaj|m1|{DAY}")
    # e1 est moins bien payé : mêmes paris, cotes et code du moment
    now = {"e1": {("h2h", "HOME", None): 1.2}, "e2": {("h2h", "HOME", None): 1.45}, "e3": {("h2h", "HOME", None): XB},
           "e4": {("h2h", "HOME", None): XB}}
    asked = []
    with Fakes(code="VERIF1") as f:
        out = app.refresh_combo("m1", DAY, now=at(13), fetch=fake_fetch(now, asked))
    assert f.sent[0].startswith("⏳") and {i for c in asked for i in c} == {"e1", "e2", "e3", "e4"}, (f.sent, asked)
    assert "Moins bien payé" in out and "🔄 COMBI SUR MESURE À JOUR 13:00" in out and "Code : <code>VERIF1</code>" in out, out
    row = fair.load_combos()[0]
    assert row["keys"] == keys and row["coupon"] == "VERIF1" and row["at"] == "13:00" and row["stake"] == before["stake"], row
    assert abs(row["min_odds"] - 1.2 * 1.45 * XB * XB) < 0.006 and {l["id"]: l["xodds"] for l in row["legs"]} == {"e1": 1.2, "e2": 1.45, "e3": XB, "e4": XB}, row
    assert buttons(out) == [f"joue|vm1|{DAY}", f"xmaj|m1|{DAY}"] and "played" not in row, buttons(out)      # la cote totale a changé : la note tombe
    # 1xBet ne propose plus un pari : plus jouable tel quel, rien n'est modifié
    gone = {k: v for k, v in now.items() if k != "e2"} | {"e2": {("btts", "Yes", None): 1.8}}
    saved = fair.load_combos()[0]
    with Fakes(code="NOPE") as f:
        out = app.refresh_combo("m1", DAY, now=at(13, 10), fetch=fake_fetch(gone))
    assert "n'est plus jouable tel quel" in out and "❌ Plus chez 1xBet : Home2 – Away2" in out and not f.coupons, out
    assert fair.load_combos()[0] == saved
    # 1xBet muet
    with Fakes() as f:
        assert "1xBet ne répond pas" in app.refresh_combo("m1", DAY, now=at(13, 20), fetch=fake_fetch({}))
    print("ok  4 « Vérifier ce combiné » d'un combi sur mesure de paris réels : relu comme un combiné réel, paris gardés, nouveau code")


def test_real_picks_without_budget():
    """Paris réels sans budget : le message ne donne aucune mise (la ligne « règle ton budget »), jamais d'unités ; sa ligne n'a pas de mise,
    seule la mise est demandée (sa cote 1xBet est connue), puis le message de vérification redonne la mise NOTÉE en FCFA."""
    reset()
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": [], "picks_at": "09:30"})
    with Fakes() as f:
        msg = app.custom_combo(picks, [], 5.0, DAY, when="09:30")
        item = store.load(custom.FILE, {})["items"]["m1"]
        assert item["cur"] is None and item["stake"] is None, item
        assert "Mise : règle ton budget du jour avec « 💰 Mon budget »" in msg and " u</b>" not in msg and "mise conseillée :" not in msg, msg
        reply = chat.on_callback(f"cjoue|m1|{DAY}", 9)
        assert "stake" not in fair.load_combos()[0] and reply.count("Avec quelle mise en FCFA ?") == 1 and "À quelle cote" not in reply, (fair.load_combos()[0], reply)
        out = chat.route("1 000")[1]               # la cote 1xBet (5,06) est connue : un montant seul est la mise
    row = fair.load_combos()[0]
    assert "joué à 5,06 chez 1xBet, mise 1 000 FCFA" in out and row["src"] == "picks" and row["cur"] == "FCFA", out
    assert row["played"]["stake"] == 1000 and row["played"]["odds"] == 5.06, row
    # « ✏️ Autre cote » : la cote est demandée, la mise notée reste
    with Fakes():
        assert "À quelle cote as-tu finalement joué" in chat.on_callback(f"joue|vm1|{DAY}", 9)
        out = chat.route("5,10")[1]
    assert "joué à 5,10 chez 1xBet, mise 1 000 FCFA" in out and fair.load_combos()[0]["played"]["odds"] == 5.1, out
    with Fakes(code="VERIF2") as f:
        out = app.refresh_combo("m1", DAY, now=at(13), fetch=fake_fetch({f"e{i}": {("h2h", "HOME", None): XB} for i in range(1, 5)}))
    assert "💰 Mise : <b>1 000 FCFA</b>" in out and " u</b>" not in out and "aucun changement" in out, out
    assert fair.load_combos()[0]["played"]["odds"] == 5.1, "mêmes paris, même cote : la note « joué à … » reste"
    print("ok  2 paris réels sans budget : aucune mise en unités, seule la mise est demandée (cote 1xBet connue), reprise par « Vérifier ce combiné » (mise notée)")


# ---------- 5. alertes ----------

def alert_setup(budget=True):
    reset()
    if budget:
        money.set_daily(5000)
    keep_custom(pronos(4), stake=250 if budget else 1.0, cur="FCFA" if budget else None)
    today = {"day": DAY, "picks": [], "pronos": pronos(7), "xbet_at": "13:00"}
    store.save("today.json", today)
    return today


def test_alerts():
    today = alert_setup()
    rows, combos = track.load(), []
    targets = xalerts.real_targets(today, rows, combos, DAY, at(13))
    assert len(targets) == 1 and targets[0][:3] == ("Le combi sur mesure (cote 5)", "Combi sur mesure (cote 5)", "vm1"), targets
    assert [l["key"] for l in targets[0][4]] == [f"m{i}|totals||3.5|Under" for i in range(4)] and all(l["xgid"] for l in targets[0][4])
    took, noted = xalerts.took_bets(rows, combos, DAY)
    assert took == {f"m{i}|totals||3.5|Under": 1.55 for i in range(4)} and noted == set(took), (took, noted)
    assert not xalerts.real_targets(today, rows, combos, DAY, at(18, 55)), "un match imminent : plus d'alerte"
    # ⚠️ un pari quitte 1xBet : message, bouton « Vérifier ce combiné : Combi sur mesure (cote 5) »
    px = {f"m{i}": {("totals", "Under", 3.5): 1.55} for i in range(4)} | {"m2": {("btts", "Yes", None): 1.8}}
    up, got = [prono(i) for i in range(7)], {"m0", "m1", "m2", "m3"}
    with Fakes() as f:                                               # ticket déjà placé (noté joué) : plus d'alerte « en danger »
        assert xalerts.on_refresh(today, at(13), got, up, fake_fetch(px)) is None and not f.sent, f.sent
    rows_ = fair.load_combos()                                       # la suite : le même combi, suivi mais PAS ENCORE joué
    rows_[0].pop("played", None)
    store.save(fair.COMBOS_FILE, rows_)
    store.save(xalerts.FILE, {})
    with Fakes() as f:
        msg = xalerts.on_refresh(today, at(13), got, up, fake_fetch(px))
    assert msg is not None and len(f.sent) == 1, f.sent
    text = str(msg)
    assert "⚠️ Mes paris en danger" in text and "Le combi sur mesure (cote 5) n'est plus jouable tel quel" in text, text
    assert "❌ Plus chez 1xBet : H2 – A2" in text, text
    assert buttons(msg) == [f"xmaj|m1|{DAY}"] and "🎟️ Vérifier ce combiné : Combi sur mesure (cote 5)" in json.dumps(msg.markup, ensure_ascii=False)
    assert "📈" not in text and "💰" not in text
    with Fakes() as f:                                               # même état au relevé suivant : pas deux fois la même alerte
        assert xalerts.on_refresh(today, at(13, 30), got, up, fake_fetch(px)) is None and not f.sent
    # 1xBet paie moins que la cote mini de Kelly : danger ; au-dessus de la cote mini (mais sous la cote prise) : rien
    store.save(xalerts.FILE, {})
    low = {f"m{i}": {("totals", "Under", 3.5): 1.55} for i in range(4)} | {"m1": {("totals", "Under", 3.5): 1.30}}
    with Fakes() as f:
        msg = xalerts.on_refresh(today, at(14), got, up, fake_fetch(low))
    assert msg and "📉 Moins bien payé : H1 – A1" in str(msg) and "1,55 ➜ <b>1,30</b>" in str(msg), msg
    store.save(xalerts.FILE, {})
    ok = {f"m{i}": {("totals", "Under", 3.5): 1.55} for i in range(4)} | {"m1": {("totals", "Under", 3.5): 1.50}}
    with Fakes() as f:
        assert xalerts.on_refresh(today, at(14), got, up, fake_fetch(ok)) is None and not f.sent
    # ni « mieux payé » ni « joue maintenant » pour un pari déjà pris dans un combi sur mesure
    risen = [prono(0, xodds=1.90, xev=0.3)] + [prono(i) for i in range(1, 7)]
    prices = xalerts.Prices(xalerts.Reader(fake_fetch(px)), at(13))
    assert [it["key"] for it in xalerts.rise_items(risen, {"m0"}, "13:00", [], prices)] == ["m0|totals||3.5|Under"]
    assert xalerts.rise_items(risen, {"m0"}, "13:00", [], prices, noted) == [], "pari pris : pas de « mieux payé »"
    # retiré par « ❌ Pas joué » ou réglé : plus rien
    money.skip("vm1", DAY)
    assert not xalerts.real_targets(today, track.load(), [], DAY, at(13)) and xalerts.took_bets(track.load(), [], DAY) == ({}, set())
    money.skip("vm1", DAY, undo=True)
    rows_ = fair.load_combos()
    rows_[0]["result"] = "win"
    store.save(fair.COMBOS_FILE, rows_)
    assert not xalerts.real_targets(today, track.load(), [], DAY, at(13)) and xalerts.took_bets(track.load(), [], DAY) == ({}, set())
    # alertes coupées
    C.XALERTS_ENABLED = False
    assert xalerts.on_refresh(today, at(13), got, up, fake_fetch(px)) is None
    C.XALERTS_ENABLED = True
    print("ok  5 ⚠️ combi sur mesure en danger (pari parti / sous la cote mini) avec « Vérifier ce combiné : Combi sur mesure (cote 5) », jamais 📈 💰 pour ses paris")


def test_alerts_real_picks_and_good_news():
    reset()
    money.set_daily(5000)
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    track.record(picks, set(), DAY, at(4))                           # paris suivis en silence : de quoi comparer à la clôture
    today = {"day": DAY, "picks": picks, "pronos": [], "picks_at": "09:30"}
    store.save("today.json", today)
    with Fakes():
        app.custom_combo(picks, [], 5.0, DAY, when="09:30")
        custom.track("m1", DAY)
    rows = track.load()
    took, noted = xalerts.took_bets(rows, [], DAY)
    assert set(took) == {f"e{i}|h2h||Home{i}" for i in range(1, 5)} and set(took.values()) == {XB} and noted == set(took), took
    # ✅ le marché a baissé de plus de 10 % depuis le matin (cote typique 1,60 -> 1,40) : tu as pris 1,50
    good = xalerts.good_items([pick(1, odds=1.40, prob=0.7, prob_book=0.7)], rows, took)
    assert len(good) == 1 and "tu as pris <b>1,50</b>, le marché est maintenant à <b>1,40</b>" in good[0]["text"], good
    # 💰 « joue maintenant » : annoncé pour un pari libre, jamais pour un pari déjà pris dans le combi sur mesure
    prev, moved = {"o": 1.9, "pb": 0.52}, pick(1, odds=1.55, prob=0.65, prob_book=0.65)
    reader = lambda: xalerts.Reader(fake_fetch({"e1": {("h2h", "HOME", None): 1.8}}))      # noqa: E731
    assert len(xalerts.lag_items([(0.3, moved, prev)], today, reader(), at(13), set())) == 1
    assert xalerts.lag_items([(0.3, moved, prev)], today, reader(), at(13), noted) == []
    # ⚠️ e2 n'est plus chez 1xBet : le danger part, avec le bouton « Vérifier » du combi sur mesure — s'il n'est pas encore joué
    px = {f"e{i}": {("h2h", "HOME", None): XB} for i in range(1, 5)} | {"e2": {("btts", "Yes", None): 1.8}}
    with Fakes() as f:
        assert xalerts.on_refresh(today, at(13), {"e1", "e2", "e3", "e4"}, [], fake_fetch(px)) is None, "ticket placé : pas d'alerte « en danger »"
    rows_ = fair.load_combos()
    rows_[0].pop("played", None)
    store.save(fair.COMBOS_FILE, rows_)
    store.save(xalerts.FILE, {})
    with Fakes() as f:
        msg = xalerts.on_refresh(today, at(13), {"e1", "e2", "e3", "e4"}, [], fake_fetch(px))
    assert msg and "Le combi sur mesure (cote 5) n'est plus jouable tel quel" in str(msg) and "Home2 – Away2" in str(msg), msg
    assert buttons(msg) == [f"xmaj|m1|{DAY}"]
    print("ok  5 combi sur mesure de paris réels : ✅ cote prise vs marché, ⚠️ pari parti de 1xBet (jambes enrichies de today.json)")


# ---------- 6. messages de /joue ----------

def test_names_everywhere():
    reset()
    money.set_daily(5000)
    morning_fair(pronos(4), tier=5, stake=500, cur="FCFA")
    keep_custom(pronos(3), stake=500, cur="FCFA", target=3.0, lo=3.0, hi=3.75)
    text = str(played.todo(DAY))
    assert "Combi sur mesure (cote 3)" in text and "Aux cotes 1xBet" in text, text
    t = played.find("vm1", DAY)
    assert t.label.startswith("combi sur mesure (cote 3) du ") and t.ref_word == "cote 1xBet" and t.stake == 500, t.label
    assert "combi sur mesure (cote 3)" in money.skip("vm1", DAY)
    print("ok  6 nom « Combi sur mesure (cote N) » dans Mes paris, /joue, ❌ Pas joué")


def main():
    test_button()
    test_no_button()
    test_tap_with_budget()
    test_tap_without_budget()
    test_tap_unfunded_and_gone()
    test_record_custom()
    test_command_route()
    test_skip()
    test_settle_and_bilan()
    test_settle_real_picks_and_unknown()
    test_no_auto_resend()
    test_refresh_custom()
    test_refresh_real_picks()
    test_real_picks_without_budget()
    test_alerts()
    test_alerts_real_picks_and_good_news()
    test_names_everywhere()
    print("tests combiné sur mesure suivi : OK")


if __name__ == "__main__":
    main()
