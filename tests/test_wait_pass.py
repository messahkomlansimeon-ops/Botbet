"""« kelly update » / « kelly restart » (scripts/kelly) attendent un passage en cours (matin, horaire, suivi) jusqu'à 15 minutes avant de couper/installer ; --now ne les attend pas. Passage simulé par
KELLY_PASS_BUSY_FILE (fichier présent = en cours), délais ramenés à quelques secondes (KELLY_WAIT_MAX / KELLY_WAIT_STEP). Aucun réseau. Lancer : python -m tests.test_wait_pass"""
import os
import re
import subprocess
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "kelly")
D = tempfile.mkdtemp(prefix="kelly-wait-")
BUSY = os.path.join(D, "busy")


def run(*args, mx=10, step=1, busy_for=None):
    open(BUSY, "w").close()
    if busy_for is not None:
        threading.Timer(busy_for, lambda: os.path.exists(BUSY) and os.remove(BUSY)).start()
    env = {**os.environ, "HOME": D, "KELLY_PASS_BUSY_FILE": BUSY, "KELLY_WAIT_MAX": str(mx), "KELLY_WAIT_STEP": str(step)}
    t = time.time()
    p = subprocess.run(["bash", SCRIPT, "wait-pass", *args], capture_output=True, text=True, env=env, timeout=60)
    if os.path.exists(BUSY):
        os.remove(BUSY)
    return p.returncode, p.stdout.strip(), time.time() - t


def test_wait():
    open(BUSY, "w").close()
    os.remove(BUSY)
    env = {**os.environ, "HOME": D, "KELLY_PASS_BUSY_FILE": BUSY}
    p = subprocess.run(["bash", SCRIPT, "wait-pass"], capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0 and p.stdout.strip() == "", "aucun passage : immédiat, silencieux"
    code, out, dt = run(busy_for=2.5)
    assert code == 0 and "passage en cours, mise à jour dans" in out and out.endswith("✅ Passage terminé.") and 2 <= dt < 8, (code, out, dt)
    assert re.search(r"mise à jour dans [1-9] min", out)
    code, out, dt = run(mx=3, step=1)
    assert code == 0 and "toujours en cours" in out and "j'installe quand même" in out and 2.5 <= dt < 8, (out, dt)
    code, out, dt = run("--now")
    assert code == 0 and out == "" and dt < 2, "--now : on ne l'attend pas"
    print("ok  attente d'un passage : silencieuse sans passage, message « mise à jour dans N min » puis fin, 15 min (ici 3 s) puis on installe, --now immédiat")


def test_wiring():
    s = open(SCRIPT, encoding="utf-8").read()
    upd = s[s.index("    update)"):s.index("    guard)")]
    assert upd.index("ci_gate") < upd.index("wait_pass") < upd.index("keep_mirror") < upd.index("git pull"), "update : CI, puis attente du passage, puis miroir et git pull"
    assert 'exec "$PREFIX/bin/kelly" restart --now "$@"' in upd and "shift" in upd, "update a déjà attendu : le restart final ne ré-attend pas 15 min"
    rs = s[s.index("    restart)"):s.index("    update)")]
    assert rs.index("wait_pass") < rs.index('"$0" stop'), "restart : attendre avant d'arrêter"
    assert "pgrep -f \"bot\\.py (--daily|--track|--watch)\"" in s
    assert "--force" in upd and 'has_flag --force "$@"' in upd
    print("ok  câblage : update attend après la CI et avant git pull, restart avant stop, --force toujours lu")


def main():
    test_wait()
    test_wiring()
    print("tests attente d'un passage : OK")


if __name__ == "__main__":
    main()
