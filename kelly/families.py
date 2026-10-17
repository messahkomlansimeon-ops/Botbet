"""Familles de marché : cycle de vie automatique (admission aux tickets). Pré-déclaration : families_ledger.json.

Trois états de base, plus un intermédiaire :
  hors_ticket  calculée, visible (/match), comptée en calibration ; jamais sur un ticket ni en fantôme ; AUCUNE lecture 1xBet dédiée ;
  fantome      journalisée avec prix 1xBet, xclose, mu/val ; jamais sur un ticket ; lecture 1xBet dans les plafonds existants ;
  proposee     se comporte comme fantome ; attend la commande manuelle « familles X on » ;
  jouee        comportement des familles de voie (sharpgap : buts... ; stats : corners, cartons, tirs...).

Ce module est une FEUILLE (il n'importe que config et store ; calib seulement dans family_of, à l'appel) : ensemble, lanes, xbet... l'importent, jamais l'inverse. L'état est relu au plus toutes
les 2 secondes (le bot Telegram est un processus long, l'état change le lundi) : aucune liste de familles n'est figée à l'import. Tant que le document data/families.json n'existe pas,
DEFAULTS répond exactement comme les anciennes listes codées en dur : la migration (migrate.families_init) ne change aucun comportement.
Prédicats : ticket(f) = jouee ; reads(f) = jouee, fantome ou proposee (sous-matchs 1xBet, fenêtre des stats, relevé de clôture) ; lane(f) = la voie de la famille si elle est jouee.
Les transitions automatiques du lundi sont dans familyeval.py."""
import sys
import time

from . import config as C, store

FILE = "families.json"
STATES = ("hors_ticket", "fantome", "proposee", "jouee")
READ_STATES = ("jouee", "fantome", "proposee")
RESERVED = frozenset(("_watch", "_log", "week", "skipped"))
LOG_MAX = 200                                       # historique des changements gardé (les plus récents)

# État initial (migration) : {famille: (état, voie)}. `voie` = la voie que la famille rejoint si elle est admise (pré-déclaré dans le ledger).
DEFAULTS = {
    "goals": ("jouee", "sharpgap"), "h2h": ("jouee", "sharpgap"), "dc": ("jouee", "sharpgap"), "btts": ("jouee", "sharpgap"), "team_goals": ("jouee", "sharpgap"),
    "corners": ("jouee", "stats"), "cards": ("jouee", "stats"), "yellow": ("jouee", "stats"), "shots": ("jouee", "stats"), "sot": ("jouee", "stats"), "fouls": ("jouee", "stats"),
    "stats_cup": ("fantome", "stats"),             # pseudo-famille : les stats de COUPE (contexte, pas un marché) ; voir lanes.cup_ok
    "throws": ("hors_ticket", "stats"), "offsides": ("hors_ticket", "stats"), "player": ("hors_ticket", None),
}
PSEUDO = frozenset(("stats_cup",))                  # pas de marché derrière : jamais une famille de calib.family
_memo = {"t": 0.0, "v": None}


def reset():
    _memo.update(t=0.0, v=None)


def doc():
    """Document d'état (relu au plus toutes les 2 s) ; {} sans document ou illisible. Ne lève jamais."""
    now = time.monotonic()
    if _memo["v"] is None or now - _memo["t"] > 2.0:
        try:
            d = store.load(FILE, {})
        except Exception as e:                      # noqa: BLE001 - un état illisible = les valeurs par défaut, jamais un plantage de la sélection
            print(f"[familles] état illisible : {type(e).__name__}: {e}", file=sys.stderr)
            d = {}
        _memo.update(t=now, v=d if isinstance(d, dict) else {})
    return _memo["v"]


def _rec(fam):
    r = doc().get(fam)
    return r if isinstance(r, dict) and fam not in RESERVED else {}


def state_of(fam):
    """État d'une famille : celui du document, sinon DEFAULTS, sinon hors_ticket (une famille inconnue n'est jamais sur un ticket)."""
    s = _rec(fam).get("state")
    if s in STATES:
        return s
    return DEFAULTS.get(fam, ("hors_ticket", None))[0]


def voie_of(fam):
    """Voie de la famille (sharpgap, stats ou None), qu'elle soit jouee ou non."""
    v = _rec(fam).get("voie") if _rec(fam).get("state") in STATES else None
    return v if v in ("sharpgap", "stats") else DEFAULTS.get(fam, (None, None))[1]


def ticket(fam):
    return state_of(fam) == "jouee"


def reads(fam):
    return state_of(fam) in READ_STATES


def lane(fam):
    """« sharpgap » ou « stats » pour une famille jouee, sinon None (remplace SHARPGAP_FAMILIES / STATS_LANE)."""
    return voie_of(fam) if ticket(fam) else None


def closeread(fam):
    """Le relevé de clôture (xclose) lit-il cette famille ? Voie stats, et lue (jouee, fantome, proposee)."""
    return reads(fam) and voie_of(fam) == "stats"


def family_of(market):
    from . import calib                             # import tardif : calib n'importe pas ce module
    return calib.family(market or "")


def of_market(market):
    return state_of(family_of(market))


def _prefix_family(p):
    p = str(p)
    if p.startswith("team_"):
        return p.split("_")[1]
    return "player" if p.startswith("player_") else None


def filter_prefix(prefixes, pred=None):
    """Les préfixes de marché (« team_fouls_ », « player_ »...) dont la famille satisfait `pred` (défaut : reads) ; ceux dont la famille n'est pas lisible (« team_ ») restent. Tuple."""
    pred = reads if pred is None else pred
    if isinstance(prefixes, str):
        prefixes = (prefixes,)
    out = []
    for p in prefixes or ():
        f = _prefix_family(p)
        if f is None or pred(f):
            out.append(p)
    return tuple(out)


def window_prefix():
    """Préfixes des marchés de la fenêtre des stats lus à cet instant (config.STATS_WINDOW_PREFIX = les candidats, filtrés par l'état)."""
    return filter_prefix(C.STATS_WINDOW_PREFIX)


def lane_families(lane_name):
    """Familles jouees de la voie `lane_name`, dans l'ordre de DEFAULTS puis du document."""
    names = list(DEFAULTS) + [k for k in doc() if k not in RESERVED and k not in DEFAULTS and isinstance(doc().get(k), dict)]
    return tuple(f for f in names if f not in PSEUDO and lane(f) == lane_name)


def cup_ok():
    """Les stats de coupe peuvent-elles être jouées ? STATS_CUPS_MODE on, ou (pas off et stats_cup jouee) : avec l'état initial fantome, exactement le comportement d'avant."""
    m = C.STATS_CUPS_MODE
    return m == "on" or (m != "off" and state_of("stats_cup") == "jouee")


def snapshot():
    """{famille: {state, voie, since, from, how, demoted}} de toutes les familles connues (document + défauts), pour l'affichage."""
    out = {}
    for f, (s, v) in DEFAULTS.items():
        out[f] = {"state": s, "voie": v, "since": None, "from": None, "how": "défaut", "demoted": False}
    for f, r in doc().items():
        if f in RESERVED or not isinstance(r, dict):
            continue
        base = out.get(f, {"state": "hors_ticket", "voie": None, "since": None, "from": None, "how": None, "demoted": False})
        out[f] = base | {k: r[k] for k in ("state", "voie", "since", "from", "how", "demoted") if k in r and (k != "state" or r[k] in STATES)}
    return out


def history(n=20):
    log = doc().get("_log")
    return [x for x in log if isinstance(x, dict)][-n:] if isinstance(log, list) else []


def _change(d, fam, new, how, day, by=None, demoted=None):
    """Applique le changement d'état de `fam` au document `d` (en place) ; -> (ancien, nouveau) ou None si l'état est déjà `new`."""
    rec = d.get(fam) if isinstance(d.get(fam), dict) and fam not in RESERVED else {}
    old = rec.get("state") if rec.get("state") in STATES else DEFAULTS.get(fam, ("hors_ticket", None))[0]
    if old == new:
        return None
    voie = rec.get("voie") if rec.get("voie") in ("sharpgap", "stats") else DEFAULTS.get(fam, (None, None))[1]
    dem = bool(rec.get("demoted")) if demoted is None else bool(demoted)
    d[fam] = {"state": new, "voie": voie, "since": str(day)[:10], "from": old, "how": how, "demoted": dem}
    log = [x for x in d.get("_log", []) if isinstance(x, dict)] if isinstance(d.get("_log"), list) else []
    d["_log"] = (log + [{"fam": fam, "from": old, "to": new, "on": str(day)[:10], "how": how, "by": by}])[-LOG_MAX:]
    return old, new


def set_state(fam, new, how, day, by=None, demoted=None):
    """Change l'état de `fam` (atomique), trace la date et la raison dans `_log`. -> (ancien état, nouvel état) ou None si rien n'a changé. `demoted` : True quand jouee -> fantome (retour à la main),
    False à la sortie ; None : inchangé."""
    if new not in STATES:
        raise ValueError(f"état inconnu : {new}")
    out = {}

    def fn(d):
        d = dict(d) if isinstance(d, dict) else {}
        out["r"] = _change(d, fam, new, how, day, by, demoted)
        return (d if out["r"] else None), None

    store.update(FILE, fn, {})
    reset()
    return out.get("r")


def commit(changes, watch, week, day):
    """Écrit d'un seul bloc (atomique) les changements du lundi `[(fam, nouvel état, raison, demoted)]`, la chaîne de surveillance `watch` et le lundi évalué `week` (et efface `skipped`). -> les changements faits."""
    done = []

    def fn(d):
        d = dict(d) if isinstance(d, dict) else {}
        for fam, new, how, dem in changes:
            r = _change(d, fam, new, how, day, "auto", dem)
            if r:
                done.append((fam, r[0], r[1], how))
        d["_watch"], d["week"] = watch, str(week)[:10]
        d.pop("skipped", None)
        return d, None

    store.update(FILE, fn, {})
    reset()
    return done


def skip(week, docs):
    """Note qu'aucune évaluation n'a eu lieu ce lundi (journaux illisibles) : l'état et la chaîne de surveillance restent tels quels."""
    def fn(d):
        d = dict(d) if isinstance(d, dict) else {}
        d["skipped"] = {"week": str(week)[:10], "docs": list(docs)}
        return d, None

    store.update(FILE, fn, {})
    reset()


def ensure(day=None):
    """Migration (une fois) : écrit l'état initial DEFAULTS si le document n'existe pas. Ne change aucun comportement (DEFAULTS répondait déjà). -> True si écrit."""
    if store.exists(FILE):
        return False
    d = {f: {"state": s, "voie": v, "since": str(day)[:10] if day else None, "from": None, "how": "migration", "demoted": False} for f, (s, v) in DEFAULTS.items()}
    d["_log"] = []
    store.save(FILE, d)
    reset()
    return True
