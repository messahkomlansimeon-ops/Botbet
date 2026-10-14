"""Lecture unique de la saison réservée (tools/audit_final.py), hors ligne, sur des CSV football-data FICTIFS : la règle pré-déclarée s'applique mécaniquement (les deux
configurations passent -> la plus petite distance ; une seule -> celle-là ; aucune -> poids 0 sur le marché et ses dérivés), la pré-déclaration s'écrit une fois et ne se
modifie pas, --final refuse sans pré-déclaration, avec une pré-déclaration altérée, ou une deuxième fois, et n'écrit rien si le rejeu est partiel ; le fichier de trace est
la seule chose écrite. Lancer : python -m tests.test_audit_final"""
import contextlib
import io
import json
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-final-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-final-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_final as F  # noqa: E402

from kelly.sources import footballdata as fd  # noqa: E402
from tests import synth  # noqa: E402


def matches():
    out = []
    for s in ("2324", "2425", "2526"):
        rows, probs = fd.parse_csv(synth.csv_for(s), "E0", s)
        assert not probs, probs
        out += rows
    return out


MS = matches()


def cell(passes, kl_mix, n=100):
    return {"n": n, "kl_mix": kl_mix, "passes": passes, "log-loss": (-0.01, 0.005), "distance à la clôture": (-0.001, 0.0005)}


def test_decide():
    both = F.decide({"1N2": {"contexte éteint": cell(True, 0.0040), "contexte actuel": cell(True, 0.0035)},
                     "plus/moins 2,5": {"contexte éteint": cell(True, 0.0010), "contexte actuel": cell(True, 0.0020)}})
    assert both["1N2"]["config"] == "contexte actuel" and both["plus/moins 2,5"]["config"] == "contexte éteint" and both["1N2"]["weight"] == 0.12, "les deux passent : la plus petite distance"
    one = F.decide({"1N2": {"contexte éteint": cell(False, 0.004), "contexte actuel": cell(True, 0.005)},
                    "plus/moins 2,5": {"contexte éteint": cell(True, 0.002), "contexte actuel": cell(False, 0.001)}})
    assert one["1N2"]["config"] == "contexte actuel" and one["plus/moins 2,5"]["config"] == "contexte éteint", "une seule passe : celle-là, même si l'autre est plus proche"
    none = F.decide({"1N2": {"contexte éteint": cell(False, 0.004), "contexte actuel": cell(False, 0.005)},
                     "plus/moins 2,5": {"contexte éteint": cell(True, 0.002), "contexte actuel": cell(False, 0.001)}})
    assert none["1N2"] == {"weight": 0.0, "config": None, "why": none["1N2"]["why"], "derived_weight_zero": ["double_chance"]}, none
    assert none["plus/moins 2,5"]["weight"] == 0.12 and none["plus/moins 2,5"]["derived_weight_zero"] == [], "les marchés se décident séparément"
    decl = F.declaration()
    assert decl["comparisons"] == 8 and decl["derived"] == {"1N2": ["double_chance"], "plus/moins 2,5": ["btts", "team_totals_home", "team_totals_away"]}
    assert "à fixer" not in json.dumps(decl, ensure_ascii=False) and "PROVISOIRE" not in json.dumps(decl, ensure_ascii=False), "aucune valeur provisoire dans la pré-déclaration"
    assert "proxy" in decl["what_it_measures"].lower() and "suivent le plus/moins 2,5" in decl["team_totals"]
    print("ok  règle pré-déclarée : deux passent -> plus petite distance ; une -> celle-là ; aucune -> poids 0 + dérivés ; 8 comparaisons")


def run(args, ledger):
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = F.main(args, loader=lambda divs: {"E0": MS}, ledger=ledger)
    return code, buf.getvalue(), err.getvalue()


def test_guards_and_run():
    d = tempfile.mkdtemp(prefix="kelly-final-ledger-")
    ledger = os.path.join(d, "audit_reserve_ledger.json")
    assert run(["--final"], ledger)[0] == 2, "pas de pré-déclaration : refus"
    assert run([], ledger)[0] == 2 and run(["--final", "--write-prereg"], ledger)[0] == 2, "un seul mode à la fois"
    code, out, _ = run(["--write-prereg"], ledger)
    assert code == 0 and "À commiter AVANT" in out and os.path.exists(ledger)
    assert run(["--write-prereg"], ledger)[0] == 2, "la pré-déclaration ne se réécrit pas"
    doc = json.load(open(ledger))
    assert set(doc) == {"prereg", "prereg_sha", "written_at"} and doc["prereg_sha"] == F.sha(doc["prereg"])
    # une pré-déclaration altérée (même empreinte recalculée) est refusée : elle doit être celle du code
    alt = json.loads(json.dumps(doc))
    alt["prereg"]["min_matches"] = 5
    alt["prereg_sha"] = F.sha(alt["prereg"])
    alt_path = os.path.join(d, "alt.json")
    json.dump(alt, open(alt_path, "w"))
    assert run(["--final"], alt_path)[0] == 2 and "final" not in json.load(open(alt_path)), "altérée : refus et rien d'écrit"
    code, out, err = run(["--final"], ledger)
    assert code == 0, (out, err)
    for s in ("LECTURE UNIQUE", "8 comparaisons pré-déclarées", "DÉCISION", "observé, non testé"):
        assert s in out, s
    done = json.load(open(ledger))
    fin = done["final"]
    assert done["prereg"] == doc["prereg"] and set(fin["decision"]) == {"1N2", "plus/moins 2,5"} and fin["matches"] > 0
    assert all(set(fin["decision"][m]) >= {"weight", "config", "why"} for m in fin["decision"])
    assert sorted(os.listdir(d)) == ["alt.json", "audit_reserve_ledger.json"], "seul le fichier de trace est écrit"
    code, out, err = run(["--final"], ledger)
    assert code == 2 and "ne se relit pas" in err, "une saison réservée ne se relit pas"
    assert json.load(open(ledger))["final"] == fin, "la trace n'a pas bougé"
    print("ok  garde-fous : pas de pré-déclaration / altérée / deuxième lecture refusés ; une lecture écrit décision + empreinte dans le seul fichier de trace")


def test_hardening():
    d = tempfile.mkdtemp(prefix="kelly-final-hard-")
    ledger = os.path.join(d, "audit_reserve_ledger.json")
    assert run(["--write-prereg"], ledger)[0] == 0
    # plus d'option --ledger en ligne de commande : on ne peut pas viser un autre fichier pour relire la saison
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            F.main(["--final", "--ledger", os.path.join(d, "autre.json")], loader=lambda divs: {"E0": MS}, ledger=ledger)
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError("--ledger ne doit plus exister")
    # l'écriture précède l'affichage : si l'écriture plante, le verdict n'est PAS affiché et rien n'est marqué comme lu
    real = F.write_atomic
    F.write_atomic = lambda path, doc: (_ for _ in ()).throw(OSError("disque plein"))
    try:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                F.main(["--final"], loader=lambda divs: {"E0": MS}, ledger=ledger)
        except OSError:
            pass
        assert buf.getvalue() == "" and "final" not in json.load(open(ledger)), "plantage à l'écriture : aucun verdict vu, aucune lecture comptée"
        assert not os.path.exists(ledger + ".lock"), "le verrou est retiré même après un plantage"
    finally:
        F.write_atomic = real
    # un verrou déjà posé (autre lecture en cours ou interrompue) bloque
    open(ledger + ".lock", "w").close()
    code, out, err = run(["--final"], ledger)
    assert code == 2 and "déjà en cours" in err and "final" not in json.load(open(ledger))
    os.remove(ledger + ".lock")
    assert run(["--final"], ledger)[0] == 0
    print("ok  durcissement : pas d'option --ledger, écriture avant affichage, verrou contre une lecture simultanée ou interrompue")


def main():
    test_decide()
    test_guards_and_run()
    test_hardening()
    print("tests lecture unique de la saison réservée : OK")


if __name__ == "__main__":
    main()
