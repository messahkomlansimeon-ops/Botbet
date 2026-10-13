"""Sauvegarde de la base sur Telegram et restauration (hors ligne). Automatique : sans sonnerie, zip daté, légende (paris notés, capital, marche à
suivre) ; base abîmée jamais envoyée ; planning du runner (tous les 2 jours dès 12h UTC, retentée dans l'heure si ratée) ; restauration : zip vérifié,
ancienne base gardée, un fichier qui n'est pas une sauvegarde de Kelly ne touche à rien ; la plus récente des téléchargements trouvée toute seule ;
`bot.py --restore`. Lancer : python -m tests.test_backup"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-backup-test-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

import termux_runner as R  # noqa: E402
from kelly import backup, store, telegram  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 15, 12, 5, tzinfo=UTC)
OUT = tempfile.mkdtemp(prefix="kelly-backup-out-")


def capture():
    """Remplace telegram.send_document : copie le zip envoyé dans OUT -> liste des (chemin copié, légende, options)."""
    sent = []

    def fake(path, caption=None, chat_id=None, **kw):
        dst = os.path.join(OUT, os.path.basename(path))
        shutil.copyfile(path, dst)
        sent.append((dst, caption, kw))
        return {"message_id": 1}
    return sent, fake


def test_send_auto():
    store.save("bets.json", {"picks": [], "combos": []})
    store.save("notes.json", {"hello": "monde"})
    sent, fake = capture()
    old = telegram.send_document
    telegram.send_document = fake
    try:
        assert backup.send(auto=True, now=NOW) is None
        assert backup.send(now=NOW) is None                                 # « backup » à la main : comme avant
    finally:
        telegram.send_document = old
    (path, cap, kw), (path2, cap2, kw2) = sent
    assert os.path.basename(path) == "kelly_backup_2026-10-15.zip" and kw == {"silent": True}, (path, kw)
    assert cap.startswith("💾 Sauvegarde automatique · 15/10/2026 12:05 UTC · ") and "kelly restaurer" in cap and ".env" in cap, cap
    assert os.path.basename(path2) == "kelly_backup.zip" and kw2 == {} and "Sauvegarde Kelly" in cap2, (path2, kw2, cap2)
    with zipfile.ZipFile(path) as zf:
        assert zf.namelist() == ["kelly.db"]
    print("ok  sauvegarde automatique : zip daté, sans sonnerie, légende avec la marche à suivre ; « backup » à la main inchangé")


def test_never_sends_broken():
    bad = os.path.join(OUT, "pas-une-base.db")
    with open(bad, "wb") as f:
        f.write(b"ceci n'est pas une base SQLite" * 100)
    try:
        backup._check(bad)
        raise AssertionError("fichier abîmé accepté")
    except backup.BackupError:
        pass
    empty = os.path.join(OUT, "vide.db")
    with sqlite3.connect(empty) as con:
        con.execute("CREATE TABLE docs (name TEXT PRIMARY KEY, body TEXT)")
    try:
        backup._check(empty)
        raise AssertionError("base vide acceptée")
    except backup.BackupError as e:
        assert "vide" in str(e)
    old_check, old_doc = backup._check, telegram.send_document
    backup._check = lambda db: (_ for _ in ()).throw(backup.BackupError("base abîmée (page 12)"))
    telegram.send_document = lambda *a, **k: (_ for _ in ()).throw(AssertionError("envoi d'une base abîmée"))
    try:
        assert backup.send(auto=True) == "⚠️ Sauvegarde non envoyée : base abîmée (page 12)"
    finally:
        backup._check, telegram.send_document = old_check, old_doc
    print("ok  une base abîmée ou vide n'est jamais envoyée (ni acceptée à la restauration)")


def test_schedule():
    st = R.new_state()
    names = lambda now, e=1e9: [a[0] for a in R.due_tasks(now, st, e)]          # noqa: E731
    assert "--backup-auto" not in names(datetime(2026, 10, 15, 11, 59, tzinfo=UTC))
    assert "--backup-auto" in names(datetime(2026, 10, 15, 12, 5, tzinfo=UTC))
    R.mark_done(st, ["--backup-auto"], datetime(2026, 10, 15, 12, 5, tzinfo=UTC), 1000.0, 1)          # ratée (réseau)
    assert "--backup-auto" not in names(datetime(2026, 10, 15, 12, 40, tzinfo=UTC), 1000.0 + 1800)
    assert "--backup-auto" in names(datetime(2026, 10, 15, 13, 5, tzinfo=UTC), 1000.0 + 3600)       # retentée dans l'heure
    R.mark_done(st, ["--backup-auto"], datetime(2026, 10, 15, 13, 5, tzinfo=UTC), 4600.0, 0)
    assert "--backup-auto" not in names(datetime(2026, 10, 16, 12, 5, tzinfo=UTC))                    # le lendemain : non
    assert "--backup-auto" not in names(datetime(2026, 10, 17, 11, 0, tzinfo=UTC))
    assert "--backup-auto" in names(datetime(2026, 10, 17, 12, 0, tzinfo=UTC))                        # 2 jours après
    assert "--backup-auto" in names(datetime(2026, 10, 20, 22, 0, tzinfo=UTC))                        # téléphone éteint : dès son retour
    path = os.path.join(OUT, "runner_state.json")
    R.save_state(st, path)
    assert R.load_state(path)["backup_day"] == "2026-10-15"                                        # gardé après un redémarrage
    assert R.TIMEOUTS["--backup-auto"] <= 900
    print("ok  planning : tous les 2 jours dès 12h UTC, retentée toutes les heures si l'envoi rate, rattrapée au retour du téléphone")


def test_restore():
    sent, fake = capture()
    old = telegram.send_document
    telegram.send_document = fake
    store.save("notes.json", {"hello": "avant"})
    try:
        assert backup.send(auto=True, now=NOW) is None
    finally:
        telegram.send_document = old
    zip_path = sent[0][0]
    store.save("notes.json", {"hello": "après la perte"})
    store.save("autre.json", [1, 2, 3])
    msg = backup.restore(zip_path, now=NOW)
    assert msg.startswith("✅ Sauvegarde restaurée (") and "avant-restauration-20261015-120500" in msg, msg
    assert store.load("notes.json", {}) == {"hello": "avant"} and not store.exists("autre.json")
    kept = store.db_path() + ".avant-restauration-20261015-120500"
    with sqlite3.connect(kept) as con:
        assert con.execute("SELECT COUNT(*) FROM docs WHERE name='autre.json'").fetchone()[0] == 1     # l'ancienne base est gardée
    # pas une sauvegarde de Kelly : rien n'est touché
    junk = os.path.join(OUT, "photos.zip")
    with zipfile.ZipFile(junk, "w") as zf:
        zf.writestr("photo.jpg", b"jpeg")
    before = os.path.getmtime(store.db_path())
    for bad, why in ((junk, "ne contient pas kelly.db"), (os.path.join(OUT, "absent.zip"), "introuvable")):
        try:
            backup.restore(bad)
            raise AssertionError(f"{bad} accepté")
        except backup.BackupError as e:
            assert why in str(e), e
    assert os.path.getmtime(store.db_path()) == before and store.load("notes.json", {}) == {"hello": "avant"}
    print("ok  restauration : zip vérifié puis installé, ancienne base gardée à côté ; un fichier étranger ne touche à rien")


def test_latest_and_cli():
    dl = tempfile.mkdtemp(prefix="kelly-dl-")
    os.makedirs(os.path.join(dl, "Telegram"))
    a, b = os.path.join(dl, "kelly_backup_2026-10-13.zip"), os.path.join(dl, "Telegram", "kelly_backup_2026-10-15 (1).zip")
    for p, t in ((a, time.time() - 3600), (b, time.time())):
        shutil.copyfile(os.path.join(OUT, "kelly_backup_2026-10-15.zip"), p)
        os.utime(p, (t, t))
    with open(os.path.join(dl, "kelly_notes.zip"), "w") as f:
        f.write("x")
    assert backup.latest((dl, "/chemin/absent")) == b
    assert backup.latest(("/chemin/absent",)) is None
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, DATA_DIR=tempfile.mkdtemp(prefix="kelly-restore-cli-"))
    r = subprocess.run([sys.executable, "bot.py", "--restore", b], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "✅ Sauvegarde restaurée" in r.stdout, (r.stdout, r.stderr)
    r = subprocess.run([sys.executable, "bot.py", "--restore", os.path.join(dl, "kelly_notes.zip")], cwd=root, env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 1 and "Ta base actuelle n'a pas été touchée" in r.stderr, (r.stdout, r.stderr)
    print("ok  la plus récente des téléchargements trouvée toute seule (sous-dossier Telegram, nom avec espace) ; bot.py --restore")


def main():
    test_send_auto()
    test_never_sends_broken()
    test_schedule()
    test_restore()
    test_latest_and_cli()
    print("tests sauvegarde automatique et restauration : OK")


if __name__ == "__main__":
    main()
