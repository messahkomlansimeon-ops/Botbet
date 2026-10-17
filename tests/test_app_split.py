"""Découpage d'app.py (17/10) : les fonctions déplacées (cmd_*, run_*, analysis, commands) restent joignables par `app.nom` (ré-exportées), le code déplacé lit app via A.nom AU MOMENT DE L'APPEL (un
`app.nom = ...` de test est vu), aucun nom indéfini ne s'y cache (un appel nu à une fonction restée dans app planterait seulement à l'exécution), et chaque fonction n'existe que dans un module.
Hors ligne. Lancer : python -m tests.test_app_split"""
import builtins
import os
import symtable
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-appsplit-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-appsplit-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY"):
    os.environ.pop(k, None)

from kelly import app  # noqa: E402
from kelly._appref import A  # noqa: E402

MODULES = ("cmd_season", "cmd_custom", "run_refresh", "run_check", "run_track", "analysis", "commands")
ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kelly")


def undefined_globals(path):
    src = open(path, encoding="utf-8").read()
    top = symtable.symtable(src, path, "exec")
    names = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
    bad = set()

    def walk(t):
        for s in t.get_symbols():
            if s.is_global() and s.is_referenced() and s.get_name() not in names and not hasattr(builtins, s.get_name()):
                bad.add((t.get_name(), s.get_name()))
        for c in t.get_children():
            walk(c)
    walk(top)
    return bad


def test_reexport_and_no_undefined():
    import importlib
    seen = {}
    for m in MODULES:
        mod = importlib.import_module(f"kelly.{m}")
        funcs = [n for n, v in vars(mod).items() if callable(v) and getattr(v, "__module__", None) == mod.__name__]
        assert funcs, m
        for n in funcs:
            assert n not in seen, f"{n} défini dans {seen.get(n)} et {m}"
            seen[n] = m
            assert getattr(app, n) is getattr(mod, n), f"app.{n} doit ré-exporter {m}.{n}"
        bad = undefined_globals(os.path.join(ROOT, f"{m}.py"))
        assert not bad, f"{m} : nom indéfini (appel nu à une fonction restée dans app ?) {sorted(bad)}"
    assert not (undefined_globals(os.path.join(ROOT, "app.py")) - {("load_sample", "__file__")}), "app.py : nom indéfini"
    assert len(seen) >= 90, len(seen)
    print(f"ok  {len(seen)} fonctions déplacées dans {len(MODULES)} modules, toutes ré-exportées par app, aucun nom indéfini")


def test_patch_on_app_is_seen_by_moved_code():
    old = app.now_utc
    try:
        app.now_utc = lambda: "patché"
        assert A.now_utc() == "patché", "A.nom lit app AU MOMENT DE L'APPEL"
        import inspect
        assert "A.now_utc()" in inspect.getsource(app.manual_refresh), "le code déplacé passe par A (un remplacement de test reste actif)"
    finally:
        app.now_utc = old
    assert A.now_utc() is not None and app.manual_refresh.__module__ == "kelly.run_refresh"
    print("ok  un remplacement `app.nom = ...` est vu par le code déplacé")


def main():
    test_reexport_and_no_undefined()
    test_patch_on_app_is_seen_by_moved_code()
    print("tests découpage d'app.py : OK")


if __name__ == "__main__":
    main()
