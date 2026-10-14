"""Drapeaux de mesure des facteurs de contexte des buts (strength.ctx_parts, predict(ctx=...)) et comparaison des cinq configurations (tools/audit_ctx.py), hors
ligne, sur des CSV football-data fictifs : ctx=True garde les cinq facteurs (comportement de production inchangé), ctx=False et ctx=() n'en gardent aucun, une liste
garde ceux demandés, la combinaison des facteurs pris un par un redonne EXACTEMENT le contexte complet (hors plafond), un nom inconnu lève ValueError, et les cinq
configurations notent les mêmes matchs avec des prédictions qui diffèrent vraiment. Lancer : python -m tests.test_ctx_flags"""
import os
import sys
import tempfile
from datetime import date

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-ctxflags-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-ctxflags-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_ctx as X  # noqa: E402
import audit_replay as R  # noqa: E402

from kelly import strength as S  # noqa: E402
from kelly.sources import footballdata as fd  # noqa: E402
from tests import synth  # noqa: E402


def matches(seasons=("2324", "2425")):
    out = []
    for s in seasons:
        rows, probs = fd.parse_csv(synth.csv_for(s), "E0", s)
        assert not probs, probs
        out += rows
    return out


MS = matches()
ON = date(2025, 3, 8)


def test_parts():
    assert S.ctx_parts(True) == frozenset(S.CTX_PARTS) and S.ctx_parts(False) == S.ctx_parts(()) == S.ctx_parts(None) == frozenset()
    assert S.ctx_parts(["form", "rest"]) == frozenset({"form", "rest"})
    try:
        S.ctx_parts(["forme"])
    except ValueError:
        pass
    else:
        raise AssertionError("un nom inconnu doit lever ValueError")
    print("ok  ctx_parts : True = les cinq, False / () = aucun, liste = ceux demandés, nom inconnu refusé")


def test_predict_flags():
    model = S.build(MS, ON, S.HALF_LIFE, ("goals",), "E0")
    assert model
    teams = sorted(model["stats"]["goals"]["t"])
    checked = moved = 0
    for h, a in ((teams[0], teams[1]), (teams[2], teams[3]), (teams[4], teams[5]), (teams[6], teams[7]), (teams[8], teams[9])):
        full = S.predict(model, h, a, ON, None, True)
        same = S.predict(model, h, a, ON, None, S.CTX_PARTS)
        none = S.predict(model, h, a, ON, None, False)
        empty = S.predict(model, h, a, ON, None, ())
        assert (full["fh"], full["fa"], full["lh"], full["la"]) == (same["fh"], same["fa"], same["lh"], same["la"]), "ctx=True = les cinq facteurs"
        assert (none["fh"], none["fa"]) == (empty["fh"], empty["fa"]) == (1.0, 1.0) and none["lh"] == none["lh0"], "aucun facteur : la force pure"
        singles = {f: S.predict(model, h, a, ON, None, [f]) for f in S.CTX_PARTS}
        prod_h = prod_a = 1.0
        for p in singles.values():
            prod_h *= p["fh"]
            prod_a *= p["fa"]
        lo, hi = 1 - S.BOUND["total"], 1 + S.BOUND["total"]
        if lo < full["fh"] < hi and lo < full["fa"] < hi and lo < prod_h < hi and lo < prod_a < hi:      # hors plafond : le produit des facteurs seuls = le contexte complet
            assert abs(full["fh"] - prod_h) < 1e-9 and abs(full["fa"] - prod_a) < 1e-9, (full["fh"], prod_h)
            checked += 1
        moved += any(abs(p["fh"] - 1) > 1e-12 or abs(p["fa"] - 1) > 1e-12 for p in singles.values())
        no_h2h = S.predict(model, h, a, ON, None, [f for f in S.CTX_PARTS if f != "h2h"])
        assert all(n[0] != "h2h" for n in no_h2h["notes"]), "sans face-à-face : plus de note de face-à-face"
    assert checked >= 1 and moved >= 1, (checked, moved)
    print(f"ok  predict : ctx=True inchangé, False = force pure, produit des facteurs seuls = contexte complet ({checked} matchs hors plafond), notes filtrées")


def test_configs():
    loader = {"E0": MS}
    runs = X.collect(loader, R.WARMUP_END, R.RESERVED_FROM, None)
    assert list(runs) == [n for n, _ in X.CONFIGS] and len({tuple(r["id"] for r in v) for v in runs.values()}) == 1, "mêmes matchs pour les cinq configurations"
    full, off = runs[X.CONFIGS[0][0]], runs[X.CONFIGS[-1][0]]
    assert full and any(a["p3"] != b["p3"] for a, b in zip(full, off)), "ctx éteint change vraiment les prédictions"
    assert all(a["q_o3"] == b["q_o3"] and a["q_c3"] == b["q_c3"] for a, b in zip(full, off)), "le marché et la cible ne dépendent pas des facteurs"
    txt = X.report(runs, "test")
    for name, _ in X.CONFIGS:
        assert name in txt, name
    assert "configuration retenue par la règle" in txt and "comparaisons faites" in txt and "PAS une décision de production" in txt
    import io
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()):
        assert X.main(["--divs", "E0", "--reserved-from", "2027-01-01"], loader=lambda divs: loader) == 2, "audit_ctx ne lit pas la période réservée"
        assert X.main(["--divs", "E0", "--warmup-end", "2025-07-01"], loader=lambda divs: loader) == 2
    print("ok  cinq configurations : mêmes matchs, prédictions différentes, marché inchangé, règle de choix et nombre de comparaisons affichés")


def main():
    test_parts()
    test_predict_flags()
    test_configs()
    print("tests drapeaux de contexte : OK")


if __name__ == "__main__":
    main()
