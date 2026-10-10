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

from kelly import calib, combos as CB, config as C, fair, fmt, store, xprice  # noqa: E402
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
MAIN = {"constId": 369147714, "eventGroups": [{"groupId": 1, "events": [cell(1, 2.878), cell(2, 3.295), cell(3, 2.745)]},
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
    # sous-match listé avant son ouverture (cellules « blocked », vu le 09/10 sur des tirs vers le but) : aucune cote jouable
    locked = json.loads(json.dumps(SOT))
    for eg in locked["eventGroups"]:
        for ev in eg["events"]:
            for c in ev:
                c["blocked"] = True
    lout = {}
    xbet._ladder(locked["eventGroups"], *xbet.SUBGAMES[12], lout)
    assert lout == {}, lout
    assert xbet.book_prob(out, "team_sot_total", "Over", 8.5) == round((1 / 1.81) / (1 / 1.81 + 1 / 1.95), 4)
    assert [g["gid"] for g in xbet.parse_games(GAMES)] == [900]                    # ni paris spéciaux (901), ni sous-matchs (902)
    eng = [{"id": 5, "mainGameId": 5, "startTs": KO, "opponent1": {"fullNameEng": "CSKA Moscow", "fullName": "CSKA Moscou"},
            "opponent2": {"fullName": "Dynamo Moscou"}}]
    assert xbet.parse_games(eng)[0]["home"] == "CSKA Moscow"                                    # nom anglais d'abord
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
    assert got["e1"]["px"][("alternate_totals_corners", "Over", 9.5)] == 1.99 and got["e1"]["cid"] == 369147714
    assert xbet.match_url(369147714) == "https://1xbet.ci/fr/line/football/1/369147714" and xbet.match_url(None) is None
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
    # proxy Webshare en panne (502 de la session 20) : session 21, retenue pour les relevés suivants ; un refus de 1xBet ne change rien
    store.save(xbet.USAGE, {})
    os.environ["XBET_PROXY"] = "http://user-GB-20:pw@proxy.test:80"

    def flaky(net):
        def fetch(url, *a, **k):
            if "-GB-20:" in http.PROXIES[xbet.DOMAIN]:
                return http.Result(url, None, error="réseau indisponible (ProxyError)")
            return FakeNet.fetch(net, url, *a, **k)
        old, http.fetch = http.fetch, fetch
        try:
            return xbet.fetch_day([("soccer_spain_la_liga", EV | {"xgid": 900})])
        finally:
            http.fetch = old
    try:
        got = with_net(flaky)
        assert set(got) == {"e1"} and "-GB-21:" in http.PROXIES[xbet.DOMAIN] and xbet.usage()["session"] == 21
        blocked = lambda net: xbet.fetch_day([("soccer_spain_la_liga", EV | {"xgid": 900})])      # noqa: E731
        old = http.fetch
        http.fetch = lambda url, *a, **k: http.Result(url, None, status=403, blocked=True, error="HTTP 403 : accès refusé")
        try:
            assert blocked(None) == {} and "-GB-21:" in http.PROXIES[xbet.DOMAIN], "refus de 1xBet : pas de changement d'IP"
        finally:
            http.fetch = old
    finally:
        os.environ["XBET_PROXY"] = saved
    store.save(xbet.USAGE, u)
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
        return {"e1": {"gid": 900, "px": px, "cid": 369147714}}
    assert xprice.apply(pronos, fetch=fetch) == {"matches": 1, "value": 1, "low": 0, "weak": 0, "none": 0}
    assert seen["events"] == ["e1", "e2"] and seen["wanted"]["e1"] == {"alternate_totals_corners"}
    text = fmt.pronos(CB.best_per_match(pronos))
    assert "💎 Plus de 9,5 corners · <b>1,99</b>" in text and "valeur +19 %" in text, text
    assert "<a href" not in text, "pas de lien : la cote seulement"
    assert "· mini <b>" not in text and "« mini »" not in text, "le pronostic sans cote 1xBet n'est pas montré"
    real = fmt.fair_combo(3.96, [pronos[0], pronos[0] | {"id": "x"}], 0.36, 0.5)
    assert real.startswith("<b>💎 COMBINÉ — cote 3,96</b>\n🎯 2 paris · ") and real.count("   Plus de 9,5 corners · <b>1,99</b>") == 2, real
    assert "<a href" not in real
    mixed = fmt.fair_combo(3.0, [pronos[0], pronos[1]], 0.4, 0.5)
    assert "À VÉRIFIER CHEZ 1XBET — cote mini 3" in mixed and " · <b>1,99</b>" in mixed and " · joue si ≥ <b>" in mixed and "⚠️ Joue seulement si chaque pari atteint sa cote mini" in mixed, mixed
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
    assert n == 1 and len(sent) == 1 and "Nouvelles cotes 1xBet" in sent[0] and "8,5 corners · <b>1,59</b>" in sent[0]
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


def test_xworld_scan():
    """Championnats hors The Odds API : matchs 1xBet du jour Kelly ajoutés au relevé avec leur id 1xBet, sans toucher aux autres."""
    from datetime import date
    from kelly import xworld
    day = date(2026, 10, 9)
    games = [{"gid": 1, "home": "FCSB", "away": "Rapid Bucuresti", "ts": KO},
             {"gid": 2, "home": "CFR Cluj", "away": "Dinamo", "ts": KO + 2 * 86400}]          # après le jour Kelly : ignoré
    scan = {"leagues": {"soccer_epl": [{"id": "x"}]}}
    n = xworld.extend_scan(scan, day, lister=lambda keys: {"soccer_xb_romania": games, "soccer_epl": games})
    ev = scan["leagues"]["soccer_xb_romania"]
    assert n == 1 and len(ev) == 1 and ev[0]["xgid"] == 1 and ev[0]["id"] == "xb1" and ev[0]["sport_key"] == "soccer_xb_romania"
    assert scan["leagues"]["soccer_epl"] == [{"id": "x"}], "un championnat déjà relevé n'est pas remplacé"
    assert xworld.extend_scan({}, day, lister=lambda keys: {}) == 0
    from kelly import catalogue
    assert catalogue.region("soccer_xb_romania") == "europe" and catalogue.tier("soccer_xb_romania") == 2
    assert "soccer_xb_romania" in C.XB_KEYS and "soccer_xb_romania" not in C.SPORTS, "jamais acheté chez The Odds API"
    from kelly import fair
    c = fair.make_pick(ev[0], "totals", "Under", 2.5, 0.62, "x", "FotMob")
    assert c["xgid"] == 1, "le pronostic garde l'id 1xBet : cotes lues directement"
    # buts attendus tirés du classement FotMob, avec l'avantage du terrain du championnat (rapproché de la moyenne mondiale)
    from kelly.sources import features
    assert features.home_share([]) == features.HOME_SHARE[0]
    home_heavy = [{"hg": 3, "ag": 0}] * 60
    assert 0.555 < features.home_share(home_heavy) < 1.0
    out = {"sources": []}
    hv, av = {"n": 10, "gf": 15, "ga": 10, "w": 5, "pts": 17}, {"n": 10, "gf": 15, "ga": 10, "w": 5, "pts": 17}
    features._standings_goals(out, "A", "B", hv, av)
    assert out["exp_goals_home"] > out["exp_goals_away"], "à forces égales, l'équipe qui reçoit marque plus"
    assert out["aps_q"] == 0.4 and out["n_home"] == 10 and "FotMob (classement)" in out["sources"]
    few = {"sources": []}
    features._standings_goals(few, "A", "B", hv | {"n": 2}, av)
    assert "exp_goals_home" not in few, "moins de 3 matchs joués : pas de chiffres"
    print("ok  championnats hors The Odds API ajoutés depuis 1xBet")


def test_save_coupon():
    """Enregistrement d'un combiné chez 1xBet : corps exact, code extrait, affichage et repli sans code."""
    from unittest.mock import patch
    from kelly.sources.http import Result

    posted = []

    def mock_post(url, body, headers=None, timeout=None):
        posted.append({"url": url, "body": body, "headers": headers})
        if "SaveCoupon" in url:
            return Result(url, json.dumps({"Value": "FGCGE", "Id": 0, "Success": True, "Error": "", "ErrorCode": 0}).encode("utf-8"), 200)
        return Result(url, b"{}", 200)

    legs = [
        {"id": "e1", "home": "Qatar SC", "away": "Al Rayyan", "pick": "Moins de 3,5 buts", "xodds": 1.68, "prob": 0.65, "xref": [757965452, 10, 3.5]},
        {"id": "e2", "home": "Corvinul", "away": "Voluntari", "pick": "Moins de 3,5 buts", "xodds": 1.36, "prob": 0.75, "xref": [755936175, 10, 3.5]},
    ]
    with patch("kelly.sources.http.post", side_effect=mock_post):
        code = xbet.save_coupon(legs)
    assert code == "FGCGE"
    assert len(posted) == 1
    req = posted[0]
    assert req["url"] == "https://1xbet.ci/service-api/LiveBet/Open/SaveCoupon"
    b = req["body"]
    assert b["Summ"] == 0 and b["Vid"] == 1 and b["Kind"] == 3 and b["partner"] == C.XBET_COUPON_PARTNER == 1 and b["CheckCf"] == 1 and b["notWait"] is True
    assert len(b["Events"]) == 2
    assert b["Events"][0] == {
        "GameId": 757965452, "Type": 10, "Coef": 1.68, "Param": 3.5,
        "PV": None, "PlayerId": 0, "Kind": 3, "InstrumentId": 0, "Seconds": 0, "Price": 0, "Expired": 0, "PlayersDuel": None
    }
    assert b["Events"][1]["GameId"] == 755936175 and b["Events"][1]["Type"] == 10 and b["Events"][1]["Coef"] == 1.36

    # Format avec code coupon
    msg = fmt.fair_combo(2.28, legs, 0.45, 0.5, coupon=code)
    assert f"🎟️ Code : <code>{code}</code>" in msg
    assert "<a href" not in msg

    # Format sans coupon
    msg_no = fmt.fair_combo(2.28, legs, 0.45, 0.5)
    assert "🎟️ Code" not in msg_no and "<code>" not in msg_no

    # Échec réseau -> None, ne lève jamais
    with patch("kelly.sources.http.post", return_value=Result("https://1xbet.ci/service-api/LiveBet/Open/SaveCoupon", None, error="HTTP 500")):
        assert xbet.save_coupon(legs) is None

    # Échec applicatif Success: false -> None
    err_res = Result("https://1xbet.ci/service-api/LiveBet/Open/SaveCoupon", json.dumps({"Success": False, "Error": "Erreur"}).encode("utf-8"), 200)
    with patch("kelly.sources.http.post", return_value=err_res) as m, patch("kelly.sources.xbet.time.sleep") as sl:
        assert xbet.save_coupon(legs) is None
        assert m.call_count == 1 + len(xbet.COUPON_BACKOFF)          # refus : un essai par attente, pas plus
        assert [c.args[0] for c in sl.call_args_list] == list(xbet.COUPON_BACKOFF) == sorted(xbet.COUPON_BACKOFF)    # attentes croissantes

    # Refus intermittent puis accepté : le code de la 2e tentative
    ok_res = Result("https://1xbet.ci/service-api/LiveBet/Open/SaveCoupon", json.dumps({"Success": True, "Value": "AP89E"}).encode("utf-8"), 200)
    with patch("kelly.sources.http.post", side_effect=[err_res, err_res, ok_res]), patch("kelly.sources.xbet.time.sleep") as sl:
        assert xbet.save_coupon(legs) == "AP89E" and sl.call_count == 2       # accepté au 3e essai : on s'arrête là

    # Pendant une commande Telegram, l'utilisateur qui attend est prévenu de chaque refus (raison de 1xBet), puis du code accepté
    said = []
    with patch("kelly.sources.http.post", side_effect=[err_res, err_res, ok_res]), patch("kelly.sources.xbet.time.sleep"), \
            xbet.coupon_notice(said.append):
        assert xbet.save_coupon(legs) == "AP89E"
    assert said == ["⏳ 1xBet a refusé le code coupon (« Erreur ») : nouvel essai dans 2 s (2/4)…",
                    "⏳ 1xBet a refusé le code coupon (« Erreur ») : nouvel essai dans 5 s (3/4)…",
                    "✅ 1xBet a accepté le code coupon."], said
    said.clear()
    ru = Result("https://1xbet.ci/service-api/LiveBet/Open/SaveCoupon",
                json.dumps({"Success": False, "Error": "Сбой вызова, попробуйте позже.", "ErrorCode": 161627}).encode("utf-8"), 200)
    ru2 = Result("https://1xbet.ci/service-api/LiveBet/Open/SaveCoupon",
                 json.dumps({"Success": False, "Error": "Неизвестно", "ErrorCode": 99}).encode("utf-8"), 200)
    with patch("kelly.sources.http.post", side_effect=[ru, ru2, ok_res]), patch("kelly.sources.xbet.time.sleep"), \
            xbet.coupon_notice(said.append):
        assert xbet.save_coupon(legs) == "AP89E"
    assert said[0].startswith("⏳ 1xBet a refusé le code coupon (« panne passagère chez 1xBet »)"), said      # jamais de russe
    assert "(« erreur chez 1xBet (code 99) »)" in said[1], said
    said.clear()
    with patch("kelly.sources.http.post", return_value=ok_res), xbet.coupon_notice(said.append):
        assert xbet.save_coupon(legs) == "AP89E" and said == []                 # accepté du premier coup : rien à dire
    with patch("kelly.sources.http.post", side_effect=[err_res, ok_res]), patch("kelly.sources.xbet.time.sleep"):
        assert xbet.save_coupon(legs) == "AP89E" and said == []                 # relevé automatique : personne n'attend, aucun message

    # Entrées invalides -> None, ne lève jamais
    assert xbet.save_coupon([]) is None
    assert xbet.save_coupon([{"home": "A"}]) is None
    assert xbet.save_coupon(None) is None
    print("ok  save_coupon 1xBet (corps exact, code extrait, affichage et repli)")


DAY2 = "2030-01-01"
KO2 = f"{DAY2}T19:00:00Z"


def live_pronos(n=7):
    """Pronostics 💎 d'un jour à venir (moins de 3,5 buts à 1,55), match m0..m(n-1), id 1xBet 900+i."""
    return [prono("totals", "Under", 3.5, 0.72) | {"id": f"m{i}", "home": f"H{i}", "away": f"A{i}", "commence": KO2, "xodds": 1.55,
                                                    "xev": 0.12, "xgid": 900 + i, "xref": [900 + i, 10, 3.5],
                                                    "psh": {"under|3.5": [0.70, "pinnacle", 1]},    # Pinnacle voit 70 % (buts : le marché décide)
                                                    "key": f"m{i}|totals||3.5|Under"} for i in range(n)]


def fake_px(drop=()):
    """Cotes 1xBet relues : moins de 3,5 buts à 1,55 partout, sauf les matchs de `drop` (pari retiré par 1xBet)."""
    return {f"m{i}": {"gid": 900 + i, "px": {} if f"m{i}" in drop else {("totals", "Under", 3.5): 1.55, ("totals", "Over", 3.5): 2.4},
                      "refs": {} if f"m{i}" in drop else {("totals", "Under", 3.5): [900 + i, 10, 3.5]}} for i in range(7)}


def test_live_mode_and_patch():
    from kelly import cache
    from kelly.sources import http as H
    cache.OFFLINE = True
    with xbet.live_mode():
        assert not cache.OFFLINE and xbet._ODDS_TTL[0] == C.XBET_FRESH_MIN * 60 and H.delay_for(xbet.DOMAIN) == C.XBET_LIVE_DELAY
        # cotes lues il y a 2 min : reprises du cache (aucune requête) ; il y a 6 min : relues
        import requests
        from kelly.sources import state as S
        u = xbet.url("gameEvents", gameId=4242)
        H._write(H._key(u, xbet.JSON_H), {"url": u, "fetched_at": S.clock() - 120, "status": 200}, b'{"eventGroups": []}')
        old_get, calls = requests.get, []
        requests.get = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(requests.ConnectionError("pas de réseau"))
        try:
            assert H.fetch(u, xbet.SOURCE, xbet._ODDS_TTL[0], headers=xbet.JSON_H).from_cache and not calls
            H._write(H._key(u, xbet.JSON_H), {"url": u, "fetched_at": S.clock() - 360, "status": 200}, b'{"eventGroups": []}')
            H.PROXIES.pop(xbet.DOMAIN, None)
            H.fetch(u, xbet.SOURCE, xbet._ODDS_TTL[0], headers=xbet.JSON_H, check_robots=False)
            assert calls, "plus de 5 min : relu chez 1xBet"
        finally:
            requests.get = old_get
    assert cache.OFFLINE and xbet._ODDS_TTL[0] == xbet.TTL_ODDS and xbet.DOMAIN not in H.DELAY_OVERRIDE
    cache.OFFLINE = False
    store.save(calib.FILE, {})
    calib._memo.clear()
    pool = live_pronos()
    legs = pool[:4]
    same = CB.patch_combo(legs, pool, 5.0, 6.5)
    assert [c["id"] for c in same[0]] == ["m0", "m1", "m2", "m3"] and not same[3] and not same[4]
    pool[1]["xnone"] = 1                                                    # retiré par 1xBet
    res = CB.patch_combo(legs, pool, 5.0, 6.5, fresh_ids={f"m{i}" for i in range(7)})
    assert [c["id"] for c in res[3]] == ["m1"] and len(res[4]) == 1 and res[1] >= 5.0 and "m1" not in [c["id"] for c in res[0]]
    assert CB.patch_combo(legs, pool[:4], 5.0, 6.5) is None, "pas de remplaçant : impossible"
    print("ok  mode direct (cache, hors ligne, délai) et combiné remis à jour")


seen_fresh = []       # durée de fraîcheur demandée à chaque vérification en direct


def with_fakes(fn, drop=(), got=True):
    """Exécute fn avec : today.json du jour DAY2, live_check simulé (matchs `drop` retirés), save_coupon simulé, envoi capturé."""
    from kelly import telegram, xprice
    store.save("today.json", {"day": DAY2, "picks": [], "pronos": live_pronos()})
    sent, coupons = [], []
    seen_fresh.clear()
    real_check = xprice.live_check

    def check(ids, now=None, fetch=None, fresh_min=None):
        seen_fresh.append(fresh_min)
        return real_check(ids, now, fetch=lambda ev, deadline=None, wanted=None, live=True, fresh_min=None:
                                 {k: v for k, v in fake_px(drop).items() if k in {e["id"] for _, e in ev}} if got else {})
    old = (xbet.save_coupon, telegram.send, xprice.live_check)
    xbet.save_coupon = lambda ls: (coupons.append([c["id"] for c in ls]), "K7Q2X")[1]
    telegram.send = lambda text, *a, **k: sent.append(str(text))
    xprice.live_check = check
    try:
        return fn(), sent, coupons
    finally:
        xbet.save_coupon, telegram.send, xprice.live_check = old


def test_combi_live():
    """« combi N » : « ⏳ », cotes relues en direct, pari retiré remplacé, code coupon aux cotes de l'instant ; 1xBet muet : sans code."""
    from datetime import datetime, timezone
    from kelly import app
    store.save(calib.FILE, {})
    calib._memo.clear()
    store.save("bets.json", {"picks": [], "combos": []})
    now = datetime(2030, 1, 1, 13, 0, tzinfo=timezone.utc)
    fc = (live_pronos()[:4], 1.55 ** 4, 0.72 ** 4)
    msg, sent, coupons = with_fakes(lambda: app._live_reply(fc, 5.0, 6.25, now=now), drop=("m1",))
    assert sent and sent[0].startswith("⏳"), sent
    assert "<b>💎 COMBINÉ — cote 5,77</b>" in msg and "🕒 Cotes 1xBet de 13:00 : vérifie-les avant de valider." in msg, msg
    assert seen_fresh == [C.XBET_COMBI_FRESH_MIN] == [180], "combi : cotes de moins de 3 h reprises"
    assert "❌ Retiré : H1 – A1" in msg and "➕ Ajouté" in msg and "🎟️ Code : <code>K7Q2X</code>" in msg
    assert coupons and "m1" not in coupons[0], "le code est créé avec les paris encore jouables"
    msg, _, coupons = with_fakes(lambda: app._live_reply(fc, 5.0, 6.25, now=now), got=False)
    assert "1xBet ne répond pas" in msg and "🎟️ Code" not in msg and not coupons
    print("ok  combi N vérifié en direct (paris remplacés, code aux cotes de l'instant)")


def test_refresh_button_and_updates():
    """Bouton « 🎟️ Vérifier ce combiné » et relevés de 2 h : combiné du matin remis aux cotes du moment, renvoyé seulement s'il change ;
    liste des 💎 nouveaux seulement à 15:00."""
    from datetime import datetime, timezone
    from kelly import app, chat
    store.save(calib.FILE, {})
    calib._memo.clear()
    now = datetime(2030, 1, 1, 13, 0, tzinfo=timezone.utc)
    legs = live_pronos()[:4]
    store.save(fair.COMBOS_FILE, [fair.combo_row(5, (legs, 1.55 ** 4, 0.72 ** 4), DAY2)])
    msg, _, coupons = with_fakes(lambda: app.refresh_combo("5", DAY2, now=now), drop=("m2",))
    assert "🔄 COMBINÉ À JOUR 13:00" in msg and "❌ Retiré : H2 – A2" in msg and "K7Q2X" in msg, msg[:300]
    assert len(fair.load_combos()) == 1, "le suivi garde le combiné du matin"
    assert seen_fresh == [None], "bouton « 🎟️ Vérifier ce combiné » : la règle des 5 min (XBET_FRESH_MIN), pas celle du combi"
    btn = chat.with_played_button("x", "v5", DAY2, refresh=5)
    assert any(b["callback_data"] == f"xmaj|5|{DAY2}" for row in btn.markup["inline_keyboard"] for b in row)
    # relevé de 13:00 : un pari retiré -> combiné renvoyé à jour ; 2e relevé identique -> rien ; pas de liste 💎 avant 15:00
    store.save(app.COMBO_UPDATES, {})

    def refresh(drop, hour):
        return app.run_xbet_refresh(now=now.replace(hour=hour), announce=False, fetch=lambda ev, deadline=None, wanted=None:
                                    {k: v for k, v in fake_px(drop).items() if k in {e["id"] for _, e in ev}})
    _, sent, _ = with_fakes(lambda: refresh(("m3",), 13))
    assert len(sent) == 1 and "🔄 COMBINÉ À JOUR 13:00" in sent[0] and "❌ Retiré : H3 – A3" in sent[0], sent
    from kelly import telegram
    old = telegram.send
    again = []
    telegram.send = lambda text, *a, **k: again.append(str(text))
    try:
        app.run_xbet_refresh(now=now.replace(hour=13, minute=30), announce=False, fetch=lambda ev, deadline=None, wanted=None:
                             {k: v for k, v in fake_px(("m3",)).items() if k in {e["id"] for _, e in ev}})
    finally:
        telegram.send = old
    assert not again, "rien n'a changé depuis la dernière version envoyée : pas de message"
    # relevé manuel « 🔄 Mettre à jour tous les paris » : bilan, puis refus s'il est redemandé moins de 10 min après
    from kelly import chat
    assert chat.route("🔄 Mettre à jour tous les paris") == ("/cotes", None) and chat.route("actualiser") == ("/cotes", None)
    assert chat.route("🔄 Actualiser les cotes") == ("/cotes", None) and chat.route("mettre à jour") == ("/cotes", None)
    store.save(app.COMBO_UPDATES, {})
    store.save(fair.COMBOS_FILE, [fair.combo_row(5, (legs, 1.55 ** 4, 0.72 ** 4), DAY2)])       # le suivi suit la dernière version envoyée : retour au matin
    fetch = lambda ev, deadline=None, wanted=None: {k: v for k, v in fake_px(("m3",)).items() if k in {e["id"] for _, e in ev}}  # noqa: E731
    reply, sent, _ = with_fakes(lambda: app.manual_refresh(now=now.replace(hour=16), fetch=fetch))
    assert reply.startswith("✅ Cotes 1xBet actualisées à 16:00 : 7 match(s) relu(s)") and "1 combiné(s) du matin mis à jour" in reply, reply
    assert sent[0].startswith("⏳") and any("COMBINÉ À JOUR" in m for m in sent), sent
    again = app.manual_refresh(now=now.replace(hour=16, minute=5), fetch=fetch)
    assert again.startswith("Cotes déjà actualisées à 16:00"), again
    print("ok  bouton « Vérifier ce combiné » et combinés du matin renvoyés seulement s'ils changent")


def test_stale_cache_never_served():
    """A1 : un échec réseau qui rend l'ANCIEN corps du cache n'est jamais pris pour des cotes à jour ; un cache frais reste servi."""
    import requests
    from kelly import cache
    store.save(state.FILE, {})
    store.save(xbet.USAGE, {})
    cache.OFFLINE = False
    u = xbet.url("gameEvents", gameId=5151)
    body = json.dumps(MAIN).encode()
    http._write(http._key(u, xbet.JSON_H), {"url": u, "fetched_at": state.clock() - 5 * 3600, "status": 200}, body)
    # vieux relevé en cache + réseau coupé (robots.txt injoignable) : erreur, pas le vieux corps
    old_get, old_ps = requests.get, http.PROXIES.pop(xbet.DOMAIN, None)
    http._robots.clear()
    requests.get = lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("pas de réseau"))
    try:
        r = http.fetch(u, xbet.SOURCE, 600, headers=xbet.JSON_H)
        assert r.ok and r.stale and r.error, "le piège : http.fetch rend l'ancien corps avec stale + error"
        try:
            xbet._get_json(u, 600)
            raise AssertionError("cotes périmées servies")
        except xbet.XbetError as e:
            assert str(e)
        # cache encore frais : repris sans requête
        assert xbet._get_json(u, 10 * 3600)["constId"] == MAIN["constId"]
    finally:
        requests.get = old_get
        if old_ps:
            http.PROXIES[xbet.DOMAIN] = old_ps
    # texte d'erreur du proxy transmis tel quel (basculement de session) ; stale sans erreur (hors ligne) : « réponse périmée »
    old = http.fetch
    http.fetch = lambda url, *a, **k: http.Result(url, body, None, from_cache=True, stale=True, error="réseau indisponible (ProxyError)")
    try:
        try:
            xbet._get_json(u, 600)
        except xbet.XbetError as e:
            assert "ProxyError" in str(e)
        http.fetch = lambda url, *a, **k: http.Result(url, body, 200, from_cache=True, stale=True)
        try:
            xbet._get_json(u, 600)
            raise AssertionError("cache périmé servi")
        except xbet.XbetError as e:
            assert "périmée" in str(e)
        # relevé : match non retrouvé (pas de cotes inventées), erreur notée
        http.fetch = lambda url, *a, **k: http.Result(url, body, None, from_cache=True, stale=True, error="HTTP 500")
        assert xbet.fetch_day([("soccer_spain_la_liga", EV | {"xgid": 900})]) == {}
        assert "HTTP 500" in state.health("xbet")["last_error"]
    finally:
        http.fetch = old
    # le proxy de la session 20 est en panne : l'erreur de la lecture périmée fait passer à la session 21
    saved = os.environ["XBET_PROXY"]
    os.environ["XBET_PROXY"] = "http://user-GB-20:pw@proxy.test:80"
    store.save(xbet.USAGE, {})

    def fetch(url, *a, **k):
        if "-GB-20:" in http.PROXIES[xbet.DOMAIN]:
            return http.Result(url, body, None, from_cache=True, stale=True, error="réseau indisponible (ProxyError)")
        return FakeNet().fetch(url, *a, **k)
    http.fetch = fetch
    try:
        got = xbet.fetch_day([("soccer_spain_la_liga", EV | {"xgid": 900})])
        assert set(got) == {"e1"} and "-GB-21:" in http.PROXIES[xbet.DOMAIN]
    finally:
        http.fetch = old
        os.environ["XBET_PROXY"] = saved
        store.save(xbet.USAGE, {})
    print("ok  cotes périmées du cache jamais servies (échec réseau, basculement de session)")


def test_post_never_replayed():
    """A1b : une écriture n'est renvoyée que si la connexion n'a pas pu s'établir ; coupon : bascule de session sur panne du proxy."""
    import requests
    from unittest.mock import patch
    url = "https://1xbet.ci/service-api/LiveBet/Open/SaveCoupon"
    old_sleep, old_post, old_ps = http._sleep, requests.post, http.PROXIES.pop(xbet.DOMAIN, None)
    http._sleep = lambda s: None
    calls = []

    def boom(exc):
        def post(*a, **k):
            calls.append(1)
            raise exc
        return post
    try:
        for exc, n in ((requests.ReadTimeout("lent"), 1), (requests.ConnectTimeout("injoignable"), 2),
                       (requests.exceptions.ProxyError("502"), 2)):
            calls.clear()
            requests.post = boom(exc)
            r = http.post(url, {"a": 1})
            assert not r.ok and type(exc).__name__ in r.error and len(calls) == n, (type(exc).__name__, calls)
        calls.clear()
        requests.post = lambda *a, **k: calls.append(1) or type("R", (), {"status_code": 503, "content": b"", "headers": {}})()
        assert http.post(url, {"a": 1}).error == "HTTP 503" and len(calls) == 1, "5xx : la requête a pu arriver"
    finally:
        http._sleep, requests.post = old_sleep, old_post
        if old_ps:
            http.PROXIES[xbet.DOMAIN] = old_ps
    # coupon : proxy en panne avant l'envoi -> session suivante ; délai de lecture : jamais renvoyé, pas de code
    saved = os.environ["XBET_PROXY"]
    os.environ["XBET_PROXY"] = "http://user-GB-20:pw@proxy.test:80"
    store.save(xbet.USAGE, {})
    store.save(state.FILE, {})
    legs = [{"xodds": 1.68, "xref": [1, 10, 3.5]}, {"xodds": 1.36, "xref": [2, 10, 3.5]}]
    seen = []

    def mock_post(u, body, headers=None, timeout=None):
        seen.append(http.PROXIES[xbet.DOMAIN])
        if "-GB-20:" in seen[-1]:
            return http.Result(u, None, error="réseau indisponible (ProxyError)")
        return http.Result(u, json.dumps({"Success": True, "Value": "ZZ9"}).encode(), 200)
    try:
        with patch("kelly.sources.http.post", side_effect=mock_post):
            assert xbet.save_coupon(legs) == "ZZ9" and "-GB-21:" in seen[-1] and len(seen) == 2
        seen.clear()
        with patch("kelly.sources.http.post", side_effect=lambda *a, **k: seen.append(1) or http.Result(
                a[0], None, error="réseau indisponible (ReadTimeout)")):
            assert xbet.save_coupon(legs) is None and len(seen) == 1, "délai de lecture : le ticket a pu être créé"
    finally:
        os.environ["XBET_PROXY"] = saved
        store.save(xbet.USAGE, {})
    print("ok  écriture jamais rejouée à tort, coupon : bascule de session")


def test_fetch_day_menu_down():
    """A7 : menu illisible -> les événements sans id 1xBet sont sautés, ceux qui ont un id 1xBet sont quand même relus."""
    store.save(state.FILE, {})
    store.save(xbet.USAGE, {})

    def run(net):
        base = net.fetch
        net.fetch = lambda url, *a, **k: (net.calls.append(url) or http.Result(url, None, error="HTTP 500") if "leftMenuSports" in url
                                          else base(url, *a, **k))
        http.fetch = net.fetch
        return xbet.fetch_day([("soccer_spain_la_liga", EV), ("soccer_spain_la_liga", EV | {"id": "e2", "xgid": 900}),
                               ("soccer_spain_la_liga", EV | {"id": "e3"})]), net.calls
    got, calls = with_net(run)
    assert set(got) == {"e2"}, got
    assert sum("leftMenuSports" in u for u in calls) == 1, "le menu n'est pas redemandé pour chaque événement"
    print("ok  menu illisible : les matchs à id 1xBet connu restent relus")


def test_choose_prefers_combo_line():
    """A4 : une ligne de valeur qui peut entrer dans un combiné (proba >= 60 %) passe avant une ligne à 59 % plus rentable.
    A6 : ligne trop faible (pas un problème de prix) -> « weak »."""
    store.save(calib.FILE, {})
    calib._memo.clear()
    px = {}
    xbet._ladder(CORNERS["eventGroups"], *xbet.SUBGAMES[2], px)
    assert C.VIABLE_MIN_P < 0.59 < C.FAIR_COMBO_MIN_P <= 0.75
    c = prono("alternate_totals_corners", "Over", 9.5, 0.59,
              alts=[["Over", 9.5, 0.59, None], ["Under", 10.5, 0.75, None]])            # 0,59 x 1,99 vaut plus que 0,75 x 1,45
    assert xprice.price(c, px) == "value" and (c["name"], c["point"], c["xodds"]) == ("Under", 10.5, 1.45), c
    assert CB.fair_eligible(c)
    solo = prono("alternate_totals_corners", "Over", 9.5, 0.59, alts=[["Over", 9.5, 0.59, None]])
    assert xprice.price(solo, px) == "value" and solo["point"] == 9.5, "seule ligne viable : gardée"
    weak = prono("alternate_totals_corners", "Over", 9.5, 0.50, alts=[["Over", 9.5, 0.50, None]])
    assert xprice.price(weak, px) == "weak" and weak["xodds"] == 1.99 and not CB.viable(weak)
    low = prono("alternate_totals_corners", "Under", 10.5, 0.72, alts=[["Under", 10.5, 0.72, None]])
    assert xprice.price(low, px) == "low", "bien probable mais mal payé : « low »"
    got = xprice.apply([weak], fetch=lambda *a, **k: {"e1": {"gid": 900, "px": px}})
    assert got["weak"] == 1 and got["low"] == 0
    print("ok  ligne éligible aux combinés préférée, ligne trop faible comptée à part")


def test_patch_combo_carry_and_changes():
    """A2 : jambe dont le match n'a pas été relu reprise telle quelle ; A3 : ligne changée signalée ; A5 : jamais au-dessus de hi."""
    store.save(calib.FILE, {})
    calib._memo.clear()
    pool = live_pronos()
    legs = pool[:4]
    pool[1]["xnone"] = 1
    pool[3]["xnone"] = 1                                               # ces deux matchs ne sont PAS relus : jambes gardées
    res = CB.patch_combo(legs, pool, 5.0, 6.5, fresh_ids={"m0", "m2"})
    assert [c["id"] for c in res[0]] == ["m0", "m1", "m2", "m3"] and not res[3] and not res[4] and not res[5]
    assert [c["xodds"] for c in res[0]] == [1.55] * 4 and res[0][1]["odds"] == 1.55, "cote enregistrée gardée"
    res = CB.patch_combo(legs, pool, 5.0, 6.5, fresh_ids=set())
    assert [c["id"] for c in res[0]] == [c["id"] for c in legs] and not res[3] and not res[4], "rien relu : rien retiré"
    # match relu et pari disparu : retiré et remplacé par un match relu
    res = CB.patch_combo(legs, pool, 5.0, 6.5, fresh_ids={"m1", "m4"})
    assert [c["id"] for c in res[3]] == ["m1"] and [c["id"] for c in res[4]] == ["m4"], res
    assert [c["id"] for c in res[0]] == ["m0", "m2", "m3", "m4"]
    # A3 : même match, autre ligne (Moins de 2,5 buts) -> signalé
    pool = live_pronos()
    new = pool[0] | {"point": 2.5, "key": "m0|totals||2.5|Under", "pick": fmt.label("totals", "Under", 2.5, None, "soccer"),
                     "xref": [900, 10, 2.5]}
    pool[0] = new
    res = CB.patch_combo(legs, pool, 5.0, 6.5)
    assert len(res) == 6 and not res[3] and not res[4] and len(res[5]) == 1
    old_leg, new_leg = res[5][0]
    assert old_leg["point"] == 3.5 and new_leg["point"] == 2.5 and new_leg["id"] == "m0"
    msg = fmt.combo_changes(res[3], res[4], res[5])
    assert f"🔁 Changé : H0 – A0 : {fmt.esc(legs[0]['pick'])} → {fmt.esc(new['pick'])}" in msg, msg
    assert fmt.combo_changes([], []) == "" and "🔁" not in fmt.combo_changes([legs[0]], [])
    # A5 : le complément ne dépasse jamais hi (2 survivants à 2,40 ; un remplaçant à 2,5 ferait 6,0 > 5,2)
    pool = live_pronos()
    pool[2]["xodds"] = 2.5
    assert CB.patch_combo(pool[:2], pool, 5.0, 5.2) is None, "aucun remplaçant dans [lo, hi] : impossible"
    ok = CB.patch_combo(pool[:2], pool, 5.0, 6.5)
    assert ok and 5.0 <= ok[1] <= 6.5
    print("ok  combiné remis à jour : jambes non relues gardées, lignes changées signalées, borne hi respectée")


def test_fair_best_single_pass():
    """A9 : toutes les jambes réelles -> un seul best_combo."""
    calls = []
    old = CB.best_combo
    CB.best_combo = lambda legs, lo, hi=None, *a, **k: calls.append(len(legs)) or None
    try:
        legs = [{"id": "a", "xodds": 1.5}, {"id": "b", "xodds": 1.6}]
        assert CB._fair_best(legs, 2.0, 3.0) is None and calls == [2]
        calls.clear()
        CB._fair_best(legs + [{"id": "c"}], 2.0, 3.0)
        assert calls == [2, 3]
    finally:
        CB.best_combo = old
    print("ok  combiné à cote juste : pas de second calcul sur les mêmes jambes")


def main():
    test_parse_and_url()
    test_find_match()
    test_fetch_day_requests()
    test_proxy_cap_and_reuse()
    test_choose_line_and_viability()
    test_apply_and_messages()
    test_afternoon_refresh()
    test_xworld_scan()
    test_live_mode_and_patch()
    test_combi_live()
    test_refresh_button_and_updates()
    test_save_coupon()
    test_stale_cache_never_served()
    test_post_never_replayed()
    test_fetch_day_menu_down()
    test_choose_prefers_combo_line()
    test_patch_combo_carry_and_changes()
    test_fair_best_single_pass()
    print("tests 1xBet : OK")


if __name__ == "__main__":
    main()
