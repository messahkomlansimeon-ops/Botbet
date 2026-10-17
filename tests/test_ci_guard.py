"""Garde de « kelly update » (scripts/kelly) : n'installe pas un commit dont la CI GitHub n'est pas verte. Quatre cas avec une réponse simulée (KELLY_CI_FIXTURE, aucun réseau) :
verte / rouge / en cours / injoignable, plus --force. Lancer : python -m tests.test_ci_guard"""
import json
import os
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "kelly")
D = tempfile.mkdtemp(prefix="kelly-ci-")


def run(cmd, body, *args):
    fx = os.path.join(D, "fx.json")
    open(fx, "w").write(body if isinstance(body, str) else json.dumps(body))
    env = {**os.environ, "KELLY_CI_FIXTURE": fx, "KELLY_CI_REPO": "o/r", "HOME": D}
    p = subprocess.run(["bash", SCRIPT, cmd, "abc123", *args], capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, p.stdout.strip()


def runs(*pairs):
    return {"total_count": len(pairs), "check_runs": [{"status": s, "conclusion": c} for s, c in pairs]}


def test_states():
    cases = [(runs(("completed", "success")), "verte"), (runs(("completed", "success"), ("completed", "failure")), "rouge"),
             (runs(("in_progress", None)), "en cours"), (runs(("completed", "success"), ("queued", None)), "en cours"), (runs(), "en cours"),
             (runs(("completed", "cancelled")), "rouge"), (runs(("completed", "failure"), ("in_progress", None)), "rouge"), ("unreachable", "injoignable"), ("pas du json", "injoignable"),
             ({"message": "Not Found"}, "injoignable")]
    for body, want in cases:
        assert run("ci-state", body) == (0, want), (body, run("ci-state", body), want)
    print("ok  état de la CI : verte / rouge / en cours / injoignable (réponses simulées, y compris vide, annulée, JSON abîmé)")


def test_gate():
    code, out = run("ci-gate", runs(("completed", "success")))
    assert code == 0 and out == "✅ CI : verte", (code, out)
    code, out = run("ci-gate", runs(("completed", "failure")))
    assert code == 1 and out.startswith("CI : rouge") and "refusée" in out and "--force" in out, (code, out)
    code, out = run("ci-gate", runs(("in_progress", None)))
    assert code == 1 and out.startswith("CI : en cours") and "refusée" in out, (code, out)
    code, out = run("ci-gate", "unreachable")
    assert code == 0 and "CI : injoignable" in out and "j'installe quand même" in out, (code, out)
    for body, word in ((runs(("completed", "failure")), "rouge"), (runs(("in_progress", None)), "en cours")):
        code, out = run("ci-gate", body, "--force")
        assert code == 0 and f"CI : {word}" in out and "--force" in out, (code, out)
    print("ok  garde : verte installe, rouge et en cours refusent avec le message, injoignable installe avec avertissement, --force passe outre")


def test_update_wiring():
    s = open(SCRIPT, encoding="utf-8").read()
    upd = s[s.index("    update)"):s.index("    guard)")]
    assert upd.index('ci_gate "$new" "$force"') < upd.index("keep_mirror") < upd.index("git pull"), "la CI est vérifiée AVANT de toucher au dépôt"
    assert "git fetch" in upd and "'@{u}'" in upd
    print("ok  « kelly update » : la CI du dernier commit de la branche est vérifiée avant keep_mirror et git pull")


def main():
    test_states()
    test_gate()
    test_update_wiring()
    print("tests garde CI : OK")


if __name__ == "__main__":
    main()
