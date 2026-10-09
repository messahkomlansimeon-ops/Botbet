"""Cotes 1xBet (kelly/sources/xbet.py) et choix de la ligne à la cote réelle (kelly/xprice.py), hors ligne : réponses de l'API au
format réel (marché principal AU PREMIER NIVEAU, sous-matchs statistiques typés), paramètres triés, rapprochement des matchs,
sous-matchs demandés seulement pour les familles voulues, meilleure ligne, pronostic écarté si 1xBet paie trop peu, affichage.
Lancer : python -m tests.test_xbet"""
import json
import os
import tempfile
from urllib.parse import parse_qsl, urlparse

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-xbet-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-xbet-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-xbet-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ["XBET_PROXY_FILE"] = os.path.join(os.environ["DATA_DIR"], "pas-de-fichier.env")      # jamais le vrai proxy de la machine
os.environ["XBET_PROXY"] = "http://user:secret@proxy.test:80"

from kelly import calib, combos as CB, config as C, fmt, store, xprice  # noqa: E402
from kelly.sources import http, state, xbet  # noqa: E402

KO = 1791572400                                     # 2026-10-09 19:00 UTC
EV = {"id": "e1", "home_team": "Málaga", "away_team": "Espanyol", "commence_time": "2026-10-09T19:00:00Z"}


def cell(t, cf, p=None):
    return [{"type": t, "cf": cf, "cfView": str(cf)} | ({"parameter": p} if p is not None else {})]


MENU = [{"menuSectionId": 2, "sports": [{"id": 1, "nameEng": "Football", "ligas": [
    {"id": 71, "nameEng": "Spain", "subLigas": [{"id": 127733, "nameEng": "Spain. La Liga"},
                                               {"id": 1700000, "nameEng": "Spain. La Liga. Statistics Round"}]},
    {"id": 118587, "nameEng": "UEFA Champions League", "globalChampId": 105}]}]}]
GAMES = [{"id": 900, "mainGameId": 900, "startTs": KO, "sport": {"id": 1},
          "opponent1": {"fullName": "Málaga"}, "opponent2": {"fullName": "Espanyol"}},
         {"id": 901, "mainGameId": 901, "startTs": KO, "sport": {"id": 1},
          "opponent1": {"fullName": "Home (Special bets)"}, "opponent2": {"fullName": "Away (Special bets)"}},
         {"id": 902, "mainGameId": 777, "startTs": KO, "sport": {"id": 1}, "opponent1": {"fullName": "x"}, "opponent2": {"fullName": "y"}}]
MAIN = {"eventGroups": [{"groupId": 1, "events": [cell(1, 2.878), cell(2, 3.295), cell(3, 2.745)]},
                        {"groupId": 8, "events": [cell(4, 1.512), cell(5, 1.385), cell(6, 1.474)]},
                        {"groupId": 19, "events": [cell(180, 1.79), cell(181, 1.937)]},
                        {"groupId": 17, "events": [cell(9, 2.167, 2.5) + cell(10, 1.806, 2.5), cell(9, 1.33, 1.5) + cell(10, 3.3, 1.5)]}],
        # la 1re mi-temps (premier sous-match) a d'autres cotes : elles ne doivent JAMAIS être prises pour celles du match
        "subGamesForMainGame": [{"id": 950, "period": 1, "subGameName": "1ère mi-temps",
                                 "eventGroups": [{"groupId": 1, "events": [cell(1, 3.52), cell(2, 2.0), cell(3, 3.32)]}]},
                                {"id": 951, "gameTypeId": 2, "subGameName": "Corners"},
                                {"id": 952, "gameTypeId": 2, "period": 1, "subGameName": "Corners. 1ère mi-temps"},
                                {"id": 953, "gameTypeId": 12, "subGameName": "Tirs Cadrés"},
                                {"id": 954, "gameTypeId": 95, "subGameName": "Tirs vers le but"}]}
CORNERS = {"eventGroups": [{"groupId": 17, "events": [cell(9, 1.59, 8.5) + cell(10, 2.2, 8.5), cell(9, 1.99, 9.5) + cell(10, 1.8, 9.5),
                                                      cell(9, 2.6, 10.5) + cell(10, 1.45, 10.5)]},
                           {"groupId": 15, "events": [cell(11, 1.65, 4.5)]}]}
SOT = {"eventGroups": [{"groupId": 17, "events": [cell(9, 1.81, 8.5) + cell(10, 1.95, 8.5)]},
                       {"groupId": 15, "events": [cell(11, 2.16, 4.5) + cell(12, 1.65, 4.5)]},
                       {"groupId": 62, "events": [cell(13, 1.95, 4.5) + cell(14, 1.8, 4.5)]}]}


class FakeNet:
    def __init__(self):
        self.calls = []

    def fetch(self, url, source, ttl, headers=None, check_robots=True):
        self.calls.append(url)
        q = dict(parse_qsl(urlparse(url).query))
        keys = [k for k, _ in parse_qsl(urlparse(url).query)]
        assert keys == sorted(keys), f"paramètres non triés : {url}"         # sinon HTTP 400 chez 1xBet
        ep = urlparse(url).path.rsplit("/", 1)[-1]
        body = {"leftMenuSports": MENU, "games1x2": GAMES}.get(ep)
        if ep == "gameEvents":
            body = {"900": MAIN, "951": CORNERS, "953": SOT}.get(q["gameId"], {"eventGroups": []})
        return http.Result(url, json.dumps(body).encode(), 200)


def with_net(fn):
    old = http.fetch
    net = FakeNet()
    http.fetch = net.fetch
    try:
        return fn(net)
    finally:
        http.fetch = old


def test_parse_and_url():
    u = xbet.url("gameEvents", gameId=5, cfView=3)
    keys = [k for k, _ in parse_qsl(urlparse(u).query)]
    assert keys == sorted(keys) and "selectedMs" not in keys
    px, subs = xbet.parse_main(MAIN, "Málaga", "Espanyol")
    assert px[("h2h", "Málaga", None)] == 2.878 and px[("h2h", "Draw", None)] == 3.295          # pas la 1re mi-temps (3,52 / 2,00)
    assert px[("double_chance", "Málaga or Draw", None)] == 1.512 and px[("double_chance", "Draw or Espanyol", None)] == 1.474
    assert px[("totals", "Over", 2.5)] == 2.167 and px[("totals", "Under", 1.5)] == 3.3 and px[("btts", "No", None)] == 1.937
    assert subs == {2: 951, 12: 953, 95: 954}                                                  # sous-matchs du match entier seulement
    out = {}
    xbet._ladder(SOT["eventGroups"], *xbet.SUBGAMES[12], out)
    assert out[("team_sot_total", "Over", 8.5)] == 1.81 and out[("team_sot_home", "Under", 4.5)] == 1.65
    assert out[("team_sot_away", "Over", 4.5)] == 1.95
    assert xbet.book_prob(out, "team_sot_total", "Over", 8.5) == round((1 / 1.81) / (1 / 1.81 + 1 / 1.95), 4)
    assert [g["gid"] for g in xbet.parse_games(GAMES)] == [900, 901]                           # pas les sous-matchs (902)
    print("ok  lecture des cotes et adresses triées")


def test_find_match():
    cands = [{"gid": 1, "home": "Málaga", "away": "Espanyol", "ts": KO},
             {"gid": 2, "home": "Real Sociedad", "away": "Espanyol", "ts": KO + 3 * 86400}]
    assert xbet.find(EV, cands)["gid"] == 1
    naples = {"id": "n", "home_team": "Napoli", "away_team": "Juventus", "commence_time": "2026-10-09T19:00:00Z"}
    assert xbet.find(naples, [{"gid": 3, "home": "SSC Naples", "away": "Juventus", "ts": KO}])["gid"] == 3      # même heure
    assert xbet.find(naples, [{"gid": 4, "home": "SSC Naples", "away": "Juventus", "ts": KO + 7200}]) is None   # 2 h d'écart
    assert xbet.find(EV, [{"gid": 5, "home": "Malmö", "away": "Elfsborg", "ts": KO}]) is None
    # noms très différents chez 1xBet : même heure, mots qui se recoupent, un seul match possible
    sjk = {"id": "s", "home_team": "SJK Seinäjoki", "away_team": "FC Lahti", "commence_time": "2026-10-09T19:00:00Z"}
    ald = {"id": "a", "home_team": "Aldosivi Mar del Plata", "away_team": "Sarmiento de Junin", "commence_time": "2026-10-09T19:00:00Z"}
    pool = [{"gid": 6, "home": "Seinäjoen Jalkapallokerho", "away": "Lahti", "ts": KO},
            {"gid": 7, "home": "CAAldosivi", "away": "Club Atletico Sarmiento Junin", "ts": KO},
            {"gid": 8, "home": "à domicile", "away": "à l'extérieur", "ts": KO}]
    assert xbet.find(sjk, pool)["gid"] == 6 and xbet.find(ald, pool)["gid"] == 7
    assert xbet.find({"id": "r", "home_team": "Racing Club", "away_team": "Atletico Tucuman", "commence_time": EV["commence_time"]},
                     [{"gid": 9, "home": "Club Atletico Sarmiento", "away": "Atletico Mitre", "ts": KO}]) is None   # mots génériques
    assert xbet.find(ald, [pool[1], pool[1] | {"gid": 10}]) is None                       # deux candidats : on ne devine pas
    print("ok  rapprochement des matchs (noms + heure)")


def test_fetch_day_requests():
    store.save(state.FILE, {})

    def run(net):
        got = xbet.fetch_day([("soccer_spain_la_liga", EV), ("soccer_epl", EV | {"id": "e2"}), ("tennis_x", EV | {"id": "e3"})],
                             wanted={"e1": {"alternate_totals_corners", "totals"}})
        return got, net.calls
    got, calls = with_net(run)
    assert set(got) == {"e1"} and got["e1"]["gid"] == 900, got
    eps = [urlparse(u).path.rsplit("/", 1)[-1] + ("#" + dict(parse_qsl(urlparse(u).query)).get("gameId", "")) for u in calls]
    # menu, liste de la Liga, marché principal, corners ; pas de tirs cadrés (non voulus) ; Premier League absente du menu : rien
    assert eps == ["leftMenuSports#", "games1x2#", "gameEvents#900", "gameEvents#951"], eps
    assert got["e1"]["px"][("alternate_totals_corners", "Over", 9.5)] == 1.99
    assert state.health("xbet")["rows"] == 1
    print("ok  requêtes limitées aux familles voulues")


def test_proxy_cap_and_reuse():
    """Proxy obligatoire (jamais en direct), plafond mensuel, id 1xBet du matin relu sans menu ni liste, données comptées."""
    store.save(state.FILE, {})
    store.save(xbet.USAGE, {})
    saved = os.environ.pop("XBET_PROXY")
    try:
        calls = with_net(lambda net: (xbet.fetch_day([("soccer_spain_la_liga", EV)]), net.calls)[1])
        assert calls == [] and "proxy introuvable" in state.health("xbet")["last_error"]
        with open(C.XBET_PROXY_FILE, "w") as f:
            f.write("SOFA_PROXY=http://a:b@x:80\nXBET_PROXY=http://u:p@from-file:80\n")
        assert xbet.proxy() == "http://u:p@from-file:80"
    finally:
        os.environ["XBET_PROXY"] = saved
        os.remove(C.XBET_PROXY_FILE)
    assert xbet.proxy() == saved
    # id 1xBet déjà connu : seulement le marché principal (pas de menu, pas de liste du championnat)
    http.WIRE[xbet.DOMAIN] = {"bytes": 0, "requests": 0}

    def run(net):
        def counted(*a, **k):
            http.WIRE[xbet.DOMAIN]["bytes"] += 7000
            http.WIRE[xbet.DOMAIN]["requests"] += 1
            return FakeNet.fetch(net, *a, **k)
        old, http.fetch = http.fetch, counted
        try:
            got = xbet.fetch_day([("soccer_spain_la_liga", EV | {"xgid": 900})])
        finally:
            http.fetch = old
        return got, net.calls
    got, calls = with_net(run)
    assert set(got) == {"e1"} and [urlparse(u).path.rsplit("/", 1)[-1] for u in calls] == ["gameEvents"], calls
    assert "marketType=2" in calls[0] and http.PROXIES[xbet.DOMAIN] == saved
    u = xbet.usage()
    assert u["bytes"] == 7000 and u["requests"] == 1 and sum(u["days"].values()) == 7000
    # plafond du mois atteint : aucun relevé
    store.save(xbet.USAGE, u | {"bytes": int(C.XBET_MONTH_MB * 1e6)})
    assert with_net(lambda net: (xbet.fetch_day([("soccer_spain_la_liga", EV)]), net.calls)[1]) == []
    assert "plafond mensuel" in state.health("xbet")["last_error"]
    store.save(xbet.USAGE, {})
    print("ok  proxy obligatoire, plafond mensuel, id du matin relu directement")


def prono(market, name, point, p0, alts=None, **kw):
    c = {"id": "e1", "sport_key": "soccer_spain_la_liga", "home": "Málaga", "away": "Espanyol", "commence": EV["commence_time"],
         "start": "19:00", "league": "Liga", "market": market, "name": name, "point": point, "who": None, "prob": p0, "p0": p0,
         "min_odds": round(1 / p0 * 1.05, 2), "conf": 0.6, "n_min": 8, "score": 0.2, "key": f"e1|{market}||{point}|{name}",
         "pick": fmt.label(market, name, point, None, "soccer")}
    if alts is not None:
        c["alts"] = alts
    return c | kw


def test_choose_line_and_viability():
    store.save(calib.FILE, {})
    calib._memo.clear()
    px = {}
    xbet._ladder(CORNERS["eventGroups"], *xbet.SUBGAMES[2], px)
    # Kelly visait plus de 8,5 (68 %, payé 1,59 < mini 1,54 ? non : 1,59 >= 1,54) ; plus de 9,5 (60 %) payé 1,99 vaut mieux
    c = prono("alternate_totals_corners", "Over", 8.5, 0.68,
              alts=[["Over", 8.5, 0.68, None], ["Over", 9.5, 0.60, 0.52], ["Over", 10.5, 0.45, None], ["Under", 9.5, 0.40, None]])
    assert xprice.price(c, px) == "value"
    assert (c["name"], c["point"], c["xodds"]) == ("Over", 9.5, 1.99) and c["key"].endswith("|9.5|Over") and "9,5" in c["pick"]
    assert c["prob"] == 0.6 and c["min_odds"] == round(1 / 0.6 * 1.05, 2) and abs(c["xev"] - (0.6 * 1.99 - 1)) < 1e-4
    assert c["xp"] == xbet.book_prob(px, "alternate_totals_corners", "Over", 9.5) and CB.viable(c)
    # 1xBet paie trop peu : pronostic gardé pour mémoire mais plus viable (ni montré ni dans un combiné)
    low = prono("alternate_totals_corners", "Under", 10.5, 0.72, alts=[["Under", 10.5, 0.72, None]])
    assert xprice.price(low, px) == "low" and low["xodds"] == 1.45 and not CB.viable(low)
    # marché que 1xBet ne propose pas : price() ne trouve rien (apply() le marque alors « absent de 1xBet », plus viable)
    none = prono("team_fouls_total", "Over", 22.5, 0.66)
    assert xprice.price(none, px) is None and "xodds" not in none and CB.viable(none)
    assert not CB.viable(none | {"xnone": 1})
    absent = prono("team_fouls_total", "Over", 22.5, 0.66)                                  # même match (e1), trouvé chez 1xBet
    assert xprice.apply([absent], fetch=lambda *a, **k: {"e1": {"gid": 900, "px": px}})["none"] == 1
    assert absent["xnone"] == 1 and absent["xgid"] == 900 and not CB.viable(absent)
    other = none | {"id": "e9"}
    assert CB.best_per_match([none, c, other]) == [c], "cotes 1xBet du jour connues : seulement les pronostics à leur cote 1xBet"
    assert [l["odds"] for l in CB.fair_legs([c, other])] == [1.99], "aucune cote inventée dans un combiné"
    assert [l["odds"] for l in CB.fair_legs([other])] == [other["min_odds"]], "relevé en panne : « à vérifier » comme avant"
    assert CB.best_per_match([other]) == [other]
    print("ok  meilleure ligne à la cote 1xBet, pronostic mal payé écarté")


def test_apply_and_messages():
    store.save(calib.FILE, {})
    calib._memo.clear()
    px = {}
    xbet._ladder(CORNERS["eventGroups"], *xbet.SUBGAMES[2], px)
    pronos = [prono("alternate_totals_corners", "Over", 9.5, 0.60, alts=[["Over", 9.5, 0.60, None]]),
              prono("team_fouls_total", "Over", 22.5, 0.66) | {"id": "e2", "home": "A", "away": "B"}]
    seen = {}

    def fetch(events, deadline=None, wanted=None):
        seen.update(events=[e["id"] for _, e in events], wanted=wanted)
        return {"e1": {"gid": 900, "px": px}}
    assert xprice.apply(pronos, fetch=fetch) == {"matches": 1, "value": 1, "low": 0, "none": 0}
    assert seen["events"] == ["e1", "e2"] and seen["wanted"]["e1"] == {"alternate_totals_corners"}
    text = fmt.pronos(CB.best_per_match(pronos))
    assert "💎 Plus de 9,5 corners — cote 1xBet <b>1,99</b>" in text and "valeur +19 %" in text, text
    assert "— mini <b>" not in text and "« mini »" not in text, "le pronostic sans cote 1xBet n'est pas montré"
    real = fmt.fair_combo(3.96, [pronos[0], pronos[0] | {"id": "x"}], 0.36, 0.5)
    assert real.startswith("<b>💎 COMBINÉ AUX COTES 1XBET</b>") and "🎯 Cote <b>" in real and "✔️ Cote 1xBet <b>1,99</b>" in real
    mixed = fmt.fair_combo(3.0, [pronos[0], pronos[1]], 0.4, 0.5)
    assert "À VÉRIFIER CHEZ 1XBET" in mixed and "Kelly n'a pas vu toutes les cotes" in mixed
    old = C.XBET_ENABLED
    C.XBET_ENABLED = False
    try:
        assert xprice.apply(pronos, fetch=lambda *a, **k: 1 / 0)["matches"] == 0
    finally:
        C.XBET_ENABLED = old
    assert xprice.apply(pronos, fetch=lambda *a, **k: 1 / 0)["matches"] == 0              # panne : le matin continue
    print("ok  relevé du matin, messages 💎 et combinés aux cotes 1xBet")


def test_afternoon_refresh():
    """15:00 : match commencé ignoré, 💎 du matin pas renvoyé, nouveau 💎 envoyé et suivi, match non retrouvé : cotes du matin gardées."""
    from datetime import datetime, timezone
    from kelly import app, fair, telegram
    store.save(calib.FILE, {})
    calib._memo.clear()
    store.save("fair.json", [])
    px = {}
    xbet._ladder(CORNERS["eventGroups"], *xbet.SUBGAMES[2], px)
    started = prono("alternate_totals_corners", "Over", 9.5, 0.60, alts=[["Over", 9.5, 0.60, None]]) | {
        "id": "s", "commence": "2026-10-09T14:00:00Z"}
    kept = prono("alternate_totals_corners", "Over", 9.5, 0.60, alts=[["Over", 9.5, 0.60, None]]) | {
        "id": "k", "xodds": 1.99, "xev": 0.194, "xp": 0.47}
    fresh = prono("alternate_totals_corners", "Over", 8.5, 0.68, alts=[["Over", 8.5, 0.68, None]]) | {"id": "f"}
    gone = prono("team_fouls_total", "Over", 22.5, 0.66) | {"id": "g", "xodds": 1.9, "xev": 0.25}
    store.save("today.json", {"day": "2026-10-09", "picks": [], "pronos": [started, kept, fresh, gone]})
    asked, sent = {}, []

    def fetch(events, deadline=None, wanted=None):
        asked["ids"] = sorted(e["id"] for _, e in events)
        return {"k": {"gid": 1, "px": px}, "f": {"gid": 2, "px": px}}          # « g » non retrouvé cet après-midi
    old = telegram.send
    telegram.send = lambda text, *a, **k: sent.append(text)
    try:
        n = app.run_xbet_refresh(now=datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc), fetch=fetch)
    finally:
        telegram.send = old
    assert asked["ids"] == ["f", "g", "k"], asked                               # le match commencé n'est pas relu
    assert n == 1 and len(sent) == 1 and "Nouvelles cotes 1xBet" in sent[0] and "8,5 corners — cote 1xBet <b>1,59</b>" in sent[0]
    assert "9,5" not in sent[0], "le 💎 du matin n'est pas renvoyé"
    saved = {c["id"]: c for c in store.load("today.json", {})["pronos"]}
    assert saved["f"]["xodds"] == 1.59 and saved["g"]["xodds"] == 1.9 and "xodds" not in saved["s"]
    assert [r["id"] for r in fair.load()] == ["f"] and fair.load()[0]["xodds"] == 1.59
    # rien de nouveau : aucun message
    sent.clear()
    telegram.send = lambda text, *a, **k: sent.append(text)
    try:
        assert app.run_xbet_refresh(now=datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc), fetch=fetch) == 0 and not sent
        # pas de pronostics du jour Kelly (matin pas encore passé) : rien
        assert app.run_xbet_refresh(now=datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc), fetch=fetch) == 0 and not sent
    finally:
        telegram.send = old
    print("ok  relevé 1xBet de l'après-midi")


def main():
    test_parse_and_url()
    test_find_match()
    test_fetch_day_requests()
    test_proxy_cap_and_reuse()
    test_choose_line_and_viability()
    test_apply_and_messages()
    test_afternoon_refresh()
    print("tests 1xBet : OK")


if __name__ == "__main__":
    main()
