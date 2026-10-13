"""Sauvegarde de la base sur Telegram et restauration (téléphone perdu ou en panne).

- send(auto=False) : copie COHÉRENTE de data/kelly.db (API de sauvegarde de SQLite : d'autres processus peuvent écrire pendant ce temps), vérifiée
  (PRAGMA integrity_check), zippée, envoyée comme document Telegram. Automatique (`bot.py --backup-auto`, termux_runner.py : tous les
  BACKUP_EVERY_DAYS jours dès BACKUP_HOUR h UTC, retentée toutes les heures en cas d'échec) : SANS sonnerie, avec une légende (date, taille, paris
  notés, capital) et la marche à suivre pour restaurer. Manuelle : « backup ».
- restore(path) : un zip reçu de Kelly (ou un kelly.db) -> vérifié (intégrité, table des documents), l'ancienne base mise de côté
  (kelly.db.avant-restauration-<date>), la nouvelle installée ; `kelly restaurer <fichier>` arrête Kelly avant et la relance après.

Jamais dans la sauvegarde : le fichier .env (token Telegram, clés, proxy) : à garder ailleurs (gestionnaire de mots de passe)."""
import contextlib
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone

from . import store, telegram

ZIP_NAME = "kelly_backup.zip"          # « backup » à la main ; l'automatique est daté (kelly_backup_2026-10-15.zip) : la plus récente se retrouve d'un coup d'œil
SEARCH = ("~/storage/downloads", "~/storage/shared/Download", "~/storage/shared/Telegram", "/storage/emulated/0/Download",
          "/storage/emulated/0/Telegram", "~/Downloads", "~/Téléchargements")     # où `kelly restaurer` sans fichier cherche la dernière sauvegarde
SEARCH_DEPTH = 3


class BackupError(Exception):
    pass


def _check(db):
    """Vérifie une base SQLite de Kelly -> nombre de documents ; BackupError si elle est abîmée ou n'est pas une base de Kelly."""
    try:
        with closing(sqlite3.connect(db)) as con:
            ok = con.execute("PRAGMA integrity_check").fetchone()
            if not ok or ok[0] != "ok":
                raise BackupError(f"base abîmée ({ok[0] if ok else 'contrôle impossible'})")
            n = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    except sqlite3.Error as e:
        raise BackupError(f"ce n'est pas une base de Kelly ({type(e).__name__}: {e})") from None
    if not n:
        raise BackupError("base vide")
    return n


def _summary():
    """« 412 paris notés · capital 58 860 FCFA » (ce que la base contient, pour la légende) ; '' si illisible."""
    try:
        from . import money
        bets, rows, fair_rows = money._docs()
        n = sum(1 for x in money.items(bets, rows, fair_rows) if x.get("played") and not x.get("skipped"))
        st = money.capital_state()
        return f"{n} paris notés" + (f" · capital {money.money(round(st['capital']))}" if st else "")
    except Exception:                               # noqa: BLE001 - la légende est facultative
        return ""


def send(auto=False, now=None):
    """Envoie la sauvegarde sur Telegram -> None si c'est parti, sinon le message d'erreur. `auto` : sans sonnerie, légende complète."""
    db_file = store.db_path()
    if not os.path.exists(db_file):
        return "⚠️ Base de données introuvable."
    now = now or datetime.now(timezone.utc)
    tmp = tempfile.mkdtemp(prefix="kelly-backup-")
    try:
        fd, snap = tempfile.mkstemp(prefix="kelly-", suffix=".db", dir=tmp)
        os.close(fd)
        with closing(sqlite3.connect(db_file, timeout=60)) as src, closing(sqlite3.connect(snap)) as dst:
            src.backup(dst)
        _check(snap)
        orig_mb = os.path.getsize(snap) / (1024 * 1024)
        zip_path = os.path.join(tmp, f"kelly_backup_{now:%Y-%m-%d}.zip" if auto else ZIP_NAME)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap, arcname="kelly.db")
        zip_mb = os.path.getsize(zip_path) / (1024 * 1024)
        size = f"{zip_mb:.1f} Mo".replace(".", ",")
        if auto:
            what = _summary()
            caption = (f"💾 Sauvegarde automatique · {now:%d/%m/%Y %H:%M} UTC · {size}" + (f" · {what}" if what else "")
                       + "\nNouveau téléphone : installe Kelly, remets ton .env, télécharge ce fichier puis tape « kelly restaurer » dans Termux.")
        else:
            caption = f"💾 Sauvegarde Kelly ({size}, compressée depuis {orig_mb:.1f} Mo".replace(".", ",") + f") — {now:%d/%m/%Y %H:%M} UTC"
        telegram.send_document(zip_path, caption=caption, **({"silent": True} if auto else {}))
    except telegram.TelegramError as e:
        return f"⚠️ Échec de la sauvegarde : {e}"
    except BackupError as e:
        return f"⚠️ Sauvegarde non envoyée : {e}"
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as e:
        return f"⚠️ Échec de la sauvegarde : {type(e).__name__}: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return None


def latest(dirs=SEARCH):
    """Chemin de la sauvegarde la plus récente (kelly_backup*.zip) dans les dossiers de téléchargement du téléphone ; None s'il n'y en a pas."""
    found = []
    for d in dict.fromkeys(os.path.realpath(os.path.expanduser(x)) for x in dirs):
        if not os.path.isdir(d):
            continue
        top = d.count(os.sep)
        for root, subs, files in os.walk(d):
            if root.count(os.sep) - top >= SEARCH_DEPTH:
                subs[:] = []
            found += [os.path.join(root, f) for f in files if f.startswith("kelly_backup") and f.endswith(".zip")]
    return max(found, key=os.path.getmtime, default=None)


def restore(path, now=None):
    """Installe la sauvegarde `path` (zip envoyé par Kelly, ou un kelly.db) comme data/kelly.db -> message de réussite ; BackupError sinon. L'ancienne
    base (s'il y en a une) est gardée à côté : kelly.db.avant-restauration-<date>. Kelly doit être arrêtée (kelly restaurer s'en charge)."""
    if not path or not os.path.isfile(path):
        raise BackupError(f"fichier introuvable : {path}")
    now = now or datetime.now(timezone.utc)
    tmp = tempfile.mkdtemp(prefix="kelly-restore-")
    try:
        src = path
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if os.path.basename(n) == "kelly.db"]
                if not names:
                    raise BackupError("le zip ne contient pas kelly.db")
                src = os.path.join(tmp, "kelly.db")
                with zf.open(names[0]) as f, open(src, "wb") as out:
                    shutil.copyfileobj(f, out)
        n = _check(src)
        db = store.db_path()
        os.makedirs(os.path.dirname(db), exist_ok=True)
        new = db + ".restauration"
        shutil.copyfile(src, new)                   # d'abord la copie complète à côté (disque plein : rien n'est touché)
        _check(new)
        kept = f"{db}.avant-restauration-{now:%Y%m%d-%H%M%S}" if os.path.exists(db) else ""
        moved = []
        try:
            for ext in ("", "-journal", "-wal", "-shm"):   # l'ancienne base ET son journal, ensemble (jamais un journal effacé : il complète la base)
                if os.path.exists(db + ext):
                    os.replace(db + ext, (kept or db + ".ancienne") + ext)
                    moved.append(ext)
            os.replace(new, db)
        except OSError:
            for ext in moved:                       # retour à l'ancienne base
                with contextlib.suppress(OSError):
                    os.replace((kept or db + ".ancienne") + ext, db + ext)
            raise
        return f"✅ Sauvegarde restaurée ({n} documents)." + (f" Ancienne base gardée : {os.path.basename(kept)}" if kept else "")
    except (OSError, zipfile.BadZipFile) as e:
        with contextlib.suppress(OSError):
            os.remove(store.db_path() + ".restauration")
        raise BackupError(f"{type(e).__name__}: {e}") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
