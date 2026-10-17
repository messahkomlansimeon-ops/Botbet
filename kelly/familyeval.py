"""Évaluation du lundi des familles de marché (voir families.py et families_ledger.json) : mesures, décisions, affichage, commande « familles ».

Mesures (pré-déclarées) :
  calibration  data/fair.json, lignes win / loss de la famille ; résidu y - prob ; intervalle à 95 % par grappe de match (lanestats.stats) ; « dans la marge » = l'intervalle contient zéro ;
               mesurable = au moins FAM_MIN_CALIB lignes ET un intervalle (deux matchs au moins). Non mesurable n'est JAMAIS hors marge.
  CLV fantôme  journal d'ensemble + archive (lanestats.journal_rows), lignes réglées de la famille à CLV mesuré (lanestats.x_clv) ; réussi = au moins FAM_MIN_GHOST, moyenne >= 0 et borne basse > 0.
Décisions (le lundi seulement, une seule fois par lundi) : hors_ticket -> fantome (calibration dans la marge), fantome -> proposee (CLV fantôme réussi, famille jamais rétrogradée),
fantome -> hors_ticket (hors marge deux lundis consécutifs à 7 jours exacts), jouee -> fantome (couleur committée rouge de la carte des edges, mécanisme allumé). Jamais de passage automatique à jouee."""
import sys
from datetime import date, datetime, timedelta, timezone

from . import calib, config as C, families as F, lanestats as LS, store

FAIR_FILE = "fair.json"


def _today():
    return datetime.now(timezone.utc).date()


# ---------- mesures ----------

def readable(name):
    """Le document `name` est-il une liste lisible dans ce processus ? (absent ou abîmé : non lisible, jamais « vide »)."""
    try:
        d = store.load(name, None)
    except Exception:                               # noqa: BLE001
        return False
    return isinstance(d, list) and name not in getattr(store, "_unreadable", ())


def calib_rows(fam, rows=None):
    rows = store.load(FAIR_FILE, []) if rows is None else rows
    return [r for r in rows if isinstance(r, dict) and r.get("result") in ("win", "loss") and isinstance(r.get("prob"), (int, float))
            and calib.family(r.get("market") or "") == fam]


def calib_stat(fam, rows=None):
    """{"n", "mean", "h", "measurable", "in_margin", "real", "ann"} de la famille (voir le module)."""
    rs = calib_rows(fam, rows)
    xs = [(1.0 if r["result"] == "win" else 0.0) - float(r["prob"]) for r in rs]
    n, m, h = LS.stats(xs, [r.get("id") or i for i, r in enumerate(rs)])
    measurable = n >= C.FAM_MIN_CALIB and m is not None and h is not None
    return {"n": len(rs), "mean": m, "h": h, "measurable": measurable, "in_margin": bool(measurable and m - h <= 0 <= m + h),
            "real": (sum(1 for r in rs if r["result"] == "win") / len(rs)) if rs else None, "ann": (sum(float(r["prob"]) for r in rs) / len(rs)) if rs else None}


def ghost_rows(fam, journal=None, cup_rows=None):
    """Lignes réglées de la famille (clés uniques) : le journal d'ensemble ; pour stats_cup, le journal des coupes."""
    if fam == "stats_cup":
        if cup_rows is None:
            from . import cupstats
            cup_rows = cupstats.journal_rows()
        src = [r for r in cup_rows if isinstance(r, dict) and r.get("strategy") == "stats"]
    else:
        src = [r for r in (LS.journal_rows() if journal is None else journal) if isinstance(r, dict) and not r.get("cup") and calib.family(r.get("market") or "") == fam]
    seen, out = set(), []
    for r in src:
        k = r.get("key")
        if LS.settled(r) and k and k not in seen:
            seen.add(k)
            out.append(r)
    return out


def ghost_stat(fam, journal=None, cup_rows=None):
    """{"n" (réglées), "nc" (CLV mesuré), "matches", "clv", "h", "passed"}."""
    rs = ghost_rows(fam, journal, cup_rows)
    xs = [LS.x_clv(r) for r in rs]
    nc, m, h = LS.stats(xs, [r.get("id") or i for i, r in enumerate(rs)])
    return {"n": len(rs), "nc": nc, "matches": len({r.get("id") for r, x in zip(rs, xs) if x is not None}), "clv": m, "h": h,
            "passed": bool(nc >= C.FAM_MIN_GHOST and m is not None and h is not None and m >= 0 and m - h > 0)}


# ---------- décisions ----------

def decide(day, snap, calibs, ghosts, red, watch):
    """Décisions du lundi `day` (date) -> (changements [(fam, nouvel état, raison, demoted)], nouvelle chaîne de surveillance). Pure.
    snap : families.snapshot() ; calibs / ghosts : {fam: calib_stat / ghost_stat} ; red : familles dont la carte est rouge (committée, mécanisme allumé) ; watch : chaîne enregistrée."""
    iso, week_ago = str(day), str(day - timedelta(days=7))
    changes, new_watch = [], {}
    for fam, rec in snap.items():
        st = rec["state"]
        cal, gh = calibs.get(fam), ghosts.get(fam)
        if st == "hors_ticket":
            if rec.get("voie") and cal and cal["measurable"] and cal["in_margin"]:
                changes.append((fam, "fantome", f"calibration dans la marge ({cal['n']} résultats)", False))
        elif st == "fantome":
            out = bool(cal and cal["measurable"] and not cal["in_margin"]) and fam not in F.PSEUDO and not rec.get("demoted")
            w = watch.get(fam) if isinstance(watch.get(fam), dict) else None
            if out:
                if w and w.get("on") == iso:
                    new_watch[fam] = w                                         # même lundi relancé : la chaîne n'avance pas
                elif w and w.get("on") == week_ago:
                    changes.append((fam, "hors_ticket", f"calibration hors marge deux lundis de suite ({cal['n']} résultats)", False))
                    continue
                else:
                    new_watch[fam] = {"cand": "hors_marge", "on": iso}
            if not rec.get("demoted") and gh and gh["passed"]:
                changes.append((fam, "proposee", f"CLV fantôme {gh['clv']:+.3f} sur {gh['nc']} lignes", False))
                new_watch.pop(fam, None)
        elif st == "jouee":
            if fam in red:
                changes.append((fam, "fantome", "carte des edges rouge (couleur committée)", True))
    return changes, new_watch


def red_families():
    """Familles de la voie stats jouees dont l'agrégat de la carte est rouge et lisible (couleur committée, deux lundis), mécanisme allumé ; vide sinon."""
    from . import edgemap
    try:
        if not edgemap.gate_active():
            return set()
        st = edgemap.state()
        return {f for f in F.lane_families("stats") if edgemap.committed(st, "stats", "*", f, "*") == "red"}
    except Exception as e:                          # noqa: BLE001
        print(f"[familles] carte illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return set()


def apply(today=None):
    """Évalue et enregistre l'état, LE LUNDI SEULEMENT, une fois par lundi, si FAMILIES_AUTO. -> la liste des changements faits (peut être vide) ou None si rien n'a été évalué. Ne lève jamais.
    Journaux illisibles (fair.json, journal d'ensemble) : aucune écriture d'état, `skipped` noté, ligne du bilan."""
    try:
        if not C.FAMILIES_AUTO:
            return None
        day = date.fromisoformat(str(today or _today())[:10])
        if day.weekday() != 0:
            return None
        d = F.doc()
        if d.get("week") == str(day):
            return None
        bad = [n for n in (FAIR_FILE, "ensemble_log.json") if not readable(n)]
        if bad:
            print(f"[familles] non évaluées : journal illisible ({', '.join(bad)}) : état conservé", file=sys.stderr)
            F.skip(day, bad)
            return None
        snap = F.snapshot()
        fair_rows = store.load(FAIR_FILE, [])
        journal = LS.journal_rows()
        calibs = {f: calib_stat(f, fair_rows) for f, r in snap.items() if r["state"] in ("hors_ticket", "fantome") and f not in F.PSEUDO}
        ghosts = {f: ghost_stat(f, journal) for f, r in snap.items() if r["state"] == "fantome" and f not in F.PSEUDO and not r.get("demoted")}
        if snap.get("stats_cup", {}).get("state") == "fantome":
            ghosts["stats_cup"] = ghost_stat("stats_cup")
        watch = d.get("_watch") if isinstance(d.get("_watch"), dict) else {}
        changes, new_watch = decide(day, snap, calibs, ghosts, red_families(), watch)
        return F.commit(changes, new_watch, day, day)
    except Exception as e:                          # noqa: BLE001 - l'évaluation ne bloque jamais le message du matin
        print(f"[familles] évaluation ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return None
