"""Correctifs de l'audit du 17/10 (lot A) : pré-déclaration audit_fixes_ledger.json, puis un bloc de tests par point (A3, A1, A4, A13, A15, A2).
Chaque test de défaut échoue sur le code d'avant et passe après. Lancer : python -m tests.test_audit_fixes"""
import hashlib
import json
import os
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


def main():
    test_declaration_unchanged()
    test_a1_double_chance_freshness()
    test_a3_measure_tool()
    test_a3_x_clv_own_read()
    test_a3_mark_x_close_and_record()
    test_a3_review_gaps()
    test_a3_oddsonly_close_skips_own_read()
    print("tests correctifs de l'audit : OK")


if __name__ == "__main__":
    main()
