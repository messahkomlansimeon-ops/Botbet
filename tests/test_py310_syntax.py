"""Le téléphone tourne en Python 3.10 : ce que la 3.12+ accepte dans une f-string (barre oblique inverse ou guillemet identique DANS l'expression, f"{x["a"]}") y est une SyntaxError, et
`ast.parse(feature_version=...)` ne le voit pas. Contrôle direct de chaque f-string du dépôt, plus quelques API absentes de la 3.10. Lancer : python -m tests.test_py310_syntax"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAD_APIS = re.compile(r"\b(itertools\.batched|math\.sumprod|tomllib|datetime\.UTC|from datetime import [^\n]*\bUTC\b|typing\.Self|ExceptionGroup|asyncio\.TaskGroup|Path\([^)]*\)\.walk\()")


def files():
    for top in ("kelly", "tools", "tests", "scripts"):
        for dp, _, fs in os.walk(os.path.join(ROOT, top)):
            for f in fs:
                if f.endswith(".py"):
                    yield os.path.join(dp, f)
    for f in ("bot.py", "termux_runner.py"):
        yield os.path.join(ROOT, f)


def fstring_problems(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    offs = [0]
    for l in lines:
        offs.append(offs[-1] + len(l.encode("utf-8")))
    raw = src.encode("utf-8")
    out = []

    def seg(node):
        return raw[offs[node.lineno - 1] + node.col_offset:offs[node.end_lineno - 1] + node.end_col_offset].decode("utf-8")

    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        text = seg(node)
        m = re.match(r"[rRbBuUfF]*(\"\"\"|'''|\"|')", text)
        if not m:
            continue
        quote = m.group(1)
        for fv in (v for v in node.values if isinstance(v, ast.FormattedValue)):
            inner = seg(fv.value)
            if "\\" in inner:
                out.append((node.lineno, "barre oblique inverse dans l'expression"))
            if len(quote) == 1 and any(isinstance(n, ast.Constant) and isinstance(n.value, str) and seg(n)[-1:] == quote and seg(n)[:1] in (quote, "f") for n in ast.walk(fv.value)):
                out.append((node.lineno, f"guillemet {quote} réutilisé dans l'expression"))
    return out


def test_fstrings_and_apis():
    bad = []
    for path in files():
        rel = os.path.relpath(path, ROOT)
        if rel.endswith("test_py310_syntax.py"):
            continue
        for lineno, why in fstring_problems(path):
            bad.append(f"{rel}:{lineno} {why}")
        for i, line in enumerate(open(path, encoding="utf-8").read().splitlines(), 1):
            if BAD_APIS.search(line) and not line.lstrip().startswith("#"):
                bad.append(f"{rel}:{i} API absente de la 3.10 : {line.strip()[:80]}")
    assert not bad, "\n".join(bad)
    print(f"ok  aucune f-string 3.12+ ni API absente de la 3.10 (Python {sys.version_info.major}.{sys.version_info.minor} contrôle {sum(1 for _ in files())} fichiers)")


def test_detector_works():
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "x.py")
    open(p, "w").write("a = {'k': 1}\nb = f\"{a['k']} {', '.join(['x'])}\"\nc = f\"{', '.join(x for x in 'ab' if x != '\\n')}\"\nd = f'{a[\"k\"]}'\ne = f\"{'a' if a else 'b'}\"\n")
    got = fstring_problems(p)
    assert [l for l, _ in got] == [3], got           # seule la ligne 3 (barre oblique inverse) ; d, e et b sont valides en 3.10
    p2 = os.path.join(tempfile.mkdtemp(), "y.py")
    open(p2, "w").write("a = {'k': 1}\nf = f\"{a[\"k\"]}\"\n")
    if sys.version_info >= (3, 12):
        assert fstring_problems(p2), "guillemet réutilisé"
    print("ok  détecteur : barre oblique inverse et guillemet réutilisé repérés, f-strings valides laissées")


def main():
    test_detector_works()
    test_fstrings_and_apis()
    print("tests syntaxe Python 3.10 : OK")


if __name__ == "__main__":
    main()
