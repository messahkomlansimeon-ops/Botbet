"""Relevé réseau de CLÔTURE des marchés de stats (décision du superviseur du 17/10 : la couverture des relevés ≤ 90 min était de 53 % pour 70 % visés, bornée par le cache).

Chaque passage `--watch` (toutes les 10 min, son propre verrou) cherche les matchs qui portent une ligne de stats non réglée d'un des trois journaux (ensemble, saison, coupes ; réelle ou fantôme, famille
de voie seulement : `lanes.lane_of_market`) et qui commencent dans 35 à 60 min. Pour chacun : UNE lecture 1xBet FRAÎCHE de chaque sous-match (`xref[0]` des lignes, une lecture même si plusieurs lignes le
partagent), faite au premier passage où l'avance est dans [35 ; 50[ min (1xBet ferme ces sous-matchs ≈ 30 min avant ; le passage dure 10 min + son temps de travail : une tranche plus étroite ferait manquer des matchs). Ce module n'écrit JAMAIS dans les journaux (le suivi des matchs ne tient pas le verrou des données) :
la réponse est dans le cache disque, `ensemble.mark_x_close` (passage horaire, sous verrou) la lit comme les autres et marque `src = "close_read"` d'après le registre `xclose_reads.json` d'ici.

Plafond XCLOSE_READS_DAY MATCHS par jour Kelly (les octets et requêtes sont comptés à part, pour le coût) ; priorité (jour entier, pas seulement le passage) : heure de coup d'envoi dans une tranche à couverture
< 60 % (PRIORITY_HOURS), puis le plus de lignes. Au-delà de XCLOSE_MONTH_PCT % du plafond mensuel 1xBet, le relevé se tait et le dit. Interrupteur : XCLOSE_READ."""
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config as C, cupstats, ensemble, lanes, seasonlane, store

FILE = "xclose_reads.json"            # {"days": {jour Kelly: {"done": {id du match: {gid: instant du relevé ou None}}, "bytes", "requests"}}, "silenced": {...}}
WINDOW = (35, 60)                     # minutes avant le coup d'envoi : borne basse (1xBet ferme ~30 min avant), borne haute de l'éligibilité
READ_BEFORE = 50                      # on lit tant que l'avance est dans [35, 50[ : le passage dure 10 min + la durée du tick (jusqu'à plusieurs minutes), un passage sur deux manquerait sinon un match ; premier passage de cette tranche = le plus proche de 50, pas de 35
assert WINDOW[0] < READ_BEFORE <= WINDOW[1]
PRIORITY_HOURS = (0, 1, 2, 3, 16, 19, 22, 23)       # heures UTC de coup d'envoi à couverture < 60 % (section G du 17/10)
KEEP_DAYS = 10
SRC = "close_read"
SECONDS = 120                         # durée maximale des lectures d'un passage


def kelly_day(t):
    """Jour Kelly d'un instant UTC : jusqu'à C.DAY_END_HOUR h UTC le lendemain, c'est encore la veille."""
    return str(t.date() if t.hour >= C.DAY_END_HOUR else t.date() - timedelta(days=1))


def _ko(r):
    try:
        ko = datetime.fromisoformat(str(r["commence"]).replace("Z", "+00:00"))
        return ko if ko.tzinfo else ko.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError):
        return None


def _gid(r):
    ref = r.get("xref")
    return str(ref[0]) if isinstance(ref, (list, tuple)) and ref and ref[0] is not None else None


def eligible_line(r):
    """Ligne non réglée d'une famille de STATS (corners, cartons, tirs, fautes...; pas les buts : leur match principal reste ouvert), avec identifiant de match, référence 1xBet et coup d'envoi."""
    return (isinstance(r, dict) and r.get("result") is None and _gid(r) is not None and _ko(r) is not None
            and lanes.lane_of_market(r.get("market")) == "stats" and r.get("id") is not None)


def matches(now):
    """{id du match: {"ko", "lines", "gids"}} des lignes éligibles des trois journaux dont le coup d'envoi est à venir, dans le jour Kelly de `now`."""
    out, day = {}, kelly_day(now)
    for name in (ensemble.LOG_FILE, cupstats.LOG, seasonlane.LOG):
        log = store.load(name, [])
        for r in log if isinstance(log, list) else []:
            if not eligible_line(r):
                continue
            ko = _ko(r)
            if ko <= now or (kelly_day(ko) != day and (ko - now) >= timedelta(minutes=WINDOW[1])):        # le jour Kelly suivant ne compte pas, sauf un match qui commence dans l'heure (04:00-05:00 UTC vu avant 04:00)
                continue
            m = out.setdefault(str(r.get("id")), {"ko": ko, "lines": 0, "gids": set()})
            m["lines"] += 1
            m["gids"].add(_gid(r))
    return out


def rank_key(item):
    """Ordre du jour : tranche horaire prioritaire d'abord, puis le plus de lignes, puis le coup d'envoi le plus proche."""
    mid, m = item
    return (m["ko"].hour not in PRIORITY_HOURS, -m["lines"], m["ko"], mid)


def _doc():
    d = store.load(FILE, {})
    return d if isinstance(d, dict) else {}


def month_ok():
    """Le plafond mensuel de données 1xBet laisse de la place au relevé (C.XCLOSE_MONTH_PCT %)."""
    try:
        from .sources import xbet
        return xbet.usage()["bytes"] < C.XCLOSE_MONTH_PCT / 100 * C.XBET_MONTH_MB * 1e6
    except Exception:                          # noqa: BLE001 - compteur illisible : pas de blocage
        return True


def plan(now, doc=None):
    """Matchs à lire à ce passage : [(id, ko, [gid...])]. Seuls les matchs du jour dont un sous-match n'a pas de réponse enregistrée, à WINDOW[0] ≤ avance < READ_BEFORE min, et dont le rang du jour
    (tranche prioritaire, lignes) est dans ce qu'il reste du plafond ; un sous-match en panne est relu au passage suivant, un sous-match déjà répondu (même « fermé ») jamais."""
    doc = _doc() if doc is None else doc
    day = kelly_day(now)
    done = ((doc.get("days") or {}).get(day) or {}).get("done") or {}
    allm = matches(now)
    complete = {mid for mid, m in allm.items() if set(m["gids"]) <= set((done.get(mid) or {}))}
    room = max(0, C.XCLOSE_READS_DAY - len(complete) - sum(1 for mid in done if mid not in allm))         # lus (ou commencés) : un match déjà parti compte toujours
    todo = sorted(((mid, m) for mid, m in allm.items() if mid not in complete), key=rank_key)
    out = []
    for rank, (mid, m) in enumerate(todo):
        lead = (m["ko"] - now).total_seconds() / 60
        if rank < room and WINDOW[0] <= lead < READ_BEFORE:
            out.append((mid, m["ko"], sorted(g for g in m["gids"] if g not in (done.get(mid) or {}))))
    return out


def _record(now, results, spent):
    """Enregistre atomiquement les lectures du passage : {id du match: {gid: instant}}, octets et requêtes du jour (registre court, 10 jours)."""
    day = kelly_day(now)

    def add(doc):
        doc = doc if isinstance(doc, dict) else {}
        days = doc.setdefault("days", {})
        d = days.setdefault(day, {"done": {}, "bytes": 0, "requests": 0})
        for mid, reads in results.items():
            d["done"].setdefault(mid, {}).update(reads)
        d["bytes"], d["requests"] = int(d["bytes"] + spent[0]), int(d["requests"] + spent[1])
        doc["days"] = dict(sorted(days.items())[-KEEP_DAYS:])
        if results:
            doc.pop("silenced", None)                  # le relevé a repris
        return doc, doc
    return store.update(FILE, add, {})


def _start(now, gids):
    """Instant de départ de la lecture de chaque sous-match (registre `starts`, 2 jours) : `src_of` reconnaît ainsi un relevé tombé dans le cache avant l'enregistrement des résultats."""
    t = time.time()

    def put(doc):
        doc = doc if isinstance(doc, dict) else {}
        st = {g: v for g, v in (doc.get("starts") or {}).items() if isinstance(v, (int, float)) and t - v < 2 * 86400}
        st.update({g: t for g in gids})
        doc["starts"] = st
        return doc, doc
    store.update(FILE, put, {})


def _silence(now):
    def put(doc):
        doc = doc if isinstance(doc, dict) else {}
        doc["silenced"] = {"day": kelly_day(now), "pct": C.XCLOSE_MONTH_PCT}
        return doc, doc
    store.update(FILE, put, {})


def run(now=None, read=None, seconds=None):
    """Passage du relevé de clôture. `read(gids, deadline=)` : injectable (sinon xbet.read_closing). Ne lève jamais. -> {"planned", "read", "ok", "bytes", "requests", "silenced"}."""
    rep = {"planned": 0, "read": 0, "ok": 0, "bytes": 0, "requests": 0, "silenced": False}
    try:
        if not (C.XCLOSE_READ and C.XBET_ENABLED):
            return rep
        now = now or datetime.now(timezone.utc)                          # le passage du suivi ne donne PAS son heure de début (des minutes ont pu passer) : l'horloge est lue ici
        todo = plan(now)
        rep["planned"] = len(todo)
        if not todo:
            return rep
        if read is None and not month_ok():
            rep["silenced"] = True
            _silence(now)
            print(f"[clôture] plafond mensuel de données 1xBet atteint à {C.XCLOSE_MONTH_PCT:g} % : pas de relevé de clôture ({len(todo)} match(s) non lus)", file=sys.stderr)
            return rep
        if read is None:
            from .sources import xbet
            read = xbet.read_closing
        deadline = time.monotonic() + (seconds or SECONDS)
        gids = sorted({g for _, _, gs in todo for g in gs})                  # un sous-match partagé par deux journaux n'est lu qu'une fois
        _start(now, gids)                                                    # l'instant de départ est noté AVANT la lecture : le relevé qui atterrit dans le cache est reconnu même si le passage horaire passe entre-temps
        got, spent, why = read(gids, deadline=deadline)
        results = {mid: {g: got[g] for g in gs if g in got} for mid, _, gs in todo}      # une panne (gid absent de `got`) n'est PAS enregistrée : relu au passage suivant
        results = {mid: r for mid, r in results.items() if r}
        rep["read"] = len(results)
        rep["ok"] = sum(1 for r in results.values() for t in r.values() if t)
        rep["bytes"], rep["requests"] = int(spent[0]), int(spent[1])
        if results or spent[0]:
            _record(now, results, spent)
        print(f"[clôture] {len(todo)} match(s) à lire, {rep['read']} répondu(s), {rep['ok']} sous-match(s) relevé(s), ≈ {spent[0] / 1e3:.0f} Ko ({spent[1]} requête(s))"
              + (f" ; arrêt : {why}" if why else ""), file=sys.stderr)
    except Exception as e:                          # noqa: BLE001 - facultatif : ne casse jamais le suivi des matchs
        print(f"[clôture] relevé ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    return rep


# ---------- marque de la source (lue par ensemble.mark_x_close) et bilan ----------

def index():
    """{gid (texte): [instants de relevé]} des relevés de clôture enregistrés (les 10 derniers jours) ; la clé « start:<gid> » porte l'instant de départ de la dernière lecture."""
    out = {}
    try:
        for g, t in (_doc().get("starts") or {}).items():
            if isinstance(t, (int, float)):
                out[f"start:{g}"] = [float(t)]
        for d in (_doc().get("days") or {}).values():
            for reads in (d.get("done") or {}).values():
                for g, ts in (reads or {}).items():
                    if isinstance(ts, (int, float)):
                        out.setdefault(str(g), []).append(float(ts))
    except Exception as e:                          # noqa: BLE001
        print(f"[clôture] registre illisible : {type(e).__name__}: {e}", file=sys.stderr)
    return out


def src_of(xref, at, idx):
    """« close_read » si le relevé en cache de la ligne (`at`) est un de ceux que ce module a faits pour son sous-match, sinon « cache »."""
    try:
        g, at = str(xref[0]), float(at)
        if any(abs(t - at) < 1.0 for t in idx.get(g, ())):
            return SRC
        st = (idx.get(f"start:{g}") or [None])[0]
        return SRC if st is not None and st - 1.0 <= at <= st + SECONDS + 30 else "cache"        # tombé dans le cache pendant la lecture, avant que le registre ne la retienne
    except (TypeError, IndexError, ValueError):
        return "cache"


def report_lines(today=None, days=3):
    """Coût et activité des `days` derniers jours Kelly + extinction mensuelle éventuelle (texte brut, pour le bilan et le status) ; [] sans rien à dire."""
    doc = _doc()
    day = today or kelly_day(datetime.now(timezone.utc))
    ds = sorted(((doc.get("days") or {}).items()))[-days:]
    out = []
    if ds:
        n = sum(len(d.get("done") or {}) for _, d in ds)
        ok = sum(1 for _, d in ds for reads in (d.get("done") or {}).values() for t in reads.values() if t)
        kb = sum(d.get("bytes", 0) for _, d in ds) / 1e3
        rq = sum(d.get("requests", 0) for _, d in ds)
        out.append(f"relevé de clôture ({len(ds)} derniers jours) : {n} match(s) lus, {ok} sous-match(s) relevé(s), ≈ {kb:.0f} Ko, {rq} requête(s) ; plafond {C.XCLOSE_READS_DAY} matchs/jour")
    s = doc.get("silenced")
    if isinstance(s, dict) and s.get("day") == day:
        out.append(f"⚠️ relevé de clôture muet : plafond mensuel de données 1xBet atteint à {s.get('pct'):g} %")
    return out
