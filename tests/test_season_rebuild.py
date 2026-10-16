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
REAL_COLLECT = season.collect


def world():
    """Deux championnats suivis (X, Y), une équipe de X avec seulement 2 matchs (Zed), puis les matchs du jour : un de championnat, trois de LDC, un commencé, un hors liste."""
    T.setup()
    x = rename(league(10, 5, seed=1, mean_h=1.9, mean_a=1.5, corners=(6.5, 5.5), disp=0.1), "Xe") + [row("Zed", "2026-09-20", 1, goals=(1, 1), corners=(5, 5)), row("Zed", "2026-09-27", 0, goals=(0, 1), corners=(5, 5)),
                                                                                                       row("XeT0", "2026-09-20", 0, goals=(1, 1), corners=(5, 5))]
    y = rename(league(10, 5, seed=2, mean_h=1.2, mean_a=0.9, corners=(4.5, 3.5), disp=0.1), "Yo")
    obs = {"soccer_x": x, "soccer_y": y}
    season.collect = lambda sk, on, loaders=None, docs=None: (obs.get(sk, []), "football-data" if sk in obs else None)
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
    first, second = line.split("\n")
    assert first == "🔎 2 matchs lus chez 1xBet sur 5 non commencés (dont 1 de coupe) · 1 équipe sous 4 matchs (Zed) · 1 match sans prix · 1 équipe non reconnue (Inconnu FC) · 1 match de ligues non suivies", first
    assert second.startswith("📊 2 matchs · 1 famille analysée · 11 lignes évaluées · ") and second.endswith("non balayées : touches, hors-jeu, joueurs"), second
    many = dict(info, few={f"E{i}" for i in range(7)}, unknown=set(), other=0)
    assert "7 équipes sous 4 matchs (E0, E1, E2, E3…)" in SL.summary_line(many), SL.summary_line(many)
    assert [r for r in store.load(SL.LOG, []) if r["id"] in ("e1", "e3")], "lignes journalisées (CLV par jambe)"
    print("ok  reconstruction : matchs non commencés (coupes comprises) relus en direct, bilan exact (lus, de coupe, équipes sous 4 matchs, sans prix, non reconnues), journalisé")


def multi_fetch(pronos):
    """Faux 1xBet qui chiffre TOUTES les familles des pronostics (plus/moins et oui/non) : un match a plusieurs marchés intéressants."""
    def fetch(events, **kw):
        out = {}
        for k, ev in events:
            px = {}
            for c in pronos:
                if c["id"] != ev["id"]:
                    continue
                for name, pt, *_ in c["alts"]:
                    if name in ("Over", "Under"):
                        px[(c["market"], "Over", pt)], px[(c["market"], "Under", pt)] = 2.3, 1.7
                    elif name in ("Yes", "No"):
                        px[(c["market"], "Yes", pt)], px[(c["market"], "No", pt)] = 1.9, 1.9
            out[ev["id"]] = {"gid": 7000 + sum(map(ord, ev["id"])) % 1000, "px": px, "refs": {k_: [7000, 1 + i, 0] for i, k_ in enumerate(px)}}
        return out
    return fetch


def test_several_markets_per_match():
    """Un match peut avoir PLUSIEURS marchés intéressants : le balayage les garde tous (un combiné n'en prend qu'un par match) et le dit."""
    scan = world()
    SL.remember_events(scan, T.DAY)
    pronos = SL.make({"leagues": {k: [e for e in v if e["id"] not in ("e2", "e7")] for k, v in scan["leagues"].items()}}, NOW, T.DAY)
    legs, info = SL.rebuild(NOW, multi_fetch(pronos))
    assert info["families"] >= 4 and info["lines"] > 20 and info["legs"] > info["legs_matches"] >= 2 and info["per_match_max"] >= 2, info
    by = {}
    for c in legs:
        by.setdefault(c["id"], set()).add(c["market"])
    assert any(len(v) >= 2 for v in by.values()), by
    line = SL.summary_line(info).split("\n")[1]
    assert f"{info['families']} familles analysées" in line and f"{info['lines']} lignes évaluées" in line and "jambes à valeur sur" in line and "matchs avec plusieurs)" in line and "jusqu'à" not in line, line
    assert info["value_matches"] >= 2 and info["value_multi"] >= 1 and f"{info['value_legs']} jambes à valeur sur {info['value_matches']} matchs ({info['value_multi']} match" in line, (info, line)
    # le combiné, lui, ne prend qu'un pari par match
    combo = CB_plan(legs)
    assert combo and len({l["id"] for l in combo[0]}) == len(combo[0])
    print("ok  balayage : plusieurs marchés retenus par match (familles, lignes évaluées, jambes retenues dites), un seul par match dans le combiné")


def CB_plan(legs):
    from kelly import combos as CB
    return next((c for _, c in CB.season_plan(legs, [3]) if c), None)


def test_combi_sweep_line():
    """« combi N » : la même ligne de balayage (familles analysées, lignes évaluées, jambes possibles, familles non balayées)."""
    pr = lambda i, m, name, pt, alts, **kw: {"key": f"m{i}|{m}||{pt}|{name}", "id": f"m{i}", "market": m, "name": name, "point": pt, "xodds": 1.8, "prob": 0.7, "min_odds": 1.5, "conf": 0.8,     # noqa: E731
                                          "alts": [[name, pt, 0.7, None]] * alts, "home": "A", "away": "B", "sport_key": "soccer_epl"} | kw
    pronos = [pr(1, "totals", "Over", 2.5, 3), pr(1, "alternate_totals_corners", "Over", 9.5, 4), pr(1, "btts", "Yes", None, 2), pr(2, "totals", "Over", 2.5, 3), pr(2, "team_shots_total", "Over", 24.5, 5)]
    line = app.sweep_line([], pronos)
    assert line.startswith("📊 2 matchs · 4 familles analysées · 17 lignes évaluées · "), line
    assert "non balayées :" in line and "touches" in line and "hors-jeu" in line, line
    missing = line.split("non balayées :")[1]
    assert "corners" not in missing and "les 2 marquent" not in missing and "tirs," not in missing, "les familles chiffrées ne sont pas dites non balayées"
    assert app.sweep_line([], [dict(p, xodds=None) for p in pronos]) == "", "sans cote 1xBet : pas de ligne"
    from kelly.telegram import Html
    r = Html("combiné")
    r.markup = {"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]}
    out = app._with_sweep(r, line)
    assert isinstance(out, Html) and out.markup == r.markup and "📊 2 matchs" in str(out) and str(out).startswith("combiné"), out
    assert app._with_sweep("texte", line).endswith(line) and app._with_sweep(r, "") is r
    print("ok  « combi N » : ligne 📊 (matchs, familles analysées, lignes évaluées, jambes possibles, familles non balayées), message et boutons inchangés")


def test_many_leagues_load_documents_once():
    """Beaucoup de ligues connues (FotMob, 365scores...) : cup_models charge chaque document UNE fois, jamais une fois par ligue (sinon `saison N` pend sur le téléphone)."""
    from kelly import apistats
    from kelly.sources import fmmatch, s365
    world()
    season.collect = REAL_COLLECT                                      # le vrai collect (le monde de test l'avait remplacé) : il lit les documents stockés
    SL.known_leagues = lambda _filtered=True: [f"soccer_l{i}" for i in range(60)]
    season.reset()
    calls = {"n": 0}
    reals = (fmmatch.load, s365.load, apistats.load)

    def counting(load):
        def f():
            calls["n"] += 1
            return load()
        return f
    fmmatch.load, s365.load, apistats.load = (counting(x) for x in reals)
    try:
        SL.cup_models(T.DAY)
    finally:
        fmmatch.load, s365.load, apistats.load = reals
    assert calls["n"] == 3, calls                                     # un chargement par source, pas 3 x 60 ligues
    print("ok  60 ligues connues : trois documents de sources chargés une seule fois")


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
        test_several_markets_per_match()
        test_combi_sweep_line()
        test_many_leagues_load_documents_once()
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
