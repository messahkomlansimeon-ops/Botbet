"""Types de paris d'un combiné sur mesure : « combi 20 tirs fautes » et cases à cocher après « 🎯 Un combiné » -> cote (kelly/combos.py KINDS,
chat.parse_kinds, app.kinds_picker) ; le combiné ne prend que ces types, les garde une fois suivi (nom dans « Mes paris », « Vérifier ce combiné »
ne remplace un pari que par un du même type) et dit quand des types ne se mélangent pas (paris réels / pronostics). Hors ligne.
Lancer : python -m tests.test_combi_kinds"""
import re
from tests import test_combi_track as T     # noqa: I001 - fixe DATA_DIR et les faux réseaux avant d'importer kelly
from kelly import app, chat, combos as CB, custom, fair, fmt, played, store, telegram, xprice  # noqa: E402
from kelly.sources import xbet  # noqa: E402

DAY = T.DAY


def weak_legs(out):
    """Matchs (« FH12 – FA12 ») des jambes marquées 🟠 (moins sûres) dans un message de combiné, dans l'ordre du message."""
    return re.findall(r"<b>\d+\. ([^<]+)</b>[^\n]*\n[^\n]*\n🟠 ", str(out))


def fouls(i, **kw):
    """Pronostic 💎 « plus de 22,5 fautes » du match f<i> à 1,50 chez 1xBet."""
    return T.prono(i, id=f"f{i}", home=f"FH{i}", away=f"FA{i}", event=f"FH{i} - FA{i}", market="team_fouls_total", name="Over", point=22.5,
                   key=f"f{i}|team_fouls_total||22.5|Over", pick=fmt.label("team_fouls_total", "Over", 22.5, None, "soccer"), xodds=1.5,
                   xodds0=1.5, xgid=800 + i, xref=[800 + i, 20, 22.5], psh={}) | kw


def world():
    return T.pronos(4) + [fouls(i) for i in range(4)]


class Fakes:
    """Telegram, coupon et relecture en direct simulés : 1xBet redonne les pronostics du jour tels quels."""

    def __init__(self):
        self.sent, self.edits, self.prefix = [], [], []

    def __enter__(self):
        self.old = (xbet.save_coupon, telegram.send, telegram.edit_buttons, xprice.live_check, app._live_fair)
        xbet.save_coupon = lambda ls: "KC1"
        telegram.send = lambda text, *a, **k: (self.sent.append(text), True)[1]
        telegram.edit_buttons = lambda mid, markup, chat_id=None: (self.edits.append((mid, markup)), True)[1]
        xprice.live_check = lambda ids, now=None, fetch=None, fresh_min=None: (store.load("today.json", {})["pronos"], set(ids), "10:00")
        live = app._live_fair

        def spy(legs, lo, hi, prefix=None, *a, **k):
            self.prefix.append(prefix)
            return live(legs, lo, hi, prefix, *a, **k)
        app._live_fair = spy
        return self

    def __exit__(self, *a):
        xbet.save_coupon, telegram.send, telegram.edit_buttons, xprice.live_check, app._live_fair = self.old


def setup():
    T.reset()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": world()})


def test_parse():
    cases = {"tirs fautes": ["t", "f"], "tirs vers le but": ["t"], "tir vers le but et fautes": ["t", "f"], "tirs cadrés": ["tc"],
             "Tirs cadrés + corners": ["co", "tc"], "victoire": ["v"], "les 2 marquent": ["btts"], "double chance": ["dc"], "hors-jeu": ["hj"],
             "hors jeu": ["hj"], "buts, cartons": ["b", "ca"], "tout": [], "seulement les fautes": ["f"], "k": []}
    for txt, want in cases.items():
        assert chat.parse_kinds(txt.split()) == (want, []), (txt, chat.parse_kinds(txt.split()))
    assert chat.parse_kinds(["tirs", "blabla"]) == (["t"], ["blabla"])
    assert CB.kinds_prefix(["t", "f"]) == ("team_shots_", "team_fouls_") and CB.kinds_prefix([]) is None
    assert CB.kind_of("team_sot_home") == "tc" and CB.kind_of("team_shots_total") == "t" and CB.kind_of("alternate_totals_corners") == "co"
    assert CB.kind_of("totals") == "b" and CB.kind_of("h2h") == "v" and CB.kind_of("spreads") is None
    assert all(chat.parse_kinds([w]) == ([k], []) for k, w in chat.KIND_WORD.items()), "le bouton « Construire » relit ses propres mots"
    assert chat.parse_kinds([chat.KIND_WORD[k] for k in CB.KIND_CODES]) == (list(CB.KIND_CODES), []), "tous les mots ensemble (tirs + tirs-cadres)"
    print("ok  « combi 20 tirs fautes » : types lus (expressions, accents, mots de liaison)")


def test_command():
    setup()
    with Fakes() as f:
        out = app.handle_command("combi 5 fautes")
    assert "🎯 Paris choisis : fautes" in out and "Plus de 22,5 fautes" in out and "moins de 3,5 buts" not in out.lower(), out
    item = store.load(custom.FILE, {})["items"]["m1"]
    assert item["kinds"] == ["f"] and all(l["market"] == "team_fouls_total" for l in item["legs"]), item
    assert f.prefix == [("team_fouls_",)], "relu en direct : remplaçants du même type seulement"
    with Fakes():
        out = app.handle_command("/combi 5 buts")
    item = store.load(custom.FILE, {})["items"]["m2"]
    assert item["kinds"] == ["b"] and all(l["market"] == "totals" for l in item["legs"]) and "🎯 Paris choisis : buts" in out, item
    with Fakes():
        out = app.handle_command("combi 5")                # sans type : comme avant, rien de gardé
    assert "Paris choisis" not in out and "kinds" not in store.load(custom.FILE, {})["items"]["m3"]
    assert app.handle_command("combi 5 blabla").startswith("❓ « blabla » : type de pari inconnu.")
    with Fakes():
        out = app.handle_command("combi 500 fautes")
    assert out.startswith("❌ Pas de combiné à la cote 500 (fautes).") and "la cote monte au plus à 5,1" in out, out
    texts = [b["text"] for row in out.markup["inline_keyboard"] for b in row]
    assert T.buttons(out)[0] == "cgo|5|f|" and texts[0] == "🎯 Cote 5 (fautes)", (texts, T.buttons(out))      # la cote atteignable, mêmes types
    assert "cgo|500||" in T.buttons(out) and "🔄 Tous les types de paris" in texts, texts                              # ou d'autres types de paris
    print("ok  combiné bâti seulement sur les types choisis, types gardés, mot inconnu expliqué, échec qui garde les types")


def test_combi_auto():
    """« combi auto » : pas de cote à choisir, la cote visée vient des jambes du jour (plafond 10) ; sans jambe, une explication."""
    setup()
    with Fakes():
        out = app.handle_command("combi auto")
    assert "Pas de combiné" not in str(out) and "Pas assez de jambes" not in str(out) and "Je le joue" in str(T.buttons(out)) + str(out.markup), out
    with Fakes():
        out2 = app.handle_command("/combi auto fautes")
    assert "🎯 Paris choisis : fautes" in out2, out2
    store.save("today.json", dict(store.load("today.json", {}), picks=[], pronos=[]))
    r = app.handle_command("combi auto")
    assert str(r).startswith(("Pas assez de jambes jouables", "Kelly n'a pas encore analysé")), (r, "jour sans jambe")
    # pronostics chiffrés mais sans jambe jouable (rien ne bat le marché) : les paris réels du jour servent, comme pour « combi N » (16/10)
    picks = [T.pick(i, xodds=1.5) for i in range(1, 7)]
    dead = [p | {"strategy": "fill", "xodds": 1.01, "min_odds": 9.0} for p in T.pronos(4)]
    assert not CB.fair_legs(app.upcoming(dead)) and CB.make_pool(app.upcoming(picks))
    got = app.auto_args(picks, dead, [])
    assert got and float(got[0].replace(",", ".")) >= 1.5, got
    print("ok  « combi auto » : cote choisie par Kelly (types repris), explication sans jambe")


def test_picker_buttons():
    setup()
    msg = chat.on_callback("combi|20", 55)
    data = T.buttons(msg)
    assert "quels paris" in msg and data[0] == "ck|20||" and data[-1] == "cgo|20||", data
    texts = [b["text"] for row in msg.markup["inline_keyboard"] for b in row]
    assert texts[0] == "✅ Tout" and "⬜ Buts (4)" in texts and "⬜ Fautes (4)" in texts and not any("Corners" in t for t in texts), texts
    assert all(len(d.encode()) <= 64 for d in data)
    with Fakes() as f:
        assert chat.on_callback("ck|20|f", 55) is None
        texts = [b["text"] for row in f.edits[-1][1]["inline_keyboard"] for b in row]
        data = [b["callback_data"] for row in f.edits[-1][1]["inline_keyboard"] for b in row]
        assert f.edits[-1][0] == 55 and "✅ Fautes (4)" in texts and "⬜ Tout" in texts and "cgo|20|f|" in data and "ck|20||" in data, texts
        assert "ck|20|b.f|" in data, "cocher Buts en plus"
        out = chat.on_callback("cgo|5|f", 55)
    assert "🎯 Paris choisis : fautes" in out and store.load(custom.FILE, {})["items"]["m1"]["kinds"] == ["f"], out
    with Fakes():
        out = chat.on_callback("cgo|5|", 55)               # rien de coché : tous les paris
    assert "Paris choisis" not in out and "kinds" not in store.load(custom.FILE, {})["items"]["m2"]
    print("ok  cases à cocher : Tout / types du jour avec leur nombre de matchs, cocher refait les boutons, « Construire » bâtit")


def test_picker_hides_throws_and_offsides():
    setup()
    today = store.load("today.json", {})
    base = next(c for c in today["pronos"] if str(c["market"]).startswith("team_fouls_"))
    extra = []
    for i, m in enumerate(("team_throws_total", "team_offsides_total")):
        extra.append(dict(base, id=f"x{i}", key=f"x{i}|{m}||9.5|Over", market=m, xodds=None, xgid=1000 + i))       # pas encore ouverts chez 1xBet : d'habitude un ⏳
        extra.append(dict(base, id=f"y{i}", key=f"y{i}|{m}||9.5|Over", market=m))                                    # et des paris jouables
    today["pronos"] = today["pronos"] + extra
    store.save("today.json", today)
    for sel in ([], ["to", "hj", "f"]):
        text, mk = app.kinds_picker(20, sel)
        texts = [b["text"] for row in mk["inline_keyboard"] for b in row]
        data = [b["callback_data"] for row in mk["inline_keyboard"] for b in row]
        assert not any("Touches" in t or "Hors-jeu" in t for t in texts), texts
        assert not any(".to" in d or "to." in d or "|to|" in d or ".hj" in d or "hj." in d or "|hj|" in d for d in data), data
        assert "⬜ Fautes (4)" in texts or "✅ Fautes (4)" in texts, texts
    _, mk = app.kinds_picker(20, ["to", "f"])
    assert mk["inline_keyboard"][-1][0]["callback_data"] == "cgo|20|f|", "un code caché reçu d'un vieux bouton est écarté"
    print("ok  sélecteur : Touches et Hors-jeu retirés (cases, ⏳, codes des boutons, vieux boutons)")


def test_other_odds():
    setup()
    first = chat.combi_picker()
    assert "combi|autre" in T.buttons(first) and "combi|20" in T.buttons(first)
    assert chat.on_callback("combi|autre", 7).startswith("✏️ Écris ta cote")
    cmd, reply = chat.route("1 900")
    assert cmd is None and "Combiné à la cote 1900 : quels paris ?" in reply and T.buttons(reply)[-1] == "cgo|1900||", reply
    assert chat.get_pending() is None, "question refermée"
    chat.on_callback("combi|autre", 7)
    assert chat.route("0,5")[1].startswith("Écris juste la cote") and chat.get_pending()["what"] == chat.COMBI_ASK, "trop petite : redemandée"
    assert "à la cote 2,5" in chat.route("2,5")[1]
    print("ok  « ✏️ Autre cote » : la cote écrite (1 900) ouvre les cases à cocher")


def test_tracked_and_refresh():
    setup()
    with Fakes():
        out = app.handle_command("combi 5 fautes")
        custom.track("m1", DAY)
    row = fair.load_combos()[0]
    assert row["kinds"] == ["f"] and played.custom_name(row) == "Combi sur mesure fautes (cote 5)", row
    with Fakes() as f:
        app.refresh_combo("m1", DAY, now=T.at(13))
    assert f.prefix == [("team_fouls_",)], f.prefix
    print("ok  combiné suivi : types dans son nom, « Vérifier ce combiné » ne remplace que par le même type")


def test_split_note():
    setup()
    picks = [T.pick(i, xodds=1.5, xref=[9000 + i, 1, 0]) for i in range(1, 5)]
    with Fakes():
        out = app.custom_combo(picks, world(), 5.0, DAY, kinds=["v", "f"])
    assert "⚠️ Victoire et fautes ne vont pas dans le même combiné : demande-les à part." in out, out
    with Fakes():
        out = app.custom_combo(picks, world(), 5.0, DAY, kinds=["b", "f"])          # buts et fautes : tous deux dans les pronostics
    assert "ne vont pas dans le même combiné" not in out, out
    print("ok  victoire (paris réels) + fautes (pronostics) : la limite est dite")


def test_nlegs():
    """Nombre de paris d'un combi : « max 4 » / « 4 paris » / « ≤ 4 » (au plus), « pile 4 » (exactement) ; cases « Paris : libre · ≤ 3 · ≤ 5 · ≤ 8 »."""
    assert chat.parse_nlegs("tirs max 4".split()) == (4, False, ["tirs"]) and chat.parse_nlegs("pile 3 paris fautes".split()) == (3, True, ["fautes"])
    assert chat.parse_nlegs(["≤5"])[0] == 5 and chat.parse_nlegs("en 4 paris".split()) == (4, False, []) and chat.parse_nlegs(["tirs"]) == (None, False, ["tirs"])
    legs = [{"id": f"m{i}", "odds": 1.5 + i * 0.1, "prob": 0.7 - i * 0.02} for i in range(10)]
    assert len(CB.best_combo(legs, 5.0, 6.25, max_legs=2)[0]) == 2 and CB.best_combo(legs, 5.0, 6.25, max_legs=4, min_legs=4) is None
    assert len(CB.best_combo(legs, 7.0, 9.0, max_legs=4, min_legs=4)[0]) == 4
    assert round(CB.reach(legs, 3), 2) == round(2.4 * 2.3 * 2.2, 2)
    setup()
    pool = T.pronos(6) + [fouls(i) for i in range(6)]
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pool})
    with Fakes():
        out = app.handle_command("combi 5 pile 4")
    item = store.load(custom.FILE, {})["items"]["m1"]
    assert len(item["legs"]) == 4 and item["nmax"] == 4 and item["exact"], item
    with Fakes():
        out = app.handle_command("combi 2 max 2")
    assert len(store.load(custom.FILE, {})["items"]["m2"]["legs"]) == 2
    with Fakes():
        out = app.handle_command("combi 200 max 3")
    assert out.startswith("❌ Pas de combiné à la cote 200 en 3 paris au plus.") and "📉 Les bons paris du jour ont de petites cotes" in out and "Kelly accumule" not in out, out
    data = T.buttons(out)
    assert data[0].startswith("cgo|") and data[0].endswith("||3") and data[1] == "cgo|200||", data
    with Fakes():                                    # chaque bouton tient sa promesse : « 🎯 Cote X en 3 paris » donne bien un combiné
        ok = chat.on_callback(data[0], 9)
    assert "COMBINÉ" in str(ok) and len(store.load(custom.FILE, {})["items"]["m3"]["legs"]) <= 3, ok
    store.save(custom.FILE, {"day": DAY, "n": 2, "items": {k: v for k, v in store.load(custom.FILE, {})["items"].items() if k in ("m1", "m2")}})
    assert app.handle_command("combi 20 max 1").startswith("Un combiné a entre 2 et")
    text, mk = app.kinds_picker(20, ["f"], 5)
    rows = [[b["text"] for b in row] for row in mk["inline_keyboard"]]
    assert rows[-3] == ["Paris : libre", "≤ 3", "✅ ≤ 5", "≤ 8"] and rows[-2] == ["✏️ Autre nombre de paris"], rows
    assert mk["inline_keyboard"][-1][0]["callback_data"] == "cgo|20|f|5"
    # le bouton « 🎯 Cote X avec mes matchs » garde aussi la contrainte « pile N » (encodée « =N »), et le callback la relit
    with Fakes():
        fail = app.custom_combo([], app.upcoming(store.load("today.json", {})["pronos"]), 500.0, DAY, avoid={"m0"}, again="m1", nlegs=(3, True))
    btns = [b["callback_data"] for row in fail.markup["inline_keyboard"] for b in row]
    assert any(d == "cgo|500||=3" for d in btns), btns
    seen, old_cmd = [], app.handle_command
    app.handle_command = lambda cmd: seen.append(cmd) or "ok"
    try:
        chat.on_callback("cgo|500||=3", 9)
    finally:
        app.handle_command = old_cmd
    assert seen and "pile 3" in seen[0], seen
    # « ✏️ Autre nombre de paris » : le nombre écrit revient coché dans les cases
    assert chat.on_callback("cn|20|f", 9).startswith("✏️ Combien de paris ?")
    assert chat.route("1")[1].startswith("Écris un nombre de paris entre 2"), "trop petit : redemandé"
    _, back = chat.route("6")
    rows6 = [[b["text"] for b in row] for row in back.markup["inline_keyboard"]]
    assert rows6[-3] == ["✅ ≥ 6 (au moins)", "≤ 6 (au plus)"] and back.markup["inline_keyboard"][-1][0]["callback_data"] == "cgo|20|f|6+", rows6
    assert chat.get_pending() is None
    with Fakes():
        out = chat.on_callback("cgo|3|f|3", 9)
    item = store.load(custom.FILE, {})["items"]["m3"]
    assert item["nmax"] == 3 and not item["exact"] and len(item["legs"]) <= 3 and all(l["market"] == "team_fouls_total" for l in item["legs"]), item
    seen = {}
    old = app.custom_combo
    app.custom_combo = lambda *a, **k: seen.update(k) or "ok"
    try:
        app.another_combo("m3", DAY)
    finally:
        app.custom_combo = old
    assert seen["nlegs"] == (3, False), "« 🔀 Un autre » garde le nombre de paris"
    # au moins N (« min 4 », « au moins 4 », « ≥4 ») : jamais moins de N paris ; impossible : dit pourquoi
    assert chat.parse_nlegs("min 4".split())[:2] == (4, "min") and chat.parse_nlegs(["≥4"])[:2] == (4, "min") and chat.parse_nlegs("au moins 4".split())[:2] == (4, "min")
    assert chat.legs_bounds(4, "min") == (None, 4) and chat.legs_bounds(4, True) == (4, 4) and chat.legs_bounds(4, False) == (4, None)
    setup()
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pool})
    with Fakes():
        out = app.handle_command("combi 5 min 4")
    item = store.load(custom.FILE, {})["items"]["m1"]
    assert len(item["legs"]) >= 4 and item["exact"] == "min" and 5.0 <= item["total"] <= 6.25, item
    with Fakes():
        out = app.handle_command("combi 2 min 6")                  # 6 paris dépassent déjà 2,5 : les 6 paris quand même, la plus petite cote dite
    assert "🧩 6 paris (au moins 6 demandés) : en 6 paris, même les plus sûrs, la cote ne descend pas sous" in str(out), out
    items = store.load(custom.FILE, {})["items"]
    assert len(items[max(items, key=lambda k: int(k[1:]))]["legs"]) == 6
    with Fakes():
        out = app.handle_command("combi 50 min 20")
    assert "Seulement 12 matchs avec un bon pari" in out and "il en faut 20" in out, out
    _, mk = app.kinds_picker(20, [], "15+")
    rows = [[b["text"] for b in row] for row in mk["inline_keyboard"]]
    assert rows[-3] == ["✅ ≥ 15 (au moins)", "≤ 15 (au plus)"] and mk["inline_keyboard"][-1][0]["callback_data"] == "cgo|20||15+", rows
    with Fakes():
        out = chat.on_callback("cgo|5||4+", 9)
    items = store.load(custom.FILE, {})["items"]
    assert items[max(items, key=lambda k: int(k[1:]))]["exact"] == "min", ("le bouton « ≥ » donne « au moins »", items.keys())
    print("ok  nombre de paris : au plus / pile / au moins, texte et cases à cocher, échecs expliqués, « 🔀 » le garde")


def test_when_note():
    """L'heure des cotes d'un combiné : la plus récente, et les cotes plus anciennes nommées (pas « Cotes 1xBet de 05:01 » pour 3 cotes relues à 07:08)."""
    from datetime import datetime, timezone
    old = app._leg_time
    at = {"f0": "07:08", "f1": "07:09", "f2": "07:08", "f3": "05:01"}
    app._leg_time = lambda c: datetime(2030, 1, 1, *map(int, at[c["id"]].split(":")), tzinfo=timezone.utc).timestamp()
    try:
        when, note = app._when_note([fouls(i) for i in range(4)])
        assert when == "07:09" and note == "\n⚠️ 1 cote pas relue depuis 05h01 : FH3 – FA3 — vérifie-la" and "FH0" not in note, (when, note)
        assert app._when_note([fouls(i) for i in range(3)]) == ("07:09", "")
        at.update(f0="05:01", f2="05:02")
        _, note = app._when_note([fouls(i) for i in range(4)])
        assert note.startswith("\n⚠️ 3 cotes pas relues depuis 05h01 : ") and "FH0 – FA0" in note and "FH1" not in note and note.endswith(" — vérifie-les") and "…" not in note, note
    finally:
        app._leg_time = old
    assert app._when_note([fouls(0)]) == (None, "")
    print("ok  heure des cotes : la plus récente, et les cotes pas relues nommées")


def test_open_stats_and_hint():
    """Matchs dont 1xBet n'a pas encore ouvert les paris tirs : relus en direct s'ils sont dans la fenêtre des stats, et le message d'échec le dit."""
    from datetime import timedelta
    from kelly import config as C
    setup()
    now = T.at(10)
    shots = [T.prono(10 + i, id=f"s{i}", market="team_shots_total", name="Over", point=24.5, key=f"s{i}|team_shots_total||24.5|Over",
                     pick="Plus de 24,5 tirs", xodds=None, xref=None, xev=None, psh={},
                     commence=(now + timedelta(hours=2 if i < 3 else 6)).strftime("%Y-%m-%dT%H:%M:00Z")) for i in range(5)]
    store.save("today.json", {"day": DAY, "picks": [], "pronos": world() + shots})
    seen = []
    old = xprice.live_check
    xprice.live_check = lambda ids, now=None, fetch=None, fresh_min=None: (seen.append(set(ids)), (store.load("today.json", {})["pronos"], set(ids), "10:00"))[1]
    try:
        with Fakes() as f:
            pass
        telegram.send = lambda text, *a, **k: f.sent.append(text) or True
        out = app.custom_combo([], world() + shots, 50.0, DAY, kinds=["t"], now=now)
    finally:
        xprice.live_check = old
        f.__exit__()
    assert seen == [{"s0", "s1", "s2", "s3", "s4"}], "les matchs de la fenêtre des stats (45 min à 24 h) sont relus"
    assert any("viennent d'ouvrir" in m for m in f.sent), f.sent
    assert "⏳ Paris tirs vers le but pas encore ouverts chez 1xBet sur 5 matchs : redemande plus près des matchs (3 à 12h00)." in out, out
    # hors types stats (victoire) : aucune relecture
    seen.clear()
    assert app._open_stats(world(), CB.kinds_prefix(["v"]), now) == world() and not seen
    C.COMBI_OPEN_MAX = 10
    print("ok  paris stats pas encore ouverts : relus en direct dans la fenêtre, et l'heure où redemander est dite")


def test_bold():
    setup()
    weak = [fouls(10 + i, id=f"w{i}", key=f"w{i}|team_fouls_total||22.5|Over", prob=0.55, xodds=1.9) for i in range(3)]
    pool = world() + weak
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pool})
    with Fakes():
        out = app.custom_combo([], pool, 30.0, DAY, kinds=["f"])
    texts = [b["text"] for row in out.markup["inline_keyboard"] for b in row]
    assert ("🎲 Plus risqué, même cote", "cplus|30|f") in zip(texts, T.buttons(out)) and sum(d.startswith("cplus|") for d in T.buttons(out)) == 1, (texts, T.buttons(out))
    with Fakes():
        out = chat.on_callback("cplus|30|f", 9)
    assert out.startswith("<b>🎲 COMBINÉ AUDACIEUX — cote ") and "🎯 Paris choisis : fautes" in out, out
    # les paris moins solides : comptés sous le titre, marqués 🟠 dans la liste (plus nommés en tête), légende en pied
    assert ("\n<i>🎰 Ticket loterie : 1 chance sur 56 selon 1xBet. En moyenne il rend 38 % de moins que la mise : 100 FCFA au plus.</i>\n"
            "📈 Espérance du combiné : +8,4 % (valeur 7/7, remplissage 0/7)\n"
            "🟠 3 paris moins sûrs pour atteindre la cote\n") in out, out                 # 3 % selon Kelly : ticket loterie, dit une seule fois
    assert sorted(weak_legs(out)) == ["FH10 – FA10", "FH11 – FA11", "FH12 – FA12"] and "<i>🟢 solide · 🟠 moins sûr</i>" in out, out
    assert T.buttons(out)[0].startswith("cjoue|"), out
    item = store.load(custom.FILE, {})["items"]["m1"]
    assert item["kinds"] == ["f"] and 30.0 <= item["total"] <= 37.5, item
    with Fakes():
        reused = app.handle_command("combi 5000 fautes audacieux")
    assert reused.startswith("❌ Pas de combiné à la cote 5000 (fautes), même avec des paris plus risqués.\n📉 Au plus"), reused      # les jambes déjà envoyées aujourd'hui (plafond par jambe) sont écartées
    store.save(custom.FILE, {"day": DAY, "n": 0, "items": {}})
    with Fakes():
        out = app.handle_command("combi 5000 fautes audacieux")
    assert out.startswith("❌ Pas de combiné à la cote 5000 (fautes), même avec des paris plus risqués.\n📉 Au plus 34,7."), out
    with Fakes():
        out = app.custom_combo([], world(), 5.0, DAY, kinds=["f"], bold=True)      # les solides suffisent : dit tel quel
    assert "Tous ses paris sont solides" in out, out
    # au moins N, plus de N que de matchs solides : le trou est comblé avec les paris les plus probables après les solides, nommés
    solid = {c["id"] for c in CB.fair_legs(world()) if c["market"].startswith(CB.kinds_prefix(["f"]))}
    n = len(solid) + 2
    fill = [c | {"prob": 0.62, "xodds": 1.55, "min_odds": 1.69} for c in weak]       # bouche-trous : probables (60 % au moins) mais mal payés
    store.save("today.json", {"day": DAY, "picks": [], "pronos": world() + fill})
    with Fakes():
        out = app.custom_combo([], world() + fill, 2.0, DAY, kinds=["f"], nlegs=(n, "min"))
    assert f"🧩 {n} paris (au moins {n} demandés)" in str(out) and "🟠 2 paris moins sûrs pour compléter" in str(out), out
    assert len(weak_legs(out)) == 2 and str(out).count("🟠 <i>Kelly 62 %") == 2 and str(out).count(" · mal payé</i>") == 2, out
    items = store.load(custom.FILE, {})["items"]
    assert len(items[max(items, key=lambda k: int(k[1:]))]["legs"]) == n
    assert app._chances(0.0005) == "0,05 % (environ 1 chance sur 2 000)" and app._chances(0.2) == "20 %"
    # données trop maigres (3 matchs, qualité 20 %) : jamais dans un combiné audacieux, même à 68 % (le 10/10 : corners Paraguay / Mexique à 30 % chez 1xBet)
    thin = [fouls(20 + i, id=f"t{i}", key=f"t{i}|team_fouls_total||22.5|Over", prob=0.68, xodds=2.9, conf=0.2, n_min=3) for i in range(4)]
    mine, _ = app._bold_pools([], world() + thin, CB.kinds_prefix(["f"]))
    assert not {c["id"] for c in mine} & {"t0", "t1", "t2", "t3"} and {c["id"] for c in mine} >= {"f0", "f1"}, [c["id"] for c in mine]
    print("ok  « 🎲 Aller plus loin quand même » : paris moins solides marqués 🟠, chances et loterie dites, suivi comme un combi")


def test_force_and_proches():
    """L'utilisateur peut choisir de forcer pour dénicher les événements les plus proches statistiquement."""
    rest = ["fautes", "forcer"]
    bold = any(played.norm(w) in chat.BOLD_WORDS for w in rest)
    kinds, unknown = chat.parse_kinds([w for w in rest if played.norm(w) not in chat.BOLD_WORDS])
    assert bold and kinds == ["f"] and not unknown
    assert "proche" in chat.BOLD_WORDS and "proches" in chat.BOLD_WORDS and "force" in chat.BOLD_WORDS
    store.save(custom.FILE, {"day": DAY, "n": 0, "items": {}})          # les jambes déjà envoyées aujourd'hui sont plafonnées : on repart d'une journée vide
    weak = [fouls(10 + i, id=f"w{i}", key=f"w{i}|team_fouls_total||22.5|Over", prob=0.55, xodds=1.9) for i in range(3)]
    pool = world() + weak
    store.save("today.json", {"day": DAY, "picks": [], "pronos": pool})
    with Fakes():
        out = app.handle_command("combi 30 fautes forcer")
    assert "<b>🎲 COMBINÉ AUDACIEUX — cote " in out and "🎯 Paris choisis : fautes" in out, out
    assert sorted(weak_legs(out)) == ["FH10 – FA10", "FH11 – FA11", "FH12 – FA12"] and "🟠 3 paris moins sûrs pour atteindre la cote" in out, out
    store.save(custom.FILE, {"day": DAY, "n": 0, "items": {}})
    with Fakes():
        out2 = chat.on_callback("cforce|30|f", 9)
    assert "<b>🎲 COMBINÉ AUDACIEUX — cote " in out2, out2
    print("ok  « forcer » et « proche » : commande libre et callback cforce pour dénicher les événements proches")


def main():
    test_parse()
    test_command()
    test_combi_auto()
    test_picker_buttons()
    test_picker_hides_throws_and_offsides()
    test_other_odds()
    test_tracked_and_refresh()
    test_split_note()
    test_nlegs()
    test_when_note()
    test_open_stats_and_hint()
    test_bold()
    test_force_and_proches()
    print("tests types de paris des combinés : OK")


if __name__ == "__main__":
    main()

