"""Mémoire du bot : base SQLite data/kelly.db (DATA_DIR pour les tests). Chaque document garde son ancien nom de fichier
(« bets.json », « archive/tracked-2026.json »...) et son contenu JSON : load / save ne changent pas pour le reste du code.
Les anciens fichiers data/*.json sont importés une seule fois, à la création de la base, puis rangés dans data/sauvegarde/ ; une base
recréée (kelly.db perdu) sans ancien fichier se remplit depuis data/sauvegarde/ (_restore_backup) au lieu de repartir à vide.
Sauvegarde lisible (et commitée par git) : export() écrit chaque document en JSON dans data/sauvegarde/ ; import_dir() restaure."""
import contextlib
import fcntl
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing, contextmanager

DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_FILE = "kelly.db"
BACKUP_DIR = "sauvegarde"
NO_EXPORT = ("strength_", "progress.json", "telegram_later.json")      # caches recalculés, progression du passage en cours, messages en attente : pas dans la sauvegarde
NOT_DOCS = ("stats_cache.json",)  # fichier lu directement par stats.py (tests, --sample) : jamais importé


def path(name):
    return os.path.join(DATA_DIR, name)


def db_path():
    return path(DB_FILE)


def backup_dir():
    return path(BACKUP_DIR)


@contextmanager
def _db():
    """Connexion courte (une par opération : plusieurs processus se partagent la base, le verrou interne de SQLite les sérialise).
    Création de la base : table + import des anciens fichiers JSON. Erreur SQLite -> OSError (comme l'ancien stockage fichier)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with closing(sqlite3.connect(db_path(), timeout=60, isolation_level=None)) as con:
            con.execute("CREATE TABLE IF NOT EXISTS docs (name TEXT PRIMARY KEY, body TEXT NOT NULL, updated TEXT NOT NULL,"
                        " version INTEGER NOT NULL DEFAULT 1)")
            con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            if not con.execute("SELECT 1 FROM meta WHERE key='migrated'").fetchone():
                _migrate(con)
            yield con
    except sqlite3.Error as e:
        raise OSError(f"base de données {db_path()} : {e}") from None


def _legacy_files():
    """Anciens fichiers de données : data/*.json et data/archive/*.json -> [(nom, chemin)]."""
    out = []
    for sub in ("", "archive"):
        d = path(sub) if sub else DATA_DIR
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        out += [(f"{sub}/{f}" if sub else f, os.path.join(d, f)) for f in names if f.endswith(".json") and f not in NOT_DOCS]
    return out


def _backup_files(src=None):
    """Documents JSON d'un dossier de sauvegarde (data/sauvegarde/ par défaut) : [(nom, chemin)] triés."""
    src = src or backup_dir()
    out = []
    for root, _, files in os.walk(src):
        out += [(os.path.relpath(os.path.join(root, f), src).replace(os.sep, "/"), os.path.join(root, f))
                for f in files if f.endswith(".json")]
    return sorted(out)


def _restore_backup(con):
    """Base neuve, aucun ancien fichier à importer, mais data/sauvegarde/ a des documents : kelly.db a disparu (le dossier est son miroir).
    Les importe tout de suite (dans la transaction de _migrate) : sans cela le bot tournerait à vide et le prochain export() viderait la
    sauvegarde. -> nombre de documents restaurés."""
    n = 0
    for name, p in _backup_files():
        try:
            with open(p, encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, ValueError) as e:
            print(f"[store] {p} non restauré ({type(e).__name__}) : laissé en place", file=sys.stderr)
            continue
        con.execute("INSERT OR IGNORE INTO docs (name, body, updated) VALUES (?, ?, ?)", (name, _dump(obj), _now()))
        n += 1
    if n:
        print(f"[store] ATTENTION : {db_path()} était absent ; {n} document(s) restauré(s) depuis {backup_dir()}", file=sys.stderr)
    return n


def _migrate(con):
    """Une seule fois par base (drapeau meta.migrated, posé dans la même transaction) : importe les anciens fichiers JSON, puis les
    range dans data/sauvegarde/ (même forme que l'export). Un fichier illisible reste où il est. Sans ancien fichier, restaure
    data/sauvegarde/ (_restore_backup)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT 1 FROM meta WHERE key='migrated'").fetchone():
            con.execute("COMMIT")
            return
        done = []
        for name, p in _legacy_files():
            try:
                with open(p, encoding="utf-8") as f:
                    obj = json.load(f)
            except (OSError, ValueError) as e:
                print(f"[store] {p} non importé ({type(e).__name__}) : laissé en place", file=sys.stderr)
                continue
            con.execute("INSERT OR IGNORE INTO docs (name, body, updated) VALUES (?, ?, ?)", (name, _dump(obj), _now()))
            done.append((name, p))
        if not done:
            _restore_backup(con)
        con.execute("INSERT INTO meta (key, value) VALUES ('migrated', ?)", (_now(),))
        con.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):    # la transaction est peut-être déjà finie : ne masque jamais l'erreur d'origine
            con.execute("ROLLBACK")
        raise
    for name, p in done:
        dest = os.path.join(backup_dir(), name)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(p, dest)
        except OSError as e:
            print(f"[store] {p} importé mais non rangé : {e}", file=sys.stderr)
    if done:
        print(f"[store] {len(done)} fichier(s) de données importé(s) dans {db_path()} (originaux rangés dans {backup_dir()})",
              file=sys.stderr)
    try:
        os.rmdir(path("archive"))                   # vide une fois rangé
    except OSError:
        pass


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


_unreadable = set()     # documents dont la dernière lecture a raté sur ERREUR DE BASE dans ce processus : save() refuse de les écraser (lire un default puis le réécrire effacerait de bonnes données)


def load(name, default):
    """Document `name`, ou `default` s'il n'existe pas (ou si la base est illisible : le bot continue). Une erreur de base (verrou
    dépassé, disque) est mémorisée (réussie -> oubliée) : le prochain save() du document est refusé, pour ne pas remplacer de bonnes
    données par un default. Un JSON abîmé, lui, est réparable : le save() suivant remplace le document."""
    try:
        with _db() as con:
            row = con.execute("SELECT body FROM docs WHERE name=?", (name,)).fetchone()
        _unreadable.discard(name)
        return json.loads(row[0]) if row else default
    except OSError as e:
        _unreadable.add(name)
        print(f"[store] lecture de {name} impossible : {e}", file=sys.stderr)
        return default
    except ValueError as e:
        print(f"[store] lecture de {name} impossible : {e}", file=sys.stderr)
        return default


def load_doc(name, default):
    """load(name, default) ; un document EXISTANT d'une autre forme (dict attendu, liste ou champ d'un autre type trouvés) compte
    aussi comme absent : `default`. Un document malformé (écriture partielle, édition à la main) ne plante plus le passage qui
    l'attend ; comme un JSON corrompu, le save() suivant le répare."""
    d = load(name, default)
    if type(d) is not type(default):
        return default
    if isinstance(default, dict) and any(k not in d or type(d[k]) is not type(default[k]) for k in default):
        return default
    return d


def save(name, obj, compact=False):
    """Enregistre le document `name` (remplace l'ancien). Une transaction SQLite : un lecteur (le bot Telegram pendant un passage
    programmé) ne voit jamais un document à moitié écrit, même si le PC s'éteint. `compact` : gardé pour compatibilité (tout est
    stocké compact). Lève OSError si la base refuse l'écriture. Un document dont la lecture a raté sur erreur de base dans ce
    processus n'est PAS écrasé : l'écriture est ignorée (et signalée), le contenu existant reste."""
    if name in _unreadable:
        print(f"[store] {name} non enregistré : la base n'a pas pu être relue dans ce processus (le contenu existant est gardé)",
              file=sys.stderr)
        return
    body = _dump(obj)
    with _db() as con:
        con.execute("INSERT INTO docs (name, body, updated) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET body=excluded.body,"
                    " updated=excluded.updated, version=docs.version+1", (name, body, _now()))


def exists(name):
    try:
        with _db() as con:
            return con.execute("SELECT 1 FROM docs WHERE name=?", (name,)).fetchone() is not None
    except OSError:
        return False


def remove(name):
    """Efface le document (sans erreur s'il n'existe pas)."""
    with _db() as con:
        con.execute("DELETE FROM docs WHERE name=?", (name,))


def size(name):
    """Taille du document en octets (JSON compact, UTF-8) ; 0 s'il n'existe pas."""
    try:
        with _db() as con:
            row = con.execute("SELECT length(CAST(body AS BLOB)) FROM docs WHERE name=?", (name,)).fetchone()
        return row[0] if row else 0
    except OSError:
        return 0


def names(prefix=""):
    """Noms des documents qui commencent par `prefix`, triés."""
    try:
        with _db() as con:
            return [r[0] for r in con.execute("SELECT name FROM docs WHERE substr(name, 1, ?)=? ORDER BY name", (len(prefix), prefix))]
    except OSError:
        return []


def version(name):
    """Numéro de version du document (augmente à chaque save) ; None s'il n'existe pas. Sert à relire un réglage modifié."""
    try:
        with _db() as con:
            row = con.execute("SELECT version, updated FROM docs WHERE name=?", (name,)).fetchone()
        return tuple(row) if row else None
    except OSError:
        return None


ESSENTIAL = ("bets.json", "tracked.json", "fair.json")      # documents dont l'absence de la base, alors que le dossier les a, signale une base vidée
TMP_RE = re.compile(r"\.json\.(\d+)\.tmp$")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True                                 # existe mais n'est pas à nous
    return True


def export(dest=None):
    """Écrit chaque document (sauf les caches NO_EXPORT) en JSON lisible dans `dest` (data/sauvegarde/ par défaut) et efface les
    fichiers .json qui n'y correspondent plus : le dossier est le miroir de la base, que git sauvegarde. Jamais d'effacement (écriture
    seule, avertissement) si la base paraît vidée : moins de la moitié des documents du dossier, ou bets.json / tracked.json / fair.json
    absents de la base alors que le dossier les a. Les « *.json.<pid>.tmp » laissés par un export interrompu sont supprimés.
    -> nombre de documents."""
    dest = dest or backup_dir()
    with _db() as con:
        rows = con.execute("SELECT name, body FROM docs ORDER BY name").fetchall()
    before = {n for n, _ in _backup_files(dest)}
    keep = set()
    for name, body in rows:
        if name.startswith(NO_EXPORT):
            continue
        p = os.path.join(dest, name)
        keep.add(os.path.normpath(p))
        text = json.dumps(json.loads(body), ensure_ascii=False, indent=0)
        try:
            with open(p, encoding="utf-8") as f:
                if f.read() == text:
                    continue                        # inchangé : git ne voit rien
        except OSError:
            pass
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    names_ = {os.path.relpath(k, os.path.normpath(dest)).replace(os.sep, "/") for k in keep}
    lost = sorted(before - names_)
    suspect = bool(lost) and (2 * len(names_) < len(before) or any(n in lost for n in ESSENTIAL))
    if suspect:
        print(f"[store] ATTENTION : la base ({len(names_)} document(s)) paraît vidée par rapport à {dest} ({len(before)} fichier(s) ; "
              f"absents de la base : {', '.join(lost[:5])}{'...' if len(lost) > 5 else ''}) : rien n'est effacé dans la sauvegarde",
              file=sys.stderr)
    for root, _, files in os.walk(dest):
        for f in files:
            p = os.path.normpath(os.path.join(root, f))
            if f.endswith(".json") and p not in keep and not suspect:
                os.remove(p)
            else:
                m = TMP_RE.search(f)
                if m and (int(m.group(1)) == os.getpid() or not _pid_alive(int(m.group(1)))):      # reste d'un export interrompu
                    os.remove(p)
    return len(keep)


def import_dir(src=None):
    """Restaure les documents depuis un dossier de sauvegarde (data/sauvegarde/ par défaut) : chaque .json remplace le document du
    même nom ; les autres documents de la base restent. Un fichier illisible est sauté (la restauration continue) : signalé. ->
    nombre de documents importés."""
    _unreadable.clear()                             # une restauration remplace délibérément, même ce qui n'a pas pu être lu
    src = src or backup_dir()
    n = 0
    for root, _, files in os.walk(src):
        for f in sorted(files):
            if not f.endswith(".json") or f.startswith(NO_EXPORT):
                continue
            p = os.path.join(root, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    obj = json.load(fh)
            except (OSError, ValueError) as e:
                print(f"[store] {p} non restauré ({type(e).__name__}) : la restauration continue", file=sys.stderr)
                continue
            save(os.path.relpath(p, src).replace(os.sep, "/"), obj)
            n += 1
    return n


class LockTimeout(Exception):
    """Le verrou des données est resté pris plus longtemps que prévu."""


def lock_path():
    """Fichier de verrou : le MÊME que celui des unités systemd (`flock $XDG_RUNTIME_DIR/kelly.lock python bot.py --daily`) ;
    sans XDG_RUNTIME_DIR (tests, lancement manuel ailleurs) : data/.kelly.lock."""
    run = os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(run, "kelly.lock") if run else os.path.join(DATA_DIR, ".kelly.lock")


@contextmanager
def data_lock(wait=None, on_busy=None):
    """Verrou exclusif sur les données (bets.json, tracked.json...) : le bot Telegram écrit pendant que le matin ou le relevé horaire
    tournent, et l'un écraserait l'autre (mise à jour perdue : lire, modifier, réécrire tout le document). `wait` : secondes
    d'attente au plus (None = sans limite, 0 = ne pas attendre) ; dépassé -> LockTimeout. `on_busy()` est appelé une fois si le
    verrou est déjà pris, avant d'attendre. Le verrou se libère en sortant (et si le processus meurt). Ne l'utilise pas autour d'un
    code déjà lancé sous `flock` : inter-blocage. Verrou impossible (dossier, fichier, erreur flock) : travail sans verrou plutôt que bloquer."""
    try:
        os.makedirs(os.path.dirname(lock_path()), exist_ok=True)
        f = open(lock_path(), "a")
    except OSError as e:                            # verrou impossible : mieux vaut travailler sans que ne plus répondre
        print(f"[store] verrou indisponible ({e}) : travail sans verrou", file=sys.stderr)
        yield
        return
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if wait == 0:
                raise LockTimeout(lock_path()) from None
            if on_busy:
                on_busy()
            if wait is None:
                fcntl.flock(f, fcntl.LOCK_EX)
            else:
                end = time.monotonic() + wait
                while True:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= end:
                            raise LockTimeout(lock_path()) from None
                        time.sleep(0.25)
        except OSError as e:                        # flock refusé autrement : travail sans verrou, comme si le fichier était inaccessible
            print(f"[store] verrou indisponible ({e}) : travail sans verrou", file=sys.stderr)
            yield
            return
        yield
    finally:
        f.close()                                   # libère le verrou
