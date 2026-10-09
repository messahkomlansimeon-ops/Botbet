"""Migrations de données, une seule fois chacune (marqueur dans le document data/migrations.json : {"notes_fcfa": true}).

notes_fcfa : avant le mode « je note ce que je joue en FCFA », un pari noté (x["played"] = {odds, stake, at}) sans devise avait sa mise tapée
en FCFA (« /joue sur 5,40 2000 »). Chaque ligne notée sans devise (bets.json, tracked.json, fair_combos.json) reçoit la devise du budget
(money.cur(), « FCFA ») ; la note (cote, mise, heure) ne change pas. Sa « stake » (mise du matin, en unités, jamais affichée) est effacée
(None) : avec une devise, « stake » est toujours une mise conseillée en FCFA, que Kelly recalcule sur le budget du jour. Une ligne migrée n'est
ni comptée deux fois ni perdue : la bankroll en unités ignore les lignes avec devise (bankroll.apply_results) et le bilan en argent prend
toutes les notes réglées (money.gain). Aucune liste ne rétrécit (les documents sont réécrits tels quels, ligne par ligne : tracked.json
sans l'archivage de track.save), et une seconde exécution ne change rien.
"""
import sys

from . import fair, money, store

FILE = "migrations.json"


def _fix(rows, cur):
    """Donne la devise `cur` aux lignes notées sans devise de la liste `rows` (en place) -> nombre de lignes migrées."""
    n = 0
    for x in rows if isinstance(rows, list) else []:
        if isinstance(x, dict) and x.get("played") and not x.get("cur"):
            x["cur"], x["stake"] = cur, None
            n += 1
    return n


def notes_fcfa():
    """Migration « notes en FCFA » (voir le module). -> nombre de lignes migrées (0 si déjà faite)."""
    done = store.load(FILE, {})
    done = done if isinstance(done, dict) else {}
    if done.get("notes_fcfa"):
        return 0
    cur, n = money.cur(), 0
    bets = store.load("bets.json", None)
    if isinstance(bets, dict):
        k = _fix(bets.get("combos"), cur)
        if k:
            store.save("bets.json", bets)
        n += k
    rows = store.load("tracked.json", None)
    k = _fix(rows, cur)
    if k:
        store.save("tracked.json", rows)
    n += k
    combos = fair.load_combos()
    k = _fix(combos, cur)
    if k:
        store.save(fair.COMBOS_FILE, combos)
    n += k
    store.save(FILE, done | {"notes_fcfa": True})
    if n:
        print(f"[migration] {n} pari(s) noté(s) sans devise passé(s) en {cur} (mise inchangée)", file=sys.stderr)
    return n


def ensure():
    """Lance les migrations pas encore faites (au démarrage du bot et de chaque passage). Ne lève jamais : une migration en échec sera
    retentée au prochain démarrage (son marqueur n'est posé qu'à la fin). -> nombre de lignes migrées."""
    try:
        return notes_fcfa()
    except Exception as e:                          # noqa: BLE001 - la migration ne doit jamais empêcher le bot de répondre
        print(f"[migration] ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return 0
