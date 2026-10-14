"""`saison N` reconstruit les jambes À LA DEMANDE (hors ligne, faux 1xBet) : matchs du jour pas commencés, coupes (LDC) comprises, cotes relues chez 1xBet dans le plafond de la voie
(SEASON_MAX_MATCHES matchs, SEASON_SECONDS s), une ligne dit ce qui a été trouvé (matchs lus, de coupe, équipes sous 4 matchs, matchs sans prix). Lancer : python -m tests.test_season_rebuild"""
import os
import tempfile
import time
from datetime import timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-season-rebuild-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-season-rebuild-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from tests.test_season import league, row  # noqa: E402
from tests import test_season_lane as T  # noqa: E402
from tests.test_season_cup import rename  # noqa: E402
from kelly import app, config as C, lanes, season, seasonlane as SL, store  # noqa: E402

CUP = "soccer_uefa_champs_league"
NOW = T.NOW


def world():
    """Deux championnats suivis (X, Y), une équipe de X avec seulement 2 matchs (Zed), puis les matchs du jour : un de championnat, trois de LDC, un commencé, un hors liste."""
    T.setup()
    x = rename(league(10, 5, seed=1, mean_h=1.9, mean_a=1.5, corners=(6.5, 5.5), disp=0.1), "Xe") + [row("Zed", "2026-09-20", 1, goals=(1, 1), corners=(5, 5)), row("Zed", "2026-09-27", 0, goals=(0, 1), corners=(5, 5)),
                                                                                                       row("XeT0", "2026-09-20", 0, goals=(1, 1), corners=(5, 5))]
    y = rename(league(10, 5, seed=2, mean_h=1.2, mean_a=0.9, corners=(4.5, 3.5), disp=0.1), "Yo")
    obs = {"soccer_x": x, "soccer_y": y}
    season.collect = lambda sk, on, loaders=None: (obs.get(sk, []), "football-data" if sk in obs else None)
    SL.known_leagues = lambda _filtered=True: ["soccer_x", "soccer_y"]
    season.reset()
    ko = T.KO
    ev = lambda i, sk, h, a, t=ko: {"id": f"e{i}", "sport_key": sk, "home_team": h, "away_team": a, "commence_time": t, "league": sk}      # noqa: E731
    started = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    scan = {"leagues": {"soccer_x": [ev(1, "soccer_x", "XeT1", "XeT4"), ev(2, "soccer_x", "XeT2", "XeT5", started)],
                        CUP: [ev(3, CUP, "XeT2", "YoT3"), ev(4, CUP, "YoT2", "XeT6"), ev(5, CUP, "Zed", "YoT4"), ev(6, CUP, "Inconnu FC", "YoT5")],
                        "soccer_other": [ev(7, "soccer_other", "A", "B")]}}
    return scan


def fetch_without(pronos, missing=(), calls=None):
    base = T.fetch_of(pronos, over_odds=2.4, under_odds=1.62)

    def fetch(events, **kw):
        if calls is not None:
            calls.append(kw | {"ids": [e["id"] for _, e in events]})
        out = base(events, **kw)
        return {i: v for i, v in out.items() if i not in missing}
    return fetch


def test_remember_and_rebuild():
    scan = world()
    SL.remember_events(scan, T.DAY)
    doc = store.load(SL.EVENTS_FILE, {})
    assert doc["day"] == str(T.DAY) and {e["id"] for e in doc["events"]} == {"e1", "e2", "e3", "e4", "e5", "e6"}, "championnats suivis et coupes seulement"
    pronos = SL.make({"leagues": {k: [e for e in v if e["id"] != "e2"] for k, v in scan["leagues"].items()}}, NOW, T.DAY)
    calls = []
    legs, info = SL.rebuild(NOW, fetch_without(pronos, missing={"e4"}, calls=calls))
    assert info["events"] == 5 and info["cup_events"] == 4, info          # e2 commencé : écarté ; e1 + e3 + e4 + e5 + e6
    assert info["predicted"] == 3 and info["few"] == {"Zed"} and info["unknown"] == {"Inconnu FC"}, info
    assert info["read"] == 2 and info["cup_read"] == 1 and info["no_price"] == 1, info              # e1 et e3 lus ; e4 sans prix (1xBet n'a rien rendu)
    assert legs and {c["id"] for c in legs} <= {"e1", "e3"} and any(c.get("cup") for c in legs) and any(not c.get("cup") for c in legs)
    assert all(c["strategy"] in ("saison", "fill") and c["lane"] == "saison" for c in legs)
    kw = calls[0]
    assert kw["live"] is True and kw["fresh_min"] == C.XBET_COMBI_FRESH_MIN and 0 < kw["deadline"] - time.monotonic() <= C.SEASON_SECONDS, kw
    assert set(kw["ids"]) == {"e1", "e3", "e4"}, "les matchs prédits seulement, pas les commencés ni les équipes sans modèle"
    line = SL.summary_line(info)
    assert line == "🔎 2 matchs lus chez 1xBet sur 5 non commencés (dont 1 de coupe) · 1 équipe sous 4 matchs · 1 match sans prix · 1 équipe non reconnue", line
    assert [r for r in store.load(SL.LOG, []) if r["id"] in ("e1", "e3")], "lignes journalisées (CLV par jambe)"
    print("ok  reconstruction : matchs non commencés (coupes comprises) relus en direct, bilan exact (lus, de coupe, équipes sous 4 matchs, sans prix, non reconnues), journalisé")


def test_cap_and_failures():
    scan = world()
    SL.remember_events(scan, T.DAY)
    pronos = SL.make(scan, NOW, T.DAY)
    calls = []
    C.SEASON_MAX_MATCHES = 2
    try:
        legs, info = SL.rebuild(NOW, fetch_without(pronos, calls=calls))
        line = SL.summary_line(info)
    finally:
        C.SEASON_MAX_MATCHES = 60
    ids = {c["id"] for c in pronos}
    assert info["unread"] == len(ids) - 2 == 1 and "1 match non relu (au-delà du plafond de 2 matchs)" in line, (info, line)
    assert info["no_price"] == 0 or info["no_price"] <= 2
    assert len(calls[0]["ids"]) == 2, "plafond SEASON_MAX_MATCHES"
    # 1xBet muet / en panne : rien lu, jamais d'exception
    legs, info = SL.rebuild(NOW, lambda events, **kw: {})
    assert legs == [] and info["read"] == 0
    boom = lambda events, **kw: (_ for _ in ()).throw(OSError("réseau"))      # noqa: E731
    assert SL.rebuild(NOW, boom)[0] == []
    # pas de matchs gardés ce jour-là
    store.save(SL.EVENTS_FILE, {"day": "2020-01-01", "events": []})
    assert SL.rebuild(NOW, fetch_without(pronos)) [0] == [] and SL.rebuild(NOW, fetch_without(pronos))[1]["events"] == 0
    print("ok  plafond de 60 matchs / 600 s respecté, 1xBet muet ou en panne = aucune jambe sans exception, matchs d'un autre jour ignorés")


def test_fallback_on_world_cache():
    """Installation en cours de journée : le passage du matin (ancien code) n'a pas gardé season_events.json, mais data/monde.json existe -> les matchs du jour en viennent."""
    scan = world()
    store.remove(SL.EVENTS_FILE)
    store.save("today.json", {"day": str(T.DAY), "season": [], "picks": [], "pronos": []})
    hh = (NOW + timedelta(hours=8)).strftime("%H:%M")
    store.save("monde.json", {"day": str(T.DAY), "leagues": {"soccer_x": [["XeT1", "XeT4", hh]], CUP: [["XeT2", "YoT3", hh], ["YoT2", "XeT6", "02:30"]], "soccer_other": [["A", "B", hh]]}})
    evs = SL.events_from_monde(str(T.DAY))
    assert len(evs) == 3 and all(e["id"].startswith("mx-") and "|" not in e["id"] for e in evs) and len({e["id"] for e in evs}) == 3, evs
    late = next(e for e in evs if e["home_team"] == "YoT2")
    assert late["commence_time"].startswith(f"{T.DAY + timedelta(days=1)}T02:30"), "avant 04:00 UTC : lendemain civil du même jour Kelly"
    assert SL.events_from_monde(str(T.DAY)) == evs, "identifiants stables"
    assert SL.events_from_monde("2020-01-01") == [], "un autre jour : rien"
    pronos = SL.make(world(), NOW, T.DAY)
    store.remove(SL.EVENTS_FILE)
    store.save("monde.json", {"day": str(T.DAY), "leagues": {"soccer_x": [["XeT1", "XeT4", hh]], CUP: [["XeT2", "YoT3", hh]]}})
    legs, info = SL.rebuild(NOW, T.fetch_of(SL.make({"leagues": {k: [dict(e) for e in v] for k, v in {"soccer_x": SL.events_from_monde(str(T.DAY))[:1], CUP: SL.events_from_monde(str(T.DAY))[1:]}.items()}}, NOW, T.DAY)))
    assert info["events"] == 2 and info["cup_events"] == 1 and info["read"] == 2 and info.get("from_monde") and legs, info
    # rien du tout (ni liste du matin ni tour du monde) : la réponse le dit, sans « sur 0 » muet
    store.remove(SL.EVENTS_FILE)
    store.remove("monde.json")
    out = str(app.season_command(["3"], {"day": str(T.DAY), "season": [], "xbet_at": "09:30"}, now=NOW, fetch=lambda events, **kw: {}))
    assert "aucun match du jour gardé pour la voie" in out and "prochain passage du matin" in out and "aucun match du jour à lire" not in out, out
    print("ok  repli : sans liste du matin, les matchs du jour viennent du tour du monde (identifiants stables, minuit UTC géré) ; sans rien, la réponse le dit")


def test_monthly_cap_guard():
    scan = world()
    SL.remember_events(scan, T.DAY)
    from kelly.sources import xbet
    real = xbet.usage
    xbet.usage = lambda today=None: {"month": "2026-10", "bytes": int(0.9 * C.XBET_MONTH_MB * 1e6), "requests": 1, "days": {}}
    called = []
    real_fetch = xbet.fetch_day
    xbet.fetch_day = lambda *a, **k: called.append(1) or {}
    try:
        assert not SL.month_ok()
        legs, info = SL.rebuild(NOW)                                      # sans fetch injecté : relevé réel -> bloqué par le plafond
        assert legs == [] and info.get("capped") and not called, (info, called)
        assert SL.run(scan, [], T.DAY, NOW, history={}) == ([], "on") and not called
        out = str(app.season_command(["3"], {"day": str(T.DAY), "season": [], "xbet_at": "09:30"}, now=NOW))
        assert "plafond mensuel de données 1xBet atteint à 85 %" in out, out
        xbet.usage = lambda today=None: {"month": "2026-10", "bytes": int(0.5 * C.XBET_MONTH_MB * 1e6), "requests": 1, "days": {}}
        assert SL.month_ok()
    finally:
        xbet.usage, xbet.fetch_day = real, real_fetch
    print("ok  plafond mensuel : au-delà de 85 % des données 1xBet du mois la voie ne relit plus (le relevé du matin passe avant), dit dans la réponse")


def test_kelly_day_after_midnight_utc():
    scan = world()
    SL.remember_events(scan, T.DAY)                                   # jour Kelly D (matchs du soir des Amériques jusqu'à 04:00 UTC le lendemain)
    store.save("today.json", {"day": str(T.DAY), "season": [], "picks": [], "pronos": []})
    pronos = SL.make(scan, NOW, T.DAY)
    late = NOW.replace(hour=1) + timedelta(days=1)                    # 01:00 UTC le lendemain : now.date() = D + 1, le jour Kelly est encore D
    ko = (late + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = store.load(SL.EVENTS_FILE, {})
    for e in doc["events"]:
        e["commence_time"] = ko
    store.save(SL.EVENTS_FILE, doc)
    legs, info = SL.rebuild(late, fetch_without(SL.make({"leagues": {k: [dict(e, commence_time=ko) for e in v] for k, v in scan["leagues"].items()}}, late, T.DAY)))
    assert info["events"] > 0 and info["read"] > 0, info
    assert all(r["date"] == str(T.DAY) for r in store.load(SL.LOG, []) if r["id"] in {c["id"] for c in legs}), "lignes datées du jour Kelly"
    store.save("today.json", {"day": "2020-01-01", "season": [], "picks": [], "pronos": []})
    assert SL.rebuild(late, fetch_without(pronos))[1]["events"] == 0, "un autre jour Kelly : ignoré"
    print("ok  jour Kelly : après minuit UTC les matchs du soir sont toujours reconstruits, lignes datées du jour Kelly ; un autre jour est ignoré")


def test_command():
    scan = world()
    SL.remember_events(scan, T.DAY)
    pronos = SL.make(scan, NOW, T.DAY)
    today = {"day": str(T.DAY), "season": [], "picks": [], "pronos": [], "xbet_at": "09:30"}
    store.save("today.json", today)
    # beaucoup de matchs du jour : on ajoute des matchs de LDC pour que la cote 3 soit atteignable
    more = {"leagues": {CUP: [{"id": f"u{i}", "sport_key": CUP, "home_team": f"XeT{i % 8}", "away_team": f"YoT{(i + 2) % 8}", "commence_time": T.KO, "league": "LDC"} for i in range(8)]}}
    store.save(SL.EVENTS_FILE, {"day": str(T.DAY), "events": [e for lst in (scan["leagues"], more["leagues"]) for k, v in lst.items() for e in v if k != "soccer_other"]})
    pronos = SL.make({"leagues": {k: [e for e in v if e["commence_time"] > NOW.strftime("%Y-%m-%dT%H:%M:%SZ")] for k, v in
                                  {**{k: v for k, v in scan["leagues"].items() if k != "soccer_other"}, **more["leagues"]}.items()}}, NOW, T.DAY)
    out = app.season_command(["3"], today, now=NOW, fetch=fetch_without(pronos))
    txt = str(out)
    assert "SAISON EN COURS · cote 3" in txt and "🔎 " in txt and "matchs lus chez 1xBet" in txt and "de coupe" in txt and "sous 4 matchs" in txt and "sans prix" in txt, txt[:700]
    labels = [b["text"] for r in out.markup["inline_keyboard"] for b in r]
    assert any("Je le joue" in t for t in labels) and not any("Un autre" in t for t in labels), labels
    # 1xBet muet : les jambes du matin, dit clairement
    morning = [c | {"commence": T.KO} for c in SL.run(scan, [], T.DAY, NOW, fetch=fetch_without(pronos), history={})[0]]
    today["season"] = morning
    silent = str(app.season_command(["3"], today, now=NOW, fetch=lambda events, **kw: {}))
    assert "1xBet n'a rien rendu" in silent and "du matin" in silent and "🔎 0 match lu" in silent, silent[:600]
    # voie éteinte / cote invalide
    C.STRATEGY_SEASON = "off"
    try:
        assert "éteinte" in app.season_command(["3"], today, now=NOW, fetch=fetch_without(pronos))
    finally:
        C.STRATEGY_SEASON = "on"
    assert "Exemple" in app.season_command(["abc"], today, now=NOW)
    print("ok  saison N : jambes reconstruites (championnat + LDC), une ligne « 🔎 … » avec ce qui a été trouvé, bouton « Je le joue » sans « Un autre » ; 1xBet muet : jambes du matin dites")


def main():
    real = (season.collect, SL.known_leagues)
    try:
        test_remember_and_rebuild()
        test_cap_and_failures()
        test_kelly_day_after_midnight_utc()
        test_monthly_cap_guard()
        test_fallback_on_world_cache()
        test_command()
    finally:
        season.collect, SL.known_leagues = real
    print("tests saison N (reconstruction à la demande) : OK")


if __name__ == "__main__":
    main()
