"""Distribution quotidienne des écarts 1xBet / sharp, par heure de relevé (décision 5).

Pour TOUTES les sélections qui portent un avis sharp (un book de référence : Pinnacle, ou au moins C.SHARPGAP_MIN_SHARP books sharp), à chaque lecture du prix 1xBet
(matin, relevés, passages horaires) : écart = cote 1xBet x proba sharp - 1, rangé par tranche, avec l'âge du prix sharp (relevé The Odds API) et l'avance sur le coup d'envoi.
Document SÉPARÉ (data/gapdist.json) : ni lignes fantômes dans tracked.json, ni effet sur un pari, une alerte, le bilan ou l'apprentissage. Il sert à dire, avec des chiffres, ce que
le seuil +3 % laisse passer et surtout À QUELLE HEURE un écart existe avec un prix sharp frais : un sharp relevé à 04:00 a plus de 90 minutes dès la fin de matinée.

Fantômes : les écarts de +1,5 % à +3 % (sous le seuil) dont le sharp est frais et le match pas imminent sont suivis dans le même document (un par pari, première lecture), avec
leur DERNIÈRE lecture avant le match (proba sharp, prix 1xBet, avance) : de quoi mesurer leur CLV contre la clôture sharp et contre le dernier prix 1xBet connu (tools/audit_gaps.py).
Le seuil +3 % des paris réels n'est PAS touché. Ne lève jamais."""
import sys
import zlib
from datetime import datetime, timedelta, timezone

from . import config as C, ensemble, store

DOC = "gapdist.json"
EDGES = (-0.10, -0.05, 0.0, 0.015, 0.03, 0.05, 0.10, 0.25)                 # tranches d'écart ; la dernière ouverte (> 25 % : suspect, comme sharpgap)
BIN_NAMES = ("<-10", "-10..-5", "-5..0", "0..1.5", "1.5..3", "3..5", "5..10", "10..25", ">25")
AGE_NAMES = ("≤30", "31-90", "91-240", ">240", "inconnu")
GHOST_LO = 0.015
GHOST_KEEP_DAYS = 14
GHOST_MAX_NEW_DAY = 300
KEEP_DAYS = 60


def bin_of(edge):
    for i, e in enumerate(EDGES):
        if edge < e:
            return BIN_NAMES[i]
    return BIN_NAMES[-1]


def age_bin(age):
    if age is None:
        return AGE_NAMES[4]
    return AGE_NAMES[0] if age <= 30 else AGE_NAMES[1] if age <= 90 else AGE_NAMES[2] if age <= 240 else AGE_NAMES[3]


def sharp_line(c, now):
    """(proba sharp, source, nombre de books, âge en minutes ou None) de la sélection `c` (pari réel : c["prob_book"] ; pronostic : sa ligne dans c["psh"], même périmée), ou None."""
    s = ensemble.sharp_of(c)
    if s:
        return s[0], s[1], s[2], ensemble._sharp_age_min(c, now)
    hit = (c.get("psh") or {}).get(f"{ensemble._canon(c['market'], c['name'])}|{ensemble._pt(c.get('point'))}")
    if not hit:
        return None
    age = None
    if len(hit) > 3 and hit[3]:
        try:
            age = (now - datetime.fromisoformat(str(hit[3]).replace("Z", "+00:00"))).total_seconds() / 60
        except (ValueError, TypeError):
            age = None
    return hit[0], hit[1], hit[2], None if age is None else max(0.0, age)


def _lead(c, now):
    try:
        ko = datetime.fromisoformat(str(c["commence"]).replace("Z", "+00:00"))
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
        return (ko - now).total_seconds() / 60
    except (KeyError, ValueError, TypeError):
        return None


def _bump(d, k, n=1):
    d[k] = d.get(k, 0) + n


def _one(c, now, cell, seen, ghost, day, hour, new_today, today=None):
    """Une sélection -> 1 si comptée dans la distribution, 0 sinon (`cell`, `seen` et `ghost` sont modifiés en place). Le suivi du fantôme (dernière lecture) se fait à CHAQUE
    lecture, même quand la distribution a déjà compté la sélection dans l'heure."""
    line = sharp_line(c, now)
    price = c.get("xodds")
    if not line or not price or price <= 1 or not c.get("key"):
        return 0
    lead = _lead(c, now)
    if lead is None or lead < 0:
        return 0
    p, src, nb, age = line
    fresh = age is not None and age <= C.SHARPGAP_MAX_AGE_MIN
    kid, kfresh = f"{zlib.crc32(c['key'].encode()):08x}", f"{zlib.crc32((c['key'] + '|F').encode()):08x}"       # une lecture par sélection et par heure (la 1re) ; les colonnes « sharp frais » ont leur propre déduplication
    if not (src == "pinnacle" or (nb or 0) >= C.SHARPGAP_MIN_SHARP):
        if kid not in seen:
            seen.add(kid)
            cell["weak"] += 1                           # un seul book sharp sans Pinnacle : pas un avis (comme sharpgap), compté à part
        return 0
    edge = price * p - 1
    reading = {"at": now.isoformat(timespec="minutes"), "p": round(p, 4), "x": price, "age": None if age is None else round(age), "lead": round(lead)}
    g = ghost.get(c["key"])
    if g is not None:                                   # dernière lecture avant le match (tout prix 1xBet) ; dernière lecture à sharp FRAIS à part (la clôture sharp mesurable)
        g["last"] = reading
        if fresh:
            g["last_fresh"] = reading
    elif fresh and GHOST_LO <= edge < C.SHARPGAP_EDGE and lead >= C.SHARPGAP_SOON_MIN and new_today < GHOST_MAX_NEW_DAY:
        ghost[c["key"]] = {"day": day, "at": reading["at"], "hour": hour, "id": c.get("id"), "sport_key": c.get("sport_key"), "market": c["market"],
                           "name": c.get("name"), "point": c.get("point"), "commence": c.get("commence"), "xodds": price, "p": round(p, 4), "edge": round(edge, 4),
                           "age": reading["age"], "lead": reading["lead"], "xref": c.get("xref")}
    counted = 0
    if kid not in seen:                                 # distribution de TOUTES les lectures : la 1re de l'heure
        seen.add(kid)
        cell["keys"].append(kid)
        cell["n"] += 1
        counted = 1
        _bump(cell["bins"], bin_of(edge))
        _bump(cell["age"], age_bin(age))
        if today is not None:                           # âge du prix 1xBet utilisé, pour les lectures qui portent `today` (paris réels du passage horaire seulement)
            from . import sharpgap
            _bump(cell.setdefault("xage", {}), age_bin(sharpgap.x_age(today, c, now)))
    if fresh and kfresh not in seen:                    # sharp frais : la lecture du passage horaire n'est pas masquée par celle du matin
        seen.add(kfresh)
        cell["keys"].append(kfresh)
        _bump(cell["fresh"], bin_of(edge))
        if lead <= 90:
            _bump(cell["soon"], bin_of(edge))           # sharp frais ET match dans moins de 90 min : la seule situation où l'écart est jouable avec un prix sharp utile
    return counted


def _apply(doc, rows, now, today=None):
    """Range `rows` dans le document `doc` (modifié en place) -> (doc, nombre de sélections comptées)."""
    doc = doc if isinstance(doc, dict) else {}
    days = doc.setdefault("days", {})
    ghost = doc.setdefault("ghost", {})
    day, hour = str(now.date()), f"{now.hour:02d}"
    for d, hours in days.items():                       # les clés de lecture ne servent qu'au jour courant : on ne garde que les comptes
        if d != day:
            for cell in hours.values():
                cell.pop("keys", None)
    cell = days.setdefault(day, {}).setdefault(hour, {"n": 0, "bins": {}, "age": {}, "fresh": {}, "soon": {}, "weak": 0, "keys": []})
    seen = set(cell.setdefault("keys", []))
    new_today = sum(1 for g in ghost.values() if g.get("day") == day)
    n = 0
    for c in rows:
        try:
            before = len(ghost)
            n += _one(c, now, cell, seen, ghost, day, hour, new_today, today)
            new_today += len(ghost) - before
        except Exception as e:                          # noqa: BLE001 - une sélection illisible ne fait pas perdre les autres
            print(f"[gapdist] sélection ignorée : {type(e).__name__}: {e}", file=sys.stderr)
    cell["keys"] = sorted(seen)
    cut = str(now.date() - timedelta(days=KEEP_DAYS))
    for d in [d for d in days if d < cut]:
        days.pop(d)
    gcut = str(now.date() - timedelta(days=GHOST_KEEP_DAYS))
    for k in [k for k, g in ghost.items() if (g.get("day") or "") < gcut]:
        ghost.pop(k)
    return doc, n


def observe(rows, now=None, today=None):
    """Range les sélections de `rows` (pronostics et paris réels déjà remis au prix 1xBet) dans la distribution du jour et suit les fantômes +1,5..+3 %. Lecture-modification-écriture
    ATOMIQUE (store.update : deux processus ne s'écrasent pas). -> nombre de sélections comptées ; ne lève jamais."""
    try:
        now = now or datetime.now(timezone.utc)
        return store.update(DOC, lambda doc: _apply(doc, list(rows), now, today), {})
    except Exception as e:                              # noqa: BLE001 - une mesure ne bloque jamais le passage
        print(f"[gapdist] distribution ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def summary_line(today=None):
    """« écarts 1xBet / sharp : 143 lectures aujourd'hui, 12 fantômes +1,5..+3 % suivis » (status) ; '' si le document n'existe pas encore."""
    try:
        doc = store.load(DOC, {})
        if not isinstance(doc, dict) or not doc:
            return ""
        day = str(today or datetime.now(timezone.utc).date())
        n = sum(c.get("n", 0) for c in (doc.get("days") or {}).get(day, {}).values())
        ghost = sum(1 for g in (doc.get("ghost") or {}).values() if isinstance(g, dict))
        return f"écarts 1xBet / sharp : {n} lectures aujourd'hui, {ghost} fantômes +1,5..+3 % suivis (tools/audit_gaps.py)"
    except Exception:                           # noqa: BLE001
        return ""
