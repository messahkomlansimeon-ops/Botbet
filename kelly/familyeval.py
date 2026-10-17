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


# ---------- affichage et commande ----------

LABELS = calib.LABELS | {"stats_cup": "stats de coupe"}
ALIASES = {"touche": "throws", "touches": "throws", "horsjeu": "offsides", "hors-jeu": "offsides", "joueurs": "player", "joueur": "player", "coupe": "stats_cup", "coupes": "stats_cup",
           "statsdecoupe": "stats_cup", "tirs": "shots", "tir": "shots", "cadres": "sot", "fautes": "fouls", "faute": "fouls", "cartons": "cards", "carton": "cards", "jaunes": "yellow",
           "1n2": "h2h", "victoire": "h2h", "buts": "goals", "equipe": "team_goals"}
ICON = {"hors_ticket": "⚫", "fantome": "👻", "proposee": "🟢", "jouee": "✅"}
STATE_TEXT = {"hors_ticket": "hors ticket", "fantome": "fantôme", "proposee": "proposée", "jouee": "jouée"}


def _pc(x, signed=False):
    return LS._pc(x, signed)


def label(fam):
    return LABELS.get(fam, fam)


def resolve(word):
    """Famille nommée par `word` (code, libellé, alias ; sans accents ni casse) ou None."""
    from . import played
    w = played.norm(str(word or "")).strip(" .?!")
    if not w:
        return None
    for f in list(F.snapshot()) + list(LABELS):
        if w == played.norm(f) or w == played.norm(label(f)).replace(" ", ""):
            return f
    for k, f in ALIASES.items():
        if w == played.norm(k).replace(" ", ""):
            return f
    return None


def since_text(rec):
    s = rec.get("since")
    return f"depuis le {s[8:10]}/{s[5:7]}" if isinstance(s, str) and len(s) >= 10 else "depuis l'origine"


def _row(fam, rec, fair_rows, journal):
    """Une ligne du tableau « familles » : état · depuis · résultats · calibration · lignes fantômes · CLV."""
    st = rec["state"]
    parts = [f"{ICON[st]} {label(fam)} : {STATE_TEXT[st]}" + (" (rétrogradée)" if rec.get("demoted") and st != "jouee" else "") + f" {since_text(rec)}"]
    if fam in F.PSEUDO:
        parts.append("calibration : sans objet (contexte, pas un marché)")
    else:
        c = calib_stat(fam, fair_rows)
        if c["n"]:
            real = f"réalisé {_pc(c['real'])} pour {_pc(c['ann'])} annoncé"
            verdict = "non mesurable" if not c["measurable"] else ("dans la marge" if c["in_margin"] else "HORS marge")
            parts.append(f"{c['n']} résultat{'s' if c['n'] > 1 else ''} · {real} ± {_pc(c['h'])} ({verdict})" if c["h"] is not None else f"{c['n']} résultat{'s' if c['n'] > 1 else ''} · {real} (non mesurable)")
        else:
            parts.append("aucun résultat")
    if st != "jouee" or fam in F.PSEUDO:
        g = ghost_stat(fam, journal)
        parts.append(f"{g['n']} ligne{'s' if g['n'] > 1 else ''} fantôme{'s' if g['n'] > 1 else ''} réglée{'s' if g['n'] > 1 else ''}" + (f" · CLV {_pc(g['clv'], True)} ± {_pc(g['h'])} sur {g['nc']}" if g["nc"] else " · pas de CLV mesuré"))
    if st == "jouee" and rec.get("voie") == "stats":
        from . import lanes
        if lanes.mode("stats") != "on":
            parts.append("coupée par la voie")
    return " · ".join(parts)


def report(args=()):
    """Commande `familles` : tableau état / résultats / calibration / lignes fantômes / CLV / depuis quand. Ne lève jamais."""
    try:
        snap = F.snapshot()
        fair_rows = store.load(FAIR_FILE, []) if readable(FAIR_FILE) else []
        journal = LS.journal_rows()
        d = F.doc()
        out = ["🧬 Familles de marché (admission aux tickets : hors ticket → fantôme → proposée → jouée)",
               f"évaluation le lundi seulement ({'automatique' if C.FAMILIES_AUTO else 'AUTOMATIQUE COUPÉE (FAMILIES_AUTO=0)'}) · dernier lundi évalué : {d.get('week') or 'aucun'}"
               f" · seuils : {C.FAM_MIN_CALIB} résultats (calibration), {C.FAM_MIN_GHOST} lignes fantômes (CLV)"]
        order = sorted(snap, key=lambda f: (["jouee", "proposee", "fantome", "hors_ticket"].index(snap[f]["state"]), f))
        out += [_row(f, snap[f], fair_rows, journal) for f in order]
        out.append("« familles X on » : jouer une famille proposée (ou rétrogradée par la carte) ; jamais automatique.")
        return "\n".join(out)
    except Exception as e:                          # noqa: BLE001
        print(f"[familles] affichage illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return "🧬 Les familles sont illisibles pour l'instant (voir kelly logs)."


def status_lines():
    """Une ligne par famille NON jouée (état et depuis quand) ; [] si toutes sont jouées."""
    snap = F.snapshot()
    return [f"{ICON[r['state']]} {label(f)} : {STATE_TEXT[r['state']]} {since_text(r)}" for f, r in sorted(snap.items()) if r["state"] != "jouee"]


def weekly_lines(today=None):
    """Bloc du bilan du lundi : changements d'état de la semaine, familles proposées (« familles X on »), lundi non évalué. [] sans rien à dire."""
    try:
        day = date.fromisoformat(str(today or _today())[:10])
        d = F.doc()
        out = []
        sk = d.get("skipped") if isinstance(d.get("skipped"), dict) else None
        if sk and str(sk.get("week", "")) >= str(d.get("week") or ""):
            out.append(f"familles non évaluées le lundi {sk.get('week')} : journal {', '.join(sk.get('docs', []))} illisible")
        since = str(day - timedelta(days=6))
        for x in F.history(60):
            if str(x.get("on", "")) >= since:
                out.append(f"{label(x['fam'])} : {STATE_TEXT.get(x['from'], x['from'])} → {STATE_TEXT.get(x['to'], x['to'])} ({x.get('how')})")
        out += [f"🟢 famille {label(f)} proposée pour les tickets : familles {f} on" for f, r in sorted(F.snapshot().items()) if r["state"] == "proposee"]
        return out
    except Exception as e:                          # noqa: BLE001
        print(f"[familles] bilan illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return []


def command(args=(), today=None):
    """« familles » (tableau) ; « familles X on » : joue une famille PROPOSÉE, ou rétrogradée de jouee (retour à la main) ; refuse le reste avec la raison. Tracé avec date dans families.json."""
    words = [str(a) for a in (args or ())]
    if not words:
        return report()
    if len(words) < 2 or words[-1].lower() != "on":
        return "Usage : « familles » (tableau) ou « familles <famille> on » (jouer une famille proposée)."
    fam = resolve(" ".join(words[:-1]))
    if fam is None:
        return "❓ Famille inconnue : " + " ".join(words[:-1]) + ". Familles : " + ", ".join(sorted(label(f) for f in F.snapshot())) + "."
    rec = F.snapshot().get(fam) or {}
    st, name = rec.get("state"), label(fam)
    day = date.fromisoformat(str(today or _today())[:10])
    if st == "jouee":
        return f"ℹ️ {name} est déjà jouée."
    if rec.get("voie") is None:
        return f"🚫 {name} : aucune voie définie, jamais admissible aux tickets."
    if st == "proposee" or (st == "fantome" and rec.get("demoted")):
        r = F.set_state(fam, "jouee", "main", day, by="superviseur", demoted=False)
        extra = " (STATS_CUPS_MODE=off prime encore.)" if fam == "stats_cup" and C.STATS_CUPS_MODE == "off" else ""
        return f"✅ {name} est jouée depuis le {day:%d/%m/%Y} ({STATE_TEXT[r[0]]} → jouée, à la main).{extra}" if r else f"ℹ️ {name} est déjà jouée."
    if st == "fantome":
        return (f"🚫 {name} est en fantôme : pas encore proposée (il faut {C.FAM_MIN_GHOST} lignes fantômes réglées à CLV 1xBet ≥ 0, intervalle sans zéro, évalué le lundi).")
    return f"🚫 {name} est hors ticket : elle doit d'abord passer fantôme (≥ {C.FAM_MIN_CALIB} résultats, calibration dans la marge, évalué le lundi)."
