"""Carte des edges (kelly/edgemap.py), hors ligne, sur journaux FICTIFS : case verte, rouge, grise, trop mince (99 lignes, 10 matchs), cascade vers l'agrégat, populations (stats / saison à
valeur / coupes, une clé une fois), chiffres (réalisé, annoncé, gain théorique), règle des deux lundis, mécanisme éteint par défaut et sans effet sur le journal, constantes = pré-déclaration.
Lancer : python -m tests.test_edgemap"""
import hashlib
import json
import math
import os
import tempfile
from datetime import date, timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-edgemap-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-edgemap-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY"):
    os.environ.pop(k, None)

from kelly import config as C, cupstats, edgemap as E, ensemble, lanes, seasonlane, store  # noqa: E402

EPL, LIGA, SERIA, BUN, L1 = ("soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a", "soccer_germany_bundesliga", "soccer_france_ligue_one")
LEDGER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "edgemap_ledger.json")
MONDAY = date(2026, 10, 19)


def row(tag, i, league, market="team_corners_total", name="Over", base=0.03, matches=50, strategy="stats", result="win", **kw):
    """Une ligne réglée du journal : prise à 2,00, dernier prix 1xBet tel que le CLV vaille base +- 0,02 (alternance : l'intervalle n'est jamais nul)."""
    c = base + (0.02 if i % 2 else -0.02)
    r = {"key": f"{tag}-{league}-{i}", "id": f"{tag}-{league}-{i % matches}", "sport_key": league, "market": market, "name": name, "point": 9.5, "result": result, "xodds": 2.0,
         "xclose": {"odds": 2.0 / math.exp(c), "read": "x", "lead_min": 30, "susp": False}, "strategy": strategy, "pm": 0.55, "date": "2026-10-15"}
    return r | kw


def rows_for(tag, n, league, **kw):
    return [row(tag, i, league, **kw) for i in range(n)]


def put_journal(stats=(), season=(), cups=()):
    store.save(ensemble.LOG_FILE, list(stats))
    store.save(seasonlane.LOG, list(season))
    store.save(cupstats.LOG, list(cups))


def key(voie, league, fam, sens):
    return E.key_of(voie, league, fam, sens)


def test_colours_and_thresholds():
    stats = (rows_for("g", 120, EPL, base=0.03) + rows_for("r", 120, LIGA, base=-0.03) + rows_for("y", 120, SERIA, base=0.0)
             + rows_for("t", 99, BUN, base=0.05) + rows_for("m", 120, L1, base=0.05, matches=10))
    put_journal(stats)
    t = E.compute()
    corners = lambda lg: t[key("stats", lg, "corners", "plus")]           # noqa: E731
    assert corners(EPL)["colour"] == "green" and corners(EPL)["readable"]
    assert corners(LIGA)["colour"] == "red" and corners(LIGA)["readable"]
    assert corners(SERIA)["colour"] == "grey" and corners(SERIA)["readable"], "indécise : lisible mais zéro dans l'intervalle"
    assert corners(BUN)["colour"] == "grey" and not corners(BUN)["readable"], "99 lignes : jamais verte, même à +5 %"
    assert corners(L1)["colour"] == "grey" and not corners(L1)["readable"], "120 lignes sur 10 matchs : trop peu de matchs distincts"
    agg = t[key("stats", "*", "corners", "plus")]
    assert agg["agg"] and agg["n"] == 579 and agg["readable"], agg
    print("ok  carte : verte, rouge, grise (indécise), 99 lignes et 10 matchs jamais lisibles")


def test_cascade():
    thin = [rows_for(f"c{k}", 60, lg, market="team_fouls_total", base=0.03, matches=25) for k, lg in enumerate((EPL, LIGA, SERIA, BUN, L1, "soccer_netherlands_eredivisie"))]
    thin_other = [r for k, lg in enumerate(("soccer_portugal_primeira_liga", "soccer_belgium_first_div", "soccer_turkey_super_league"))
                  for r in rows_for(f"o{k}", 50, lg, market="team_cards_total", base=0.05, matches=40)]          # 3 ligues x 50 lignes : cases minces, agrégat de 150 < 300
    put_journal([r for g in thin for r in g] + thin_other)
    t = E.compute()
    assert not t[key("stats", EPL, "fouls", "plus")]["readable"], "60 lignes : case trop mince"
    k, s = E.resolve(t, "stats", EPL, "fouls", "plus")
    assert k == key("stats", "*", "fouls", "plus") and s["colour"] == "green" and s["nc"] == 360, (k, s)
    k2, s2 = E.resolve(t, "stats", "soccer_portugal_primeira_liga", "cards", "plus")
    assert k2 is None and s2 is None, "150 lignes en tout : ni la case (50), ni l'agrégat (300)"
    print("ok  cascade : case mince -> agrégat (voie, famille, sens) lisible ; trop mince partout -> grise")


def test_populations_and_numbers():
    sea = [
        {"key": "s1|saison", "id": "s1", "sport_key": EPL, "market": "totals", "name": "Over", "result": "win", "xodds": 2.0, "xclose": {"odds": 1.9, "lead_min": 30, "susp": False},
         "strategy": "saison", "sval": 1, "pe": 0.60, "pm": 0.5},
        {"key": "s2|saison", "id": "s2", "sport_key": EPL, "market": "totals", "name": "Over", "result": "loss", "xodds": 1.5, "xclose": {"odds": 1.5, "lead_min": 30, "susp": False},
         "strategy": "saison", "sval": 1, "pe": 0.70, "pm": 0.6},
        {"key": "s3|saison", "id": "s3", "sport_key": EPL, "market": "totals", "name": "Over", "result": "win", "xodds": 2.0, "strategy": "fill", "sval": 0},       # remplissage : hors carte
        {"key": "s4|saison", "id": "s4", "sport_key": EPL, "market": "totals", "name": "Over", "result": "win", "xodds": 2.0, "strategy": "saison", "ref": 1},         # référence : hors carte
        {"key": "s5|saison", "id": "s5", "sport_key": "soccer_uefa_champs_league", "market": "team_corners_total", "name": "Under", "result": "win", "xodds": 2.0,
         "strategy": "saison", "sval": 1, "cup": True, "pe": 0.5},
        {"key": "s6|saison", "id": "s6", "sport_key": EPL, "market": "totals", "name": "Over", "result": None, "xodds": 2.0, "strategy": "saison", "sval": 1},          # non réglée
    ]
    cups = [row("cup", 0, "soccer_uefa_champs_league", market="team_fouls_total", name="Over", cup=True), row("cup", 1, "soccer_uefa_champs_league", market="team_fouls_total", name="Over", cup=True, strategy="fill")]
    stats = [row("x", 0, EPL, cup=True), row("x", 1, EPL), row("x", 1, EPL)]          # une ligne `cup` n'est jamais de la voie stats ; une même clé n'est comptée qu'une fois
    put_journal(stats, sea, cups)
    by = E.voie_rows()
    assert [r["key"] for r in by["saison"]] == ["s1|saison", "s2|saison"], by["saison"]
    assert [r["key"] for r in by["saison_cup"]] == ["s5|saison"]
    assert len(by["stats"]) == 1 and len(by["stats_cup"]) == 1 and by["stats_cup"][0]["strategy"] == "stats"
    s = E.measure(by["saison"])
    assert s["n"] == 2 and s["real"] == 0.5 and abs(s["ann"] - 0.65) < 1e-9, s                  # annoncé = pe (proba retenue)
    assert s["gain"] == 0.0, s                                                                   # (2,0 - 1) - 1 = 0
    assert s["nc"] == 2 and s["matches"] == 2 and s["h"] is not None, s          # s1 : +5 % ; s2 : prix pris = dernier prix, 0 mesuré
    st = E.measure(by["stats"])
    assert abs(st["ann"] - 0.55) < 1e-9, "stats : pm (modèle), le journal ne garde pas la proba retenue"
    assert E.sens_of("Over") == "plus" and E.sens_of("under") == "moins" and E.sens_of("Yes") == "autre" and E.sens_of(None) == "autre"
    print("ok  populations : stats `stats` hors coupe, saison `sval == 1`, coupes à part, une clé une fois ; réalisé, annoncé (pe sinon pm), gain théorique")


def test_two_mondays():
    put_journal(rows_for("g", 120, EPL, base=0.03))
    store.save(E.STATE_FILE, {})
    E.reset()
    assert E.apply(MONDAY - timedelta(days=1)) is False and not store.load(E.STATE_FILE, {}), "un dimanche : rien n'est évalué ni écrit"
    assert E.apply(MONDAY) is True
    k = key("stats", EPL, "corners", "plus")
    d = store.load(E.STATE_FILE, {})
    assert d["first"] == str(MONDAY) and d["colours"][k]["c"] == "grey" and d["colours"][k]["raw"] == "green" and d["watch"][k]["cand"] == "green", d
    nxt = MONDAY + timedelta(days=7)
    assert E.apply(nxt) and store.load(E.STATE_FILE, {})["colours"][k]["c"] == "green", "deux lundis de suite : la couleur change"
    # un lundi manqué casse la chaîne
    put_journal(rows_for("g", 120, EPL, base=-0.03))
    assert E.apply(nxt + timedelta(days=7)) and store.load(E.STATE_FILE, {})["colours"][k]["c"] == "green", "candidat rouge vu une fois : la verte reste"
    assert E.apply(nxt + timedelta(days=21)) and store.load(E.STATE_FILE, {})["colours"][k]["c"] == "green", "lundi du 4 manqué : la chaîne repart de zéro"
    assert E.apply(nxt + timedelta(days=28)) and store.load(E.STATE_FILE, {})["colours"][k]["c"] == "red", "deux lundis consécutifs rouges"
    assert store.load(E.STATE_FILE, {})["first"] == str(MONDAY) and not any(v["c"] == "green" for v in store.load(E.STATE_FILE, {})["colours"].values() if not v["r"])
    print("ok  lundi seulement ; une couleur change après deux lundis consécutifs, un lundi manqué casse la chaîne ; jamais verte sans lecture")


def test_gate_off_by_default_and_effect():
    assert C.EDGEMAP_GATE is False, "éteint par défaut"
    put_journal(rows_for("g", 120, EPL, base=0.03) + rows_for("r", 120, LIGA, base=-0.03) + rows_for("y", 120, SERIA, base=0.0))
    store.save(E.STATE_FILE, {})
    E.reset()
    E.apply(MONDAY)
    E.apply(MONDAY + timedelta(days=7))
    leg = lambda lg, **kw: {"key": f"k-{lg}", "id": "m", "sport_key": lg, "market": "team_corners_total", "name": "Over", "strategy": "stats", "prob": 0.6} | kw      # noqa: E731
    old_gate, old_today, old_ens = C.EDGEMAP_GATE, E._today, C.ENSEMBLE_ENABLED
    try:
        C.ENSEMBLE_ENABLED = True
        lanes.reset()
        assert lanes.usable(leg(LIGA)), "mécanisme éteint : la jambe d'une case rouge reste jouable"
        C.EDGEMAP_GATE = True
        E._today = lambda: MONDAY + timedelta(days=7 + 10)
        assert lanes.usable(leg(LIGA)), "allumé mais moins de 28 jours depuis le premier lundi : sans effet"
        for w in (14, 21, 28):
            E.apply(MONDAY + timedelta(days=w))                  # lundis évalués de suite : l'état reste frais
        E._today = lambda: MONDAY + timedelta(days=28)
        E.reset()
        assert not lanes.usable(leg(LIGA)), "case rouge : fantôme"
        E._today = lambda: MONDAY + timedelta(days=28 + 15)
        E.reset()
        assert lanes.usable(leg(LIGA)), "état enregistré vieux de plus de 14 jours (lundis manqués) : le mécanisme n'agit plus"
        E._today = lambda: MONDAY + timedelta(days=28)
        E.reset()
        assert lanes.usable(leg(EPL)) and lanes.usable(leg(SERIA)), "verte et grise : comportement actuel"
        assert lanes.usable(leg(LIGA, strategy="fill")), "une jambe de remplissage n'est jamais touchée"
        assert lanes.usable(leg(LIGA, market="totals", strategy="sharpgap")), "autre voie : jamais touchée"
        assert lanes.usable(leg(L1)), "case inconnue : grise"
        sea = {"key": "s", "id": "m", "sport_key": LIGA, "market": "team_corners_total", "name": "Over", "strategy": "saison", "lane": "saison", "sval": 1, "prob": 0.6}
        assert lanes.usable(sea) and lanes.usable(sea | {"sval": 0})
    finally:
        C.EDGEMAP_GATE, E._today, C.ENSEMBLE_ENABLED = old_gate, old_today, old_ens
        lanes.reset()
        E.reset()
    print("ok  mécanisme : éteint par défaut, attend 28 jours, bloque seulement les cases rouges, jamais fill ni autres voies")


def test_red_leg_still_journaled():
    """Une case rouge continue d'alimenter le journal : le journal ne passe jamais par lanes.usable (sinon la case ne pourrait plus changer de couleur)."""
    import inspect
    assert "usable" not in inspect.getsource(ensemble.record) and "edgemap" not in inspect.getsource(ensemble.record)
    put_journal()
    store.save(E.STATE_FILE, {"first": str(MONDAY), "week": str(MONDAY + timedelta(days=35)), "colours": {key("stats", LIGA, "corners", "plus"): {"c": "red", "r": True, "voie": "stats", "league": LIGA, "fam": "corners", "sens": "plus"}}})
    old_gate, old_today = C.EDGEMAP_GATE, E._today
    try:
        C.EDGEMAP_GATE, E._today = True, (lambda: MONDAY + timedelta(days=40))
        E.reset()
        c = {"key": "z|team_corners_total||9.5|Over", "id": "z", "sport_key": LIGA, "home": "h", "away": "a", "commence": "2099-01-01T00:00:00Z", "market": "team_corners_total", "name": "Over",
             "point": 9.5, "prob": 0.6, "p0": 0.6, "p_model": 0.6, "p_x": 0.55, "p_sharp": 0.57, "xodds": 1.9, "strategy": "stats", "xref": [1, 2, 9.5]}
        assert not lanes.usable(c), "case rouge : jamais choisie"
        n = ensemble.record([c], "2026-10-20", None)
        assert n == 1 and any(r["key"] == c["key"] for r in store.load(ensemble.LOG_FILE, [])), "mais bien journalisée"
    finally:
        C.EDGEMAP_GATE, E._today = old_gate, old_today
        E.reset()
    print("ok  une jambe d'une case rouge reste au journal (la case peut changer de couleur)")


def test_report_and_weekly():
    put_journal(rows_for("g", 120, EPL, base=0.03) + rows_for("r", 120, LIGA, base=-0.03))
    store.save(E.STATE_FILE, {})
    E.reset()
    before = E.report(["stats"])
    assert "pas encore évaluée un lundi" in before and "🟢" in before and "🔴" in before and "gain théorique" in before and "mécanisme (EDGEMAP_GATE) : éteint" in before, before
    E.apply(MONDAY)
    E.apply(MONDAY + timedelta(days=7))
    out = E.report(["stats"])
    assert "état du lundi 2026-10-26" in out and "🟢 corners · plus · " in out and "🔴 corners · plus · " in out and "par hasard seul" in out, out
    wk = "\n".join(E.weekly_lines())
    assert "stats : 2 cases lisibles (🟢 1 · 🔴 1 · ⚪ 0)" in wk and "mécanisme : éteint" in wk, wk
    store.save(E.STATE_FILE, {})
    E.reset()
    assert E.weekly_lines() == [], "sans état : pas de bloc"
    print("ok  commande carte (mesure du moment avant le premier lundi, état du lundi ensuite) et bloc hebdo lu dans l'état enregistré")


def test_hardening():
    """Amendement du ledger : CLV non fini ignoré, gain sur prix valides seulement, lecture vide = état conservé."""
    rows = rows_for("h", 4, EPL)
    rows[0]["xodds"] = float("nan")
    rows[1]["xclose"] = {"odds": float("inf"), "read": "x", "lead_min": 30, "susp": False}
    rows[2] |= {"xodds": 1.0}
    m = E.measure(rows)
    assert m["nc"] == 2 and m["clv"] is not None and math.isfinite(m["clv"]), m
    assert m["gain"] == 2.0, m                                       # lignes n°2 et n°4 (gagnées à 2,00) ; n°1 (nan) et n°3 (1,00) exclues
    put_journal(rows_for("e", 120, EPL, base=0.03))
    store.save(E.STATE_FILE, {})
    E.reset()
    assert E.apply(MONDAY)
    before = store.load(E.STATE_FILE, {})
    put_journal()
    assert not E.apply(MONDAY + timedelta(days=7)), "journal vide : rien n'est écrit"
    assert store.load(E.STATE_FILE, {}) == before, "l'état enregistré n'est pas écrasé"
    print("ok  durcissements : CLV non fini ignoré, gain sur prix valides, lecture vide ne détruit pas l'état")


def test_declaration_matches_code():
    led = json.load(open(LEDGER))
    pre = led["prereg"]
    assert hashlib.sha256(json.dumps(pre, sort_keys=True, ensure_ascii=False).encode()).hexdigest() == led["prereg_sha"], "pré-déclaration inchangée depuis son commit"
    th = pre["thresholds"]
    assert (th["case_min_clv_lines"], th["aggregate_min_clv_lines"], th["min_distinct_matches"]) == (E.CASE_MIN, E.AGG_MIN, E.MIN_MATCHES) == (100, 300, 30)
    assert "14 jours" in json.dumps(led["amendments"], ensure_ascii=False) and E.STALE_DAYS == 14
    assert E.GATE_WAIT_DAYS == 28 and E.VOIES == ("stats", "stats_cup", "saison", "saison_cup", "cotes") and "cinquième voie « cotes »" in json.dumps(led["amendments"], ensure_ascii=False)
    print("ok  seuils et voies du code = pré-déclaration (empreinte inchangée)")


def main():
    test_colours_and_thresholds()
    test_cascade()
    test_populations_and_numbers()
    test_two_mondays()
    test_gate_off_by_default_and_effect()
    test_hardening()
    test_red_leg_still_journaled()
    test_report_and_weekly()
    test_declaration_matches_code()
    print("tests carte des edges : OK")


if __name__ == "__main__":
    main()
