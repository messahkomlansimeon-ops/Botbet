"""Mémoire en base SQLite (data/kelly.db) : import unique des anciens fichiers JSON, export lisible / restauration, écritures de
plusieurs processus en même temps, document illisible. Lancer : python -m tests.test_db"""
import json
import os
import subprocess
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-db-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import store  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_migration_once():
    d = store.DATA_DIR
    os.makedirs(os.path.join(d, "archive"))
    for name, obj in (("bets.json", {"picks": [1], "combos": []}), ("archive/tracked-2025.json", [{"key": "a"}]),
                      ("stats_cache.json", {"1": {}})):
        with open(os.path.join(d, name), "w") as f:
            json.dump(obj, f)
    with open(os.path.join(d, "abime.json"), "w") as f:
        f.write("{pas du json")
    assert store.load("bets.json", None) == {"picks": [1], "combos": []}
    assert store.load("archive/tracked-2025.json", None) == [{"key": "a"}]
    assert not os.path.exists(os.path.join(d, "bets.json")) and os.path.exists(os.path.join(d, "sauvegarde", "bets.json"))
    assert os.path.exists(os.path.join(d, "stats_cache.json")) and not store.exists("stats_cache.json")   # cache de stats.py
    assert os.path.exists(os.path.join(d, "abime.json")) and not store.exists("abime.json")               # illisible : laissé
    assert not os.path.exists(os.path.join(d, "archive"))
    # une seule fois : un fichier déposé plus tard n'écrase rien
    with open(os.path.join(d, "bets.json"), "w") as f:
        json.dump({"picks": [], "combos": []}, f)
    assert store.load("bets.json", None) == {"picks": [1], "combos": []}
    os.remove(os.path.join(d, "bets.json"))
    print("ok  import unique des anciens fichiers")


def test_api():
    store.save("x.json", {"a": "é", "n": 1.5})
    v1 = store.version("x.json")
    store.save("x.json", {"a": "é", "n": 2})
    assert store.load("x.json", None) == {"a": "é", "n": 2} and store.version("x.json")[0] == v1[0] + 1
    assert store.exists("x.json") and store.size("x.json") == len('{"a":"é","n":2}'.encode())
    assert "x.json" in store.names("x") and store.names("zz") == []
    store.remove("x.json")
    assert not store.exists("x.json") and store.load("x.json", 7) == 7 and store.version("x.json") is None and store.size("x.json") == 0
    with store._db() as con:
        con.execute("INSERT OR REPLACE INTO docs (name, body, updated) VALUES ('casse.json', '{pas du json', 'x')")
    assert store.load("casse.json", {"ok": 1}) == {"ok": 1}                                         # illisible : valeur par défaut
    store.remove("casse.json")
    print("ok  lecture, écriture, version, taille")


def test_unreadable_not_overwritten():
    """Un document dont la lecture a raté sur ERREUR DE BASE n'est pas écrasé par le save() suivant : le défaut retourné par load()
    ne remplace jamais un contenu existant (une erreur passagère suivie d'une écriture effacerait de bonnes données). Un JSON
    abîmé, lui, est réparable : le save() suivant remplace le document."""
    store.save("bon.json", {"vrai": 1})
    old_db = store._db

    def base_ko():                                  # comme un verrou de base dépassé
        raise OSError("database is locked")
    store._db = base_ko
    assert store.load("bon.json", {"defaut": 1}) == {"defaut": 1}                                   # le bot continue (défaut)
    store.save("bon.json", {"defaut": 1})
    store._db = old_db
    assert store.load("bon.json", None) == {"vrai": 1}, "le contenu existant n'a pas été écrasé"    # contenu existant gardé
    store.save("bon.json", {"vrai": 2})                                                             # lecture réussie : écriture normale
    assert store.load("bon.json", None) == {"vrai": 2}
    # JSON abîmé : le save suivant répare
    with store._db() as con:
        con.execute("INSERT OR REPLACE INTO docs (name, body, updated) VALUES ('casse.json', '{pas du json', 'x')")
    assert store.load("casse.json", {"defaut": 1}) == {"defaut": 1}
    store.save("casse.json", {"reparé": 1})
    assert store.load("casse.json", None) == {"reparé": 1}
    # une restauration remplace délibérément, même ce qui n'a pas pu être lu
    store._db = base_ko
    assert store.load("casse.json", {"autre": 1}) == {"autre": 1}
    store.save("casse.json", {"autre": 1})
    store._db = old_db
    assert store.load("casse.json", None) == {"reparé": 1}, "le contenu n'a pas été écrasé"
    # un fichier illisible du dossier de sauvegarde ne stoppe plus la restauration ; les caches (NO_EXPORT) ne reviennent pas
    src = tempfile.mkdtemp(prefix="kelly-db-import2-")
    with open(os.path.join(src, "casse.json"), "w") as f:
        json.dump({"restauré": 2}, f)
    with open(os.path.join(src, "bon.json"), "w") as f:
        f.write("{pas du json")
    with open(os.path.join(src, "progress.json"), "w") as f:
        json.dump({"en cours": 1}, f)
    assert store.import_dir(src) == 1 and store.load("casse.json", None) == {"restauré": 2}
    assert not store.exists("progress.json")
    store.remove("casse.json")
    store.remove("bon.json")
    print("ok  lecture ratée sur erreur de base : pas d'écrasement")


def test_load_doc():
    """Un document existant d'une autre forme (dict attendu, liste ou champ d'un autre type) compte aussi comme absent."""
    store.save("forme.json", {"picks": [], "combos": []})
    assert store.load_doc("forme.json", {"picks": [], "combos": []}) == {"picks": [], "combos": []}
    store.save("forme.json", [])
    assert store.load_doc("forme.json", {"picks": [], "combos": []}) == {"picks": [], "combos": []}     # liste au lieu du dict
    store.save("forme.json", {"picks": 3})
    assert store.load_doc("forme.json", {"picks": [], "combos": []}) == {"picks": [], "combos": []}     # champ du mauvais type
    store.save("forme.json", {"picks": [], "combos": [1, 2]})
    assert store.load_doc("forme.json", {"combos": []}) == {"picks": [], "combos": [1, 2]}              # champs en plus : gardés
    store.save("forme.json", {"combos": "x"})
    assert store.load_doc("forme.json", {"combos": []}) == {"combos": []}
    store.remove("forme.json")
    print("ok  document malformé : comme absent, réparable")
    src = tempfile.mkdtemp(prefix="kelly-db-import-")
    with open(os.path.join(src, "casse.json"), "w") as f:
        json.dump({"restauré": 1}, f)
    assert store.import_dir(src) == 1 and store.load("casse.json", None) == {"restauré": 1}
    store.remove("casse.json")
    store.remove("bon.json")
    print("ok  lecture ratée sur erreur de base : pas d'écrasement")


def test_export_import():
    store.save("strength_E0.json", {"cache": 1})
    store.save("archive/tracked-2026.json", [{"key": "b"}])
    out = tempfile.mkdtemp(prefix="kelly-db-export-")
    with open(os.path.join(out, "perime.json"), "w") as f:
        f.write("{}")
    n = store.export(out)
    files = sorted(os.path.relpath(os.path.join(r, f), out) for r, _, fs in os.walk(out) for f in fs)
    assert "bets.json" in files and "archive/tracked-2026.json" in files and "strength_E0.json" not in files, files
    assert "perime.json" not in files and n == len(files)
    with open(os.path.join(out, "bets.json")) as f:
        assert json.load(f) == {"picks": [1], "combos": []}
    mtime = os.path.getmtime(os.path.join(out, "bets.json"))
    store.export(out)
    assert os.path.getmtime(os.path.join(out, "bets.json")) == mtime                                # inchangé : pas réécrit
    store.save("bets.json", {"picks": [], "combos": []})
    assert store.import_dir(out) == n and store.load("bets.json", None) == {"picks": [1], "combos": []}
    print("ok  export lisible et restauration")


def test_concurrent_processes():
    """4 processus x 50 écritures de documents différents + lectures : rien de perdu, aucune erreur de verrou."""
    code = ("import sys; from kelly import store\n"
            "w = sys.argv[1]\n"
            "for i in range(50):\n"
            "    store.save(f'p{w}.json', {'i': i}); store.load('bets.json', None)\n")
    env = dict(os.environ, PYTHONPATH=ROOT)
    procs = [subprocess.Popen([sys.executable, "-c", code, str(w)], env=env, stderr=subprocess.PIPE) for w in range(4)]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()
    assert all(store.load(f"p{w}.json", None) == {"i": 49} for w in range(4))
    print("ok  plusieurs processus en même temps")


def main():
    test_migration_once()
    test_api()
    test_unreadable_not_overwritten()
    test_load_doc()
    test_export_import()
    test_concurrent_processes()
    print("tests base de données : OK")


if __name__ == "__main__":
    main()
