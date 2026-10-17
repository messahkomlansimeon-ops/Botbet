"""Correctifs de l'audit du 17/10 (lot A) : pré-déclaration audit_fixes_ledger.json, puis un bloc de tests par point (A3, A1, A4, A13, A15, A2).
Chaque test de défaut échoue sur le code d'avant et passe après. Lancer : python -m tests.test_audit_fixes"""
import hashlib
import json
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-auditfix-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY", "XBET_PROXY_FILE"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

LEDGER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit_fixes_ledger.json")


def test_declaration_unchanged():
    led = json.load(open(LEDGER, encoding="utf-8"))
    assert hashlib.sha256(json.dumps(led["prereg"], sort_keys=True, ensure_ascii=False).encode()).hexdigest() == led["prereg_sha"], "pré-déclaration inchangée depuis son commit"
    assert {"A3", "A1", "A4", "A13", "A15", "A2", "extra_labels", "extra_avant_limite"} <= set(led["prereg"])
    print("ok  audit_fixes_ledger.json : pré-déclaration inchangée")


# ---------- A3 : CLV contre sa propre lecture ----------

def test_a3_x_clv_own_read():
    from kelly import ensemble, lanestats as LS
    base = {"xodds": 1.9, "commence": "2026-10-20T19:45:00Z"}
    close = lambda ts, odds=1.9, **k: {"odds": odds, "read_ts": ts, "lead_min": 55, "susp": False, "src": "cache"} | k
    tk = 1_792_263_000.0
    # ligne qui porte l'instant de sa prise (tk)
    assert LS.x_clv(base | {"tk": tk, "xclose": close(tk)}) is None, "lecture de la prise elle-même : pas une clôture"
    assert LS.x_clv(base | {"tk": tk, "xclose": close(tk - 30)}) is None, "lecture d'avant la prise : pas une clôture"
    v = LS.x_clv(base | {"tk": tk, "xclose": close(tk + 1, odds=1.9)})
    assert v == 0.0, "strictement après la prise, prix inchangé : un VRAI zéro, mesuré"
    assert LS.x_clv(base | {"tk": tk, "xclose": close(tk + 600, odds=1.7)}) > 0
    # lignes anciennes (sans tk) : cache + même prix = non mesuré ; close_read au même prix = vrai zéro
    assert LS.x_clv(base | {"xclose": close(tk)}) is None
    assert LS.x_clv(base | {"xclose": close(tk, src="close_read")}) == 0.0
    assert LS.x_clv(base | {"xclose": close(tk, odds=1.8)}) > 0, "prix différent : lu plus tard, mesuré"
    # voie cotes : first_at / xclose.read (même format)
    o = base | {"first_at": "2026-10-20T18:50+00:00"}
    assert LS.x_clv(o | {"xclose": {"odds": 1.9, "read": "2026-10-20T18:50+00:00", "lead_min": 55, "susp": False}}) is None
    assert LS.x_clv(o | {"xclose": {"odds": 1.7, "read": "2026-10-20T19:05+00:00", "lead_min": 40, "susp": False}}) > 0
    # xclose sans instant de lecture (track.py) : inchangé
    assert LS.x_clv(base | {"xclose": {"odds": 1.7, "age_min": 5, "lead_min": 40, "susp": False}}) > 0
    assert ensemble.not_a_close({"xclose": None}) is False
    print("ok  A3 : x_clv ne compare plus le prix pris à sa propre lecture (tk, first_at, lignes anciennes, vrais zéros gardés)")


def test_a3_mark_x_close_and_record():
    from datetime import datetime, timedelta, timezone
    from kelly import closeread, ensemble, store
    from kelly.sources import xbet
    now = datetime(2026, 10, 20, 18, 51, tzinfo=timezone.utc)
    ko = now + timedelta(minutes=54)
    c = {"key": "m|team_corners_total||9.5|Over", "id": "m", "sport_key": "soccer_epl", "home": "h", "away": "a", "commence": ko.isoformat(), "market": "team_corners_total",
         "name": "Over", "point": 9.5, "prob": 0.6, "p0": 0.6, "p_model": 0.6, "p_x": 0.55, "p_sharp": 0.57, "xodds": 1.9, "strategy": "stats", "xref": [1, 2, 9.5]}
    take = now - timedelta(seconds=40)
    store.save(ensemble.LOG_FILE, [])
    assert ensemble.record([c], "2026-10-20", take) == 1
    row = store.load(ensemble.LOG_FILE, [])[0]
    assert abs(row["tk"] - take.timestamp()) < 0.01, "l'instant de la prise est enregistré"
    old_cell, old_idx = xbet.cached_cell, closeread.index
    closeread.index = lambda: {}
    try:
        xbet.cached_cell = lambda ref: xbet.Cell(1.9, take.timestamp(), False)
        assert ensemble.mark_x_close(now) == 0 and "xclose" not in store.load(ensemble.LOG_FILE, [])[0], "relevé de l'instant de la prise : jamais posé"
        xbet.cached_cell = lambda ref: xbet.Cell(1.9, take.timestamp() - 600, False)
        assert ensemble.mark_x_close(now) == 0, "relevé d'avant la prise : jamais posé"
        xbet.cached_cell = lambda ref: xbet.Cell(1.8, take.timestamp() + 1, False)
        assert ensemble.mark_x_close(now) == 1 and store.load(ensemble.LOG_FILE, [])[0]["xclose"]["odds"] == 1.8, "relevé strictement postérieur : posé"
    finally:
        xbet.cached_cell, closeread.index = old_cell, old_idx
    print("ok  A3 : record garde l'instant de la prise ; mark_x_close ne pose jamais un relevé antérieur ou égal à la prise")


def test_a3_review_gaps():
    """Trous relevés par la relecture adverse : ts_of (Z, ISO naïf), archive aller-retour, lignes anciennes sans source, xclose de track.py."""
    from kelly import ensemble, lanestats as LS
    assert ensemble.ts_of("2026-10-17T18:50:00Z") == ensemble.ts_of("2026-10-17T18:50:00+00:00") == ensemble.ts_of("2026-10-17T18:50:00") == 1792263000.0
    assert ensemble.ts_of(None) is None and ensemble.ts_of(True) is None and ensemble.ts_of("pas une date") is None
    tk = 1_792_263_000.0
    base = {"key": "k", "market": "team_corners_total", "xodds": 1.9, "commence": "2026-10-17T19:45:00Z", "result": "win"}
    cl = lambda ts, odds=1.9, **k: {"odds": odds, "read_ts": ts, "lead_min": 55, "susp": False, "src": "cache"} | k
    # archive : un VRAI zéro (tk connu, relevé postérieur) reste mesuré après archivage ; une auto-comparaison n'est pas archivée
    good = ensemble.unpack_archive_row("k", "2026-10-17", ensemble._pack(base | {"tk": tk, "xclose": cl(tk + 600)}))
    assert good["xclose"]["ok"] and LS.x_clv(good) == 0.0, good
    own = ensemble.unpack_archive_row("k", "2026-10-17", ensemble._pack(base | {"tk": tk, "xclose": cl(tk)}))
    assert "xclose" not in own and LS.x_clv(own) is None
    old = ensemble.unpack_archive_row("k", "2026-10-17", ensemble._pack(base | {"xclose": cl(tk)}))          # ligne ancienne, cache, même prix : écartée (règle de repli)
    assert LS.x_clv(old) is None
    moved = ensemble.unpack_archive_row("k", "2026-10-17", ensemble._pack(base | {"xclose": cl(tk, odds=1.8)}))
    assert LS.x_clv(moved) > 0, "ligne ancienne au prix différent : mesurée"
    # ligne ancienne SANS source (xclose d'avant le champ src) : hors de la règle de repli du superviseur (src == « cache » exactement) -> mesurée comme avant ; écart signalé dans le ledger
    assert LS.x_clv(base | {"xclose": {"odds": 1.9, "read_ts": tk, "lead_min": 55, "susp": False}}) == 0.0
    assert LS.x_clv(base | {"xclose": {"odds": 1.9, "read_ts": tk, "lead_min": 55, "susp": False, "src": "close_read"}}) == 0.0
    # xclose de track.py (age_min, aucun instant) sur une ligne qui porte tk : non vérifiable, inchangé (limite assumée, ledger)
    assert LS.x_clv(base | {"tk": tk, "xclose": {"odds": 1.7, "age_min": 5, "lead_min": 40, "susp": False}}) > 0
    print("ok  A3 (relecture) : ts_of Z / naïf, archive aller-retour (vrai zéro gardé, auto-comparaison non archivée), lignes anciennes sans source (hors règle)")


def test_a3_oddsonly_close_skips_own_read():
    from kelly import oddsonly
    row = {"first_at": "2026-10-20T18:50+00:00", "xodds": 2.2, "xclose": None, "market": "totals", "name": "Over", "point": 2.5}
    m = {"px": {("totals", "Over", 2.5): 2.2}, "blocked": ()}
    oddsonly._close(row, m, ("totals", "Over", 2.5), "2026-10-20T18:50+00:00", 55)
    assert row["xclose"] is None, "la lecture de la détection n'est pas sa propre clôture"
    oddsonly._close(row, m, ("totals", "Over", 2.5), "2026-10-20T19:05+00:00", 40)
    assert row["xclose"]["odds"] == 2.2 and row["xclose"]["lead_min"] == 40
    print("ok  A3 : la voie cotes ne pose pas de xclose depuis sa propre lecture de détection")


def test_a3_measure_tool():
    import contextlib
    import importlib.util
    import io
    from kelly import ensemble, store
    spec = importlib.util.spec_from_file_location("audit_fixes_measure", os.path.join(os.path.dirname(LEDGER), "tools", "audit_fixes_measure.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tk = 1_792_263_000.0

    def row(i, **k):
        return {"key": f"k{i}", "id": f"i{i}", "date": "2026-10-20", "result": "win", "strategy": "stats", "xodds": 1.9, "at": "2026-10-17T18:50+00:00"} | k

    close = lambda ts, odds, **k: {"odds": odds, "read_ts": ts, "lead_min": 55, "susp": False, "src": "cache"} | k
    store.save(ensemble.LOG_FILE, [row(1, tk=tk, xclose=close(tk, 1.9)), row(2, tk=tk, xclose=close(tk + 900, 1.7)), row(3, xclose=close(tk + 900, 1.9)), row(4, xclose=close(tk, 1.9))])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        tool.a3()
    txt = out.getvalue()
    assert "lignes à xclose    4" in txt and "avant    4 -> après    1" in txt and "passent de mesuré à None    3" in txt and "vrais zéros écartés par la règle de repli : 1" in txt, txt
    print("ok  A3 : l'outil de mesure compte les zéros d'auto-comparaison et les vrais zéros écartés")


# ---------- A1 : double chance, fraîcheur du sharp ----------

def _a1_event(h2h_read="2026-10-17T04:00:00+00:00", dc_read="2026-10-17T15:00:00+00:00", with_h2h=True):
    H, A = "Alpha", "Beta"

    def bk(key, h2h, dc):
        ms = [{"key": "double_chance", "outcomes": [{"name": f"{H} or Draw", "price": dc[0]}, {"name": f"{H} or {A}", "price": dc[1]}, {"name": f"Draw or {A}", "price": dc[2]}]}]
        if with_h2h:
            ms.insert(0, {"key": "h2h", "outcomes": [{"name": H, "price": h2h[0]}, {"name": "Draw", "price": h2h[1]}, {"name": A, "price": h2h[2]}]})
        return {"key": key, "title": key, "markets": ms}
    return {"id": "e1", "sport_key": "soccer_epl", "commence_time": "2026-10-17T17:00:00Z", "home_team": H, "away_team": A,
            "bookmakers": [bk("pinnacle", (2.0, 3.5, 4.0), (1.28, 1.34, 1.87)), bk("b1", (1.9, 3.4, 3.8), (1.25, 1.3, 1.8)),
                           bk("b2", (1.95, 3.3, 3.9), (1.26, 1.3, 1.8)), bk("b3", (1.92, 3.3, 3.7), (1.26, 1.31, 1.85))],
            "read_at": h2h_read, "read_m": {"double_chance": dc_read}}


def test_a1_double_chance_freshness():
    from datetime import date, datetime, timezone
    from kelly import odds, sharpgap
    now = datetime(2026, 10, 17, 15, 30, tzinfo=timezone.utc)
    picks = odds.build_picks([_a1_event()], date(2026, 10, 17), None, {}, {})
    dc = next(c for c in picks if c["market"] == "double_chance" and c["name"] == "Alpha or Draw")
    h = next(c for c in picks if c["market"] == "h2h" and c["name"] == "Alpha")
    assert dc["sharp_at"] == h["sharp_at"] == "2026-10-17T04:00:00+00:00", (dc["sharp_at"], h["sharp_at"])
    assert dc.get("sharp_src") == "h2h" and "sharp_src" not in h
    dc["xodds"] = 1.38
    cand, why = sharpgap.judge(dc, {"picks_at": "15:25"}, now, {}, None)
    assert why == "stale" and cand["age_min"] == 690 and cand["sharp_src"] == "h2h", (why, cand and cand["age_min"])        # avant : why == '' et age_min == 30 (alerte 💰 sur un prix de 11 h 30 plus tôt)
    # 1X2 absent : la double chance garde sa propre lecture (consensus proportionnel de masse 2)
    picks = odds.build_picks([_a1_event(with_h2h=False)], date(2026, 10, 17), None, {}, {})
    dc = next(c for c in picks if c["market"] == "double_chance" and c["name"] == "Alpha or Draw")
    assert dc["sharp_at"] == "2026-10-17T15:00:00+00:00" and "sharp_src" not in dc
    # 1X2 plus récent que la double chance : la fraîcheur est celle du 1X2 (la proba vient de lui)
    picks = odds.build_picks([_a1_event(h2h_read="2026-10-17T15:20:00+00:00", dc_read="2026-10-17T10:00:00+00:00")], date(2026, 10, 17), None, {}, {})
    dc = next(c for c in picks if c["market"] == "double_chance" and c["name"] == "Alpha or Draw")
    assert dc["sharp_at"] == "2026-10-17T15:20:00+00:00"
    # lecture détaillée du 1X2 (read_m['h2h']) : elle l'emporte sur la lecture groupée read_at
    ev = _a1_event()
    ev["read_m"]["h2h"] = "2026-10-17T14:00:00+00:00"
    picks = odds.build_picks([ev], date(2026, 10, 17), None, {}, {})
    dc = next(c for c in picks if c["market"] == "double_chance" and c["name"] == "Alpha or Draw")
    assert dc["sharp_at"] == "2026-10-17T14:00:00+00:00"
    print("ok  A1 : la double chance prend l'heure de lecture du 1X2 d'où vient sa proba sharp (périmé -> stale)")


# ---------- A4 : probabilité corrigée bornée ----------

def test_a4_calibrated_probability_bounded():
    from kelly import calib, fair, store
    store.save("calib.json", {"goals": [100, 70, 60.0]})                  # famille sous-confiante : k = 1,2 (K_RANGE haut)
    calib.reset()
    assert calib.factor("totals") > 1.15
    ev = {"id": "x", "sport_key": "s", "home_team": "A", "away_team": "B", "commence_time": "2026-10-20T15:00:00Z"}
    hi = fair.make_pick(ev, "totals", "Over", 0.5, 0.95, "t", "s")        # avant : prob 1,04, min_odds 1,01
    lo = fair.make_pick(ev, "totals", "Over", 5.5, 0.02, "t", "s")        # avant : prob -0,076, fair -13,16, min_odds -13,82
    assert hi["prob"] == calib.P_MAX == 0.99 and hi["min_odds"] > 1 and hi["fair"] > 1
    assert lo["prob"] == calib.P_MIN == 0.01 and lo["min_odds"] > 1 and lo["fair"] > 1, lo
    assert calib.calibrate("totals", 0.5) == 0.5 and 0.01 <= calib.calibrate("totals", 0.6) <= 0.99, "pas de borne active au milieu : inchangé"
    assert calib.bound(1.04) == 0.99 and calib.bound(-0.076) == 0.01 and calib.bound(0.3) == 0.3
    # voie saison : même borne ; live : borne sauf résultat acquis (p0 = 0 ou 1)
    from kelly import seasonlane
    assert seasonlane.calibrated("totals", 0.95, "s", "plus", {"goals": [100, 70, 60.0]}) == 0.99
    assert seasonlane.calibrated("totals", 0.02, "s", "moins", {"goals": [100, 70, 60.0]}) == 0.01
    nan = float("nan")
    assert calib.bound(nan) != calib.bound(nan) and calib.bound(float("inf")) == float("inf"), "NaN / inf : inchangés (jamais une proba de 0,99)"
    import inspect
    from kelly import live as _live
    assert "corrected(p0" in inspect.getsource(_live.legs), "la correction live passe par live.corrected"
    assert calib.calibrate("totals", 0.995) == 0.99 and calib.calibrate("totals", 0.003) == 0.01, "proba brute hors bornes : ramenée dans [0,01 ; 0,99] (changement assumé, ledger)"
    from kelly import live
    assert live.corrected(0.95, 1.2) == 0.99 and live.corrected(0.02, 1.2) == 0.01 and live.corrected(0.6, 1.0) == 0.6
    assert abs(live.corrected(1.0, 1.2) - 1.1) < 1e-9 and abs(live.corrected(0.0, 1.2) + 0.1) < 1e-9, "résultat acquis (p0 0 ou 1) : pas touché par la borne"
    print("ok  A4 : la proba corrigée reste dans [0,01 ; 0,99], la cote minimale reste > 1 (calibrate, voie saison)")


# ---------- A13 : season_auto_plan, une jambe de tête ne bloque plus le plan ----------

def _a13_legs(*specs):
    return [{"id": str(i), "key": f"{i}|totals||2.5|Over|saison", "odds": o, "xodds": o, "prob": p, "lane": "saison", "sval": 1,
             "commence": f"2026-10-17T1{i}:00:00Z", "market": "totals", "name": "Over", "point": 2.5, "strategy": "saison"} for i, (p, o) in specs]


def test_a13_season_auto_plan_head_leg():
    from kelly import combos as CB, config as C, ensemble, lanes
    import signal
    old = (lanes.ticket_ok, ensemble.unsent)
    lanes.ticket_ok, ensemble.unsent = (lambda c: True), (lambda legs: False)
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(AssertionError("season_auto_plan ne se termine pas")))
        signal.alarm(20)                               # une boucle qui ne se termine plus échoue vite au lieu de bloquer la suite
    try:
        assert C.SEASON_AUTO_MIN_P == 0.25
        plan = lambda legs, **k: [(t, [c["id"] for c in x[0]], round(x[1], 2)) for t, x in CB.season_auto_plan(legs, CB.Usage(), **k)]
        bcd = [(2, (0.70, 1.6)), (3, (0.70, 1.6)), (4, (0.70, 1.6))]
        ref = plan(_a13_legs(*bcd))
        assert ref == [("ssa4.1", ["2", "3", "4"], 4.1)], ref
        # tête qui passe seule (p 0,30 >= 0,25) mais qui ne s'associe à aucune autre (0,30 x 0,70 = 0,21 < 0,25) : avant, [] ; maintenant le combiné des autres
        assert plan(_a13_legs((1, (0.30, 4.0)), *bcd)) == ref
        # tête qui échoue SEULE (p 0,20 < 0,25) : avant, `break` -> [] ; maintenant sautée
        assert plan(_a13_legs((1, (0.20, 8.0)), *bcd)) == ref
        # deux têtes bloquantes de suite : la boucle se termine et trouve quand même le combiné
        assert plan(_a13_legs((1, (0.30, 4.0)), (5, (0.22, 8.0)), *bcd)) == ref
        # rien ne peut former un combiné : liste vide, la boucle se termine
        assert plan(_a13_legs((1, (0.30, 4.0)), (5, (0.22, 8.0)))) == []
        assert plan(_a13_legs((2, (0.70, 1.6)))) == [], "une seule jambe : jamais de combiné"
        # une tête qui dépasse la cote maximale à elle seule est sautée
        big = _a13_legs((1, (0.60, C.SEASON_AUTO_MAX_ODDS + 5)), *bcd)
        assert plan(big) == ref, plan(big)
        # un combiné commencé se ferme à la première jambe qui ne passe pas (comme avant : les jambes de valeur passent avant le remplissage, test_season_auto) : une petite jambe en queue ne s'y glisse pas
        tail = _a13_legs((2, (0.70, 1.6)), (3, (0.70, 1.6)), (4, (0.70, 1.6)), (5, (0.70, 1.9)), (6, (0.99, 1.01)))
        tail[3]["sval"] = tail[4]["sval"] = 0          # F (échoue : p du combiné 0,24 < 0,25) puis G (passerait seule après F) : G ne se glisse pas dans le combiné de valeur
        got = plan(tail)
        assert got[0] == ref[0], got                  # le premier combiné reste [2, 3, 4] ; F et G forment le suivant, G ne se glisse pas dans le premier
        assert [x[1] for x in got[1:]] == [["5", "6"]], got
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
        lanes.ticket_ok, ensemble.unsent = old
    print("ok  A13 : une jambe de tête qui ne passe pas (seule ou à deux) n'arrête plus le plan saison ; la boucle se termine")


def test_a13_measure_tool():
    import contextlib
    import importlib.util
    import io
    from kelly import seasonlane, store
    spec = importlib.util.spec_from_file_location("audit_fixes_measure", os.path.join(os.path.dirname(LEDGER), "tools", "audit_fixes_measure.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    rows = [l | {"date": "2026-10-17", "pe": l["prob"], "result": None} for l in _a13_legs((1, (0.30, 4.0)), (2, (0.70, 1.6)), (3, (0.70, 1.6)), (4, (0.70, 1.6)))]
    store.save(seasonlane.LOG, rows)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        tool.a13()
    txt = out.getvalue()
    assert "avant 0 combiné(s) [] -> après 1 [4.1]" in txt and "passe de [] à au moins un combiné : 1" in txt, txt
    print("ok  A13 : l'outil de mesure rejoue les jours depuis le journal saison (avant / après)")


# ---------- A15 : un match = un point (means), repos jusqu'au jour du match (strength) ----------

def _a15_model():
    import math
    import random
    from datetime import date, timedelta
    from kelly import strength as S
    rnd = random.Random(1)
    teams = [f"T{i}" for i in range(8)]
    att = {t: math.exp(rnd.gauss(0, .25)) for t in teams}

    def pois(l):
        L, k, p = math.exp(-l), 0, 1.0
        while True:
            p *= rnd.random()
            if p < L:
                return k
            k += 1
    ms, d = [], date(2026, 2, 1)
    for r in range(40):
        rnd.shuffle(teams)
        for i in range(0, 8, 2):
            h, a = teams[i], teams[i + 1]
            m = dict(date=(d + timedelta(days=r * 4)).isoformat(), div="E0", home=h, away=a, hg=pois(1.5 * att[h]), ag=pois(1.2 * att[a]), referee="A Ref", odds={})
            m.update(hs=pois(12), **{"as": pois(10)}, hy=pois(2), ay=pois(2), hr=0, ar=0, hc=pois(5), ac=pois(4), hf=pois(11), af=pois(11), hst=pois(4), ast=pois(3))
            m["ftr"] = "H" if m["hg"] > m["ag"] else "A" if m["hg"] < m["ag"] else "D"
            ms.append(m)
    return S.build(ms, date(2026, 7, 1), div="E0"), S


def test_a15_means_one_match_one_point():
    from datetime import date
    from kelly import means, store
    store.save(means.FILE, {})
    means.reset()
    base = dict(id="ev1", market="team_shots_total", sport_key="soccer_epl", commence="2026-10-17T15:00:00Z", mu=24.0, val=26.0, result="win")
    # le même match suivi deux jours de suite (date = jour du suivi) : UN point (avant : deux, n gonflé)
    assert means.learn([dict(base, date="2026-10-16"), dict(base, date="2026-10-17", mu=24.5)], date(2026, 10, 18)) == 1
    assert means.load()["fam"]["shots"][0] == 1.0
    # une ancienne clé (jour de suivi) déjà comptée avant le correctif n'est pas recomptée
    store.save(means.FILE, {"fam": {"shots": [1.0, 26.0, 24.0]}, "lg": {}, "done": {"2026-10-16|ev9|team_shots_total": "2026-10-17"}, "t": "2026-10-17"})
    means.reset()
    row9 = dict(base, id="ev9", date="2026-10-16", commence="2026-10-17T15:00:00Z")
    assert means.learn([row9], date(2026, 10, 18)) == 0
    # deux matchs différents le même jour, ou le même match sur deux marchés : comptés séparément
    other = dict(base, id="ev2", date="2026-10-17")
    assert means.learn([other, dict(other, market="team_corners_total")], date(2026, 10, 18)) == 2
    print("ok  A15 : means compte un match une fois par marché (jour du match), sans recompter une ancienne clé")


def test_a15_rest_days_until_match_day():
    from datetime import date, timedelta
    from kelly import strength as S
    from kelly.sources import features
    M, S = _a15_model()
    last = date.fromisoformat(M["ctx"]["form"]["T1"]["ld"][-1])
    seen = {}
    real_get, real_predict, real_today = S.get, S.predict, S.today
    S.get = lambda div, on=None, loader=None: M
    S.today = lambda: last + timedelta(days=2)                       # « aujourd'hui » : 2 jours après son dernier match
    def predict(model, h, a, on=None, referee=None, **k):
        r = real_predict(model, h, a, on, referee, **k)
        seen["rest"] = None if r is None else r["info"]["rest"]
        return r
    S.predict = predict
    try:
        out = {"sources": []}
        features._deep(out, "T1", "T2", "soccer_epl", None, on=(last + timedelta(days=6)).isoformat() + "T15:00:00Z")
        assert seen["rest"]["home"] == (last + timedelta(days=6) - date.fromisoformat(M["ctx"]["form"]["T1"]["ld"][-1])).days == 6, seen
        features._deep({"sources": []}, "T1", "T2", "soccer_epl", None)            # sans jour de match : comme avant (aujourd'hui)
        assert seen["rest"]["home"] == 2, seen
        assert S.match_day("2026-10-18T15:00:00Z") == date(2026, 10, 18) and S.match_day(None) is None and S.match_day("x") is None
    finally:
        S.get, S.predict, S.today = real_get, real_predict, real_today
    import inspect
    from kelly import analysis, fair, odds, stats
    for mod, needle in ((stats, "on=c.get(\"commence\")"), (fair, "on=ev.get(\"commence_time\")"), (analysis, "on=free[0].get(\"commence\")"), (analysis, "on=first.get(\"commence\")"),
                        (analysis, "match_on=strength.match_day(first.get(\"commence\"))")):
        assert needle in inspect.getsource(mod), (mod.__name__, needle)
    assert "on=on" in inspect.getsource(odds.match_numbers)
    print("ok  A15 : les jours de repos se comptent jusqu'au jour du match (_deep, appelants de production), sans jour : comme avant")


def test_a15_review_gaps():
    """Trous de la relecture : match_features transmet `on`, /analyse ne reconstruit pas le modèle par jour de match, le repos n'est jamais négatif."""
    from datetime import date, timedelta
    from kelly import strength as S
    from kelly.sources import features
    M, S = _a15_model()
    last_h = date.fromisoformat(M["ctx"]["form"]["T1"]["ld"][-1])
    # match_features -> _deep : le jour du match arrive à predict
    got = {}
    names = ("_football_data", "_understat", "_fotmob", "_fbref", "_fmmatch", "_apistats", "_s365", "_deep")
    saved = {n: getattr(features, n) for n in names}
    for n in names:
        setattr(features, n, lambda out, *a, **k: None)
    features._deep = lambda out, home, away, sport_key, referee, on=None: got.update(on=on)
    try:
        features.match_features("T1", "T2", "soccer_epl", on="2026-10-18T15:00:00Z")
        assert got == {"on": "2026-10-18T15:00:00Z"}, got
    finally:
        for n, f in saved.items():
            setattr(features, n, f)
    # /analyse et market_grid : un seul modèle (celui du jour), `on` seulement pour predict
    calls = []
    real_get = S.get
    S.get = lambda div, on=None, loader=None: calls.append(on) or M
    try:
        S.market_grid("T1", "T2", "soccer_epl", match_on=last_h + timedelta(days=6))
        S.analysis_text("T1", "T2", "soccer_epl", None, match_on=last_h + timedelta(days=6))
        assert calls == [None, None], calls                # le modèle reste celui du jour : le jour du match ne sert qu'à predict
        rest = S.analysis_text("T1", "T2", "soccer_epl", None, match_on=last_h + timedelta(days=6))
        assert "Repos" in rest and " 6 j" in rest, rest
    finally:
        S.get = real_get
    # repos jamais négatif : jour visé antérieur à un match déjà au modèle
    p = S.predict(M, "T1", "T2", last_h - timedelta(days=4), ctx=["rest"])
    assert all(v >= 0 for v in p["info"]["rest"].values()), p["info"]["rest"]
    p = S.predict(M, "T1", "T2", last_h + timedelta(days=5), ctx=["rest"])
    assert p["info"]["rest"]["home"] == 5
    print("ok  A15 (relecture) : match_features transmet le jour du match, un seul modèle par jour pour /analyse, repos jamais négatif")


def test_a15_measure_tool():
    import contextlib
    import importlib.util
    import io
    from kelly import ensemble, store
    spec = importlib.util.spec_from_file_location("audit_fixes_measure", os.path.join(os.path.dirname(LEDGER), "tools", "audit_fixes_measure.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    base = {"id": "m1", "market": "team_shots_total", "name": "Over", "point": 9.5, "sport_key": "soccer_epl", "commence": "2026-10-18T15:00:00Z", "mu": 24.0, "val": 26.0, "result": "win", "prob": 0.6, "p0": 0.6}
    fair = [dict(base, key="k1", date="2026-10-17"), dict(base, key="k1", date="2026-10-18")]
    store.save("fair.json", fair)
    store.save(ensemble.LOG_FILE, [])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        tool.a15()
    txt = out.getvalue()
    assert "+1: 1" in txt and "+0: 1" in txt and "vues à plusieurs dates : 1/1" in txt, txt
    assert "clé d'avant (jour de suivi) : shots n 2" in txt and "clé d'après (jour du match) : shots n 1" in txt, txt
    store.save("fair.json", [])
    print("ok  A15 : l'outil de mesure (horizon, paires à deux dates, means avant / après)")


# ---------- A2 : prolongation, _cached_fstats ----------

def test_a2_extra_time_not_settled_on_120_minutes():
    from datetime import datetime, timedelta, timezone
    from kelly import ensemble, means, seasonlane, settle, stats
    from kelly.sources import fmmatch, s365
    saved = (fmmatch.match_stats, s365.match_stats, settle.watch_stats, settle.FootballData.stats, settle.FootballData.score, settle.fotmob_score,
             settle.FootballData.fotmob_score, settle.watch_score, settle.s365_score, stats.enabled, stats.find_fixture, stats.fixture_result, stats.cached_stats, stats.fixture_stats)
    fmmatch.match_stats = s365.match_stats = settle.watch_stats = lambda b: None
    settle.FootballData.stats = lambda self, b: None
    settle.FootballData.score = lambda self, b: None
    settle.fotmob_score = lambda b, leagues=None: None
    settle.FootballData.fotmob_score = lambda self, b: None
    settle.watch_score = lambda b: None
    settle.s365_score = lambda b, now=None: None
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    ko = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    entry = {"corners": 11.0, "cards": 3.0, "shots": 20.0, "sot": 8.0, "fouls": 20.0, "d": "2026-10-16",
             "teams": {"1": {"corners": 6.0, "shots": 12.0, "sot": 5.0, "fouls": 10.0, "cards": 1.0, "yel": 1.0, "nul": []},
                       "2": {"corners": 5.0, "shots": 8.0, "sot": 3.0, "fouls": 10.0, "cards": 2.0, "yel": 2.0, "nul": []}}}
    stats.enabled = lambda: True
    stats.find_fixture = lambda day, home, away, kickoff=None: {"id": 99, "home_id": 1, "away_id": 2, "status": "FT"}      # fixtures.json figé au premier passage : ne dit rien de la prolongation
    stats.cached_stats = lambda fid: entry
    stats.fixture_stats = lambda fid, players=True: entry
    set_status = lambda st: setattr(stats, "fixture_result", lambda day, home, away, kickoff=None: None if st is None else {"id": 99, "status": st, "finished": True, "home": 1, "away": 0})

    def row(market, point, key="k"):
        return {"key": key, "id": "e", "sport_key": "soccer_uefa_europa_league", "event": "A - B", "home": "A", "away": "B", "commence": ko, "market": market, "point": point,
                "name": "Under", "result": None, "date": ko[:10], "pm": 0.5, "px": 0.5, "mu": 9.0}
    try:
        fd = settle.FootballData()
        for market, point in (("alternate_totals_corners", 9.5), ("team_corners_home", 4.5)):
            set_status("AET")                                                 # 11 corners en 120 min, 8 à 90 min : avant, « loss » ; maintenant, non réglé sur ces stats
            r = row(market, point)
            settle._settle_fair_row(r, now, fd, settle._cached_fstats)
            assert r["result"] != "loss" and r.get("final") is None and not r.get("doubt"), r
            set_status("PEN")
            r = row(market, point)
            settle._settle_fair_row(r, now, fd, settle._cached_fstats)
            assert r["result"] != "loss", r
        set_status("FT")                                                      # temps réglementaire confirmé : réglé comme avant, aucune réserve
        r = row("alternate_totals_corners", 9.5)
        settle._settle_fair_row(r, now, fd, settle._cached_fstats)
        assert r["result"] == "loss" and not r.get("doubt"), r
        set_status(None)                                                      # statut introuvable (score donné par une autre source) : réglé mais DOUTEUX
        settle.watch_score = lambda b: {"id": b["id"], "completed": True, "scores": [{"name": "A", "score": "1"}, {"name": "B", "score": "0"}]}
        r = row("alternate_totals_corners", 9.5)
        settle._settle_fair_row(r, now, fd, settle._cached_fstats)
        assert r["result"] == "loss" and r["doubt"] is True, r
        # une ligne douteuse n'apprend rien (ensemble, voie saison, moyennes)
        ok = row("alternate_totals_corners", 9.5, "k_ok") | {"result": "loss", "final": "x", "val": 11.0}
        bad = dict(ok, key="k_bad", doubt=True)
        ens_before = ensemble.load()
        assert ensemble.learn([bad]) == 0 and ensemble.learn([ok]) == 1
        from datetime import date
        assert means.learn([bad], date(2026, 10, 18)) == 0 and means.learn([ok], date(2026, 10, 18)) == 1
        sea = dict(ok, key="s_ok", ref=1, ens=None)
        assert seasonlane.learn([dict(sea, doubt=True)]) == 0
    finally:
        (fmmatch.match_stats, s365.match_stats, settle.watch_stats, settle.FootballData.stats, settle.FootballData.score, settle.fotmob_score,
         settle.FootballData.fotmob_score, settle.watch_score, settle.s365_score, stats.enabled, stats.find_fixture, stats.fixture_result, stats.cached_stats, stats.fixture_stats) = saved
    print("ok  A2 : un match AET / PEN n'est plus réglé sur les stats de 120 min (journal), statut introuvable = règlement douteux exclu de l'apprentissage")


def test_a2_tools():
    import contextlib
    import importlib.util
    import io
    import json as _json
    from kelly import ensemble, store
    here = os.path.dirname(LEDGER)

    def load_tool(name):
        spec = importlib.util.spec_from_file_location(name, os.path.join(here, "tools", name + ".py"))
        t = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(t)
        return t
    tool = load_tool("audit_fixes_measure")
    store.save("fixtures_day.json", {"2026-10-16": [{"id": 1, "status": "AET", "home": "A", "away": "B"}, {"id": 2, "status": "FT", "home": "C", "away": "D"}]})
    row = {"key": "k", "market": "alternate_totals_corners", "home": "A", "away": "B", "commence": "2026-10-16T12:00:00Z", "result": "loss", "final": "11 corners", "name": "Under", "point": 9.5, "date": "2026-10-16"}
    store.save("fair.json", [row, dict(row, key="k2", home="C", away="D")])
    store.save(ensemble.LOG_FILE, [])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        tool.a2()
    assert "AET / PEN : 1" in out.getvalue() and "fair.json : lignes de stats sur un match AET / PEN : 1 dont réglées 1" in out.getvalue(), out.getvalue()
    store.save("fair.json", [])
    # lecture du cache 365scores : un statut inhabituel est listé
    import tempfile
    d = tempfile.mkdtemp()
    open(os.path.join(d, "k0.json"), "w").write(_json.dumps({"url": "https://webws.365scores.com/web/games/results/?x=1"}))
    open(os.path.join(d, "k0.bin"), "wb").write(_json.dumps({"games": [{"sportId": 1, "statusGroup": 4, "statusText": "Ended"},
                                                                         {"sportId": 1, "statusGroup": 4, "statusText": "After Extra Time", "homeCompetitor": {"name": "C", "score": 2}, "awayCompetitor": {"name": "D", "score": 1}}]}).encode())
    s365 = load_tool("audit_s365_status")
    old_argv = sys.argv
    sys.argv = ["x", "--cache-dir", d]
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            s365.main()
    finally:
        sys.argv = old_argv
    assert "'After Extra Time' : 1" in out.getvalue() and "statut inhabituel (ni Ended, ni Postponed / Suspended) : 1" in out.getvalue(), out.getvalue()
    print("ok  A2 : outils de mesure (matchs AET / PEN du journal) et de lecture des statuts 365scores du cache")


def main():
    test_declaration_unchanged()
    test_a1_double_chance_freshness()
    test_a4_calibrated_probability_bounded()
    test_a13_season_auto_plan_head_leg()
    test_a13_measure_tool()
    test_a15_means_one_match_one_point()
    test_a15_rest_days_until_match_day()
    test_a15_review_gaps()
    test_a15_measure_tool()
    test_a2_extra_time_not_settled_on_120_minutes()
    test_a2_tools()
    test_a3_measure_tool()
    test_a3_x_clv_own_read()
    test_a3_mark_x_close_and_record()
    test_a3_review_gaps()
    test_a3_oddsonly_close_skips_own_read()
    print("tests correctifs de l'audit : OK")


if __name__ == "__main__":
    main()
