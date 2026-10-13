"""Rapport d'audit du CLV et du modèle contre le marché (tools/audit_clv.py), hors ligne, sur une base fictive : lecture seule d'une COPIE
(le dossier donné n'est jamais modifié), marge d'erreur par match (pas par ligne), poids 0 = marché seul, verdicts seulement quand l'échantillon est
assez grand et que la marge ne touche pas 0, aucun plantage sur une base vide ou sans tracked.json. Lancer : python -m tests.test_audit_clv"""
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-auditclv-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-auditclv-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_clv as A  # noqa: E402


def make_db(folder, tracked, log):
    con = sqlite3.connect(os.path.join(folder, "kelly.db"))
    con.execute("create table docs (name text primary key, body text, updated text, version integer)")
    con.execute("create table meta (k text, v text)")
    for name, body in (("tracked.json", tracked), ("ensemble_log.json", log)):
        con.execute("insert into docs values (?,?,?,?)", (name, json.dumps(body), "2030-01-01", 1))
    con.commit()
    con.close()


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def row(i, clv, sharpgap=False, sent=True, hour="04"):
    return {"key": f"k{i}", "id": f"m{i}", "market": "h2h", "date": "2030-01-01", "sent": sent, "odds": 2.0, "clv": clv, "result": "win",
            "closed_at": "2030-01-01T18:00+00:00", "taken_at": f"2030-01-01T{hour}:00+00:00", "sharpgap": sharpgap, "event": "A - B", "pick": "A"}


def test_stats():
    assert A.stats([]) == (0, None, None)
    n, m, h = A.stats([0.1, 0.1, 0.1, 0.1])
    assert (n, round(m, 6), h) == (4, 0.1, 0.0)
    # 4 lignes d'un SEUL match valent une preuve, pas quatre : sans seconde grappe, aucune marge ne peut être donnée
    assert A.stats([0.1, 0.2, 0.3, 0.4], ["m", "m", "m", "m"])[2] is None
    # la marge par match est plus large que la marge par ligne quand les lignes d'un match se ressemblent
    xs = [1, 1, 1, 1, -1, -1, -1, -1]
    by_line = A.stats(xs)[2]
    by_match = A.stats(xs, ["a", "a", "a", "a", "b", "b", "b", "b"])[2]
    assert by_match > by_line, (by_line, by_match)
    assert abs(A.pool(0.9, 0.4, 0.0) - 0.4) < 1e-9, "poids 0 = le marché seul"
    assert abs(A.pool(0.9, 0.4, 1.0) - 0.9) < 1e-9, "poids 1 = le modèle seul"
    assert A.logloss(0.5, True) == A.logloss(0.5, False) == -math.log(0.5)
    assert A.logloss(0.0, True) < 20 and A.logloss(1.0, False) < 20, "jamais de log(0)"
    print("ok  statistiques : marge par match, poids 0 = marché seul, jamais de log(0)")


def test_report():
    tracked = [row(i, 0.02 if i % 2 else -0.01, sharpgap=(i == 0), sent=(i % 3 == 0), hour="04" if i < 20 else "13") for i in range(40)]
    log = []
    for i in range(90):
        win = i % 2 == 0
        log.append({"key": f"l{i}", "id": f"g{i // 3}", "market": "alternate_totals_corners", "result": "win" if win else "loss", "date": "2030-01-01",
                    "pm": 0.8 if win else 0.2, "px": 0.55 if win else 0.45, "ps": None, "prob": 0.8 if win else 0.2})   # modèle meilleur que le marché
    with tempfile.TemporaryDirectory() as d:
        make_db(d, tracked, log)
        before = digest(os.path.join(d, "kelly.db"))
        rows, lg = A.load(d)
        assert len(rows) == 40 and len(lg) == 90
        txt = A.report(rows, lg)
        assert digest(os.path.join(d, "kelly.db")) == before and sorted(os.listdir(d)) == ["kelly.db"], "le dossier donné n'est jamais modifié"
    for head in ("A. CE QUE MESURE LE CLV", "B. CLV", "C. LE RETARD DE 1xBET", "D. LE MODÈLE CONTRE LE MARCHÉ SEUL", "E. RYTHME RÉEL", "F. CE QUE CE RAPPORT"):
        assert head in txt, head
    assert "cote typique" in txt and "PAS ton prix 1xBet" in txt and "Aucune clôture 1xBet n'est enregistrée" in txt, txt
    fam = txt[txt.index("D. LE MODÈLE"):txt.index("E. RYTHME")]
    assert "piste : le modèle AIDE" in fam and "NUIT" not in fam and "comparaisons faites" in fam, fam      # le modèle est meilleur ici, et seulement à poids > 0
    assert "heure" in txt and "04h" in txt and "13h" in txt
    print("ok  rapport : 6 sections, lecture seule d'une copie, verdict « le modèle AIDE » seulement hors référence")


def test_empty():
    with tempfile.TemporaryDirectory() as d:
        assert A.load(d) == ([], [])                       # pas de kelly.db
        make_db(d, [], [])
        rows, lg = A.load(d)
        txt = A.report(rows, lg)                            # base vide : aucun plantage
        assert "aucune ligne réglée" in txt
        assert A.main(["--data-dir", d]) == 1           # rien à rapporter : message sur stderr, code 1
    with tempfile.TemporaryDirectory() as d:
        assert A.main(["--data-dir", d]) == 1
    print("ok  base absente ou vide : aucun plantage, code de sortie propre")


def main():
    test_stats()
    test_report()
    test_empty()
    print("tests audit CLV : OK")


if __name__ == "__main__":
    main()
