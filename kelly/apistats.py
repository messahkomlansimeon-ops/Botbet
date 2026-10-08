"""Stats mondiales via API-Football : profils d'équipe pour les championnats que football-data ne couvre pas
(Brésil, Argentine, MLS, Afrique...).

Chaque matin (avant l'achat des cotes), Kelly fait la liste des matchs du jour dont les équipes n'ont pas de vraies stats
(corners, cartons, tirs : football-data n'a que les buts hors grands championnats) et remplit leurs profils, dans cet ordre :
ligues du catalogue (région, ligues majeures), puis championnats africains. Une équipe coûte 1 requête (ses derniers
matchs) + 1 requête pour leurs statistiques (méthode groupée /fixtures?ids=, voir stats.fixture_stats_batch) ou, si elle ne
marche pas, 1 requête par match (6 au plus pour une équipe inconnue). Un match dont les statistiques sont connues n'est
jamais redemandé (stats.fixture_stats, cache du disque) ; un profil récent n'est pas recalculé (APISTATS_REFRESH_DAYS).

Budget : au plus C.APISTATS_DAILY requêtes par jour pour les profils, et jamais en dessous de C.FAIR_API_RESERVE requêtes
restantes sur le plafond du jour (stats.STATS_DAILY_CAP) : elles sont gardées pour le règlement des paris. Une erreur de
plan ou de saison (forfait gratuit) arrête tous les appels de la journée (stats.get) : le remplissage s'arrête sans rien
casser et le dit une fois, en français (journal du matin, --check).

Stockage compact : data/apistats_profiles.json, une ligne par équipe. Chaque vue (all / home / away, sur les C.APISTATS_LAST
derniers matchs) est un tableau [n, buts pour, contre, tirs, tirs cadrés, corners, fautes, cartons, jaunes ...] ; profiles()
le redonne au format de football-data (n, goals_for, goals_against, shots_for ...) que sources.features consomme.
Une équipe n'est « couverte » qu'avec au moins C.APISTATS_MIN_N matchs avec statistiques.
"""
import sys
import time
from datetime import datetime, timedelta, timezone

from . import cache, catalogue, config as C, stats, store

FILE = "apistats_profiles.json"
KEYS = ("goals", "shots", "sot", "corners", "fouls", "cards", "yellow")        # ordre des tableaux compacts
FIELD = {"shots": "shots", "sot": "sot", "corners": "corners", "fouls": "fouls", "cards": "cards", "yellow": "yel"}
LAST_LIST = 12                      # matchs demandés à /fixtures?team=..&last= (les 10 derniers terminés servent)
EVAL_MAX = 150                      # matchs du relevé examinés au plus pour la file


def active():
    """Vrai si le remplissage des profils est possible (clé API-Football et budget APISTATS_DAILY non nul)."""
    return stats.enabled() and C.APISTATS_DAILY > 0


def _today():
    return stats._today()


# ---------- stockage ----------

def _pack(v):
    return [v.get("n", 0)] + [v.get(f"{k}_{s}") for k in KEYS for s in ("for", "against")]


def _unpack(a):
    out = {"n": a[0]}
    i = 1
    for k in KEYS:
        for s in ("for", "against"):
            if a[i] is not None:
                out[f"{k}_{s}"] = a[i]
            i += 1
    return out


def load():
    d = store.load(FILE, {})
    d = d if isinstance(d, dict) else {}
    if not isinstance(d.get("teams"), dict):
        d["teams"] = {}
    return d


def save(d):
    """Écrit le fichier en oubliant les équipes non renouvelées depuis APISTATS_KEEP_DAYS jours, puis les plus anciennes
    au-delà de APISTATS_MAX_TEAMS (le fichier reste sous 300 Ko)."""
    cut = str((datetime.fromisoformat(_today()) - timedelta(days=C.APISTATS_KEEP_DAYS)).date())
    teams = {k: v for k, v in d["teams"].items() if v.get("d", "0") >= cut}
    if len(teams) > C.APISTATS_MAX_TEAMS:
        keep = sorted(teams, key=lambda k: teams[k].get("d", ""), reverse=True)[:C.APISTATS_MAX_TEAMS]
        teams = {k: teams[k] for k in keep}
    d["teams"], d["v"] = teams, 1
    store.save(FILE, d, compact=True)


def n_matches(t):
    return (t.get("all") or [0])[0] if not t.get("none") else 0


def covered_team(t):
    return bool(t) and n_matches(t) >= C.APISTATS_MIN_N


def profiles():
    """Profils au format de football-data : {"teams": {nom: {id, sk, lg, last, all/home/away: {n, goals_for...}}}, "updated"}.
    Une équipe par nom (et par alias : le nom donné par The Odds API) ; seules les équipes couvertes sont servies."""
    d = load()
    teams, updated = {}, ""
    for tid, t in d["teams"].items():
        if not covered_team(t):
            continue
        p = {"id": int(tid), "nm": t.get("nm"), "sk": t.get("sk", []), "lg": t.get("lg"), "last": t.get("l"), "d": t.get("d"),
             "all": _unpack(t["all"]), "home": _unpack(t["home"]), "away": _unpack(t["away"])}
        updated = max(updated, t.get("d", ""))
        for name in [t.get("nm")] + list(t.get("al") or []):
            if name and (name not in teams or n_matches(t) > teams[name]["all"]["n"]):
                teams[name] = p
    return {"teams": teams, "updated": updated}


def counts():
    """(équipes en mémoire, équipes couvertes, dernière mise à jour)."""
    d = load()
    cov = [t for t in d["teams"].values() if covered_team(t)]
    return len(d["teams"]), len(cov), max((t.get("d", "") for t in d["teams"].values()), default="")


# ---------- budget ----------

class Budget:
    """Requêtes API-Football encore permises pour les profils aujourd'hui : APISTATS_DAILY moins ce qui a déjà servi aux
    profils (clé « aps » de stats_quota.json), et jamais sous la réserve du règlement."""

    def __init__(self):
        q = stats._quota()
        self.n0, self.aps0 = q["n"], q.get("aps", 0)

    def spent(self):
        return self.aps0 + max(0, stats._quota()["n"] - self.n0)

    def left(self):
        if stats._halted:
            return 0
        return max(0, min(C.APISTATS_DAILY - self.spent(), stats.left_today() - C.FAIR_API_RESERVE))

    def commit(self):
        q = stats._quota()
        q["aps"] = self.spent()
        store.save("stats_quota.json", q)


# ---------- profil d'une équipe ----------

def _records(tid, fixtures, cache_):
    """[(date, côté, {stat: (pour, contre)})] des matchs de l'équipe dont les statistiques sont connues."""
    from .sources import footballdata as fd
    out = []
    for f in fixtures:
        st = cache_.get(str(f["id"]))
        if not st or st.get("missing") or not st.get("teams"):
            continue
        side = "home" if f["home_id"] == tid else "away"
        opp = f["away_id"] if side == "home" else f["home_id"]
        mine, other = st["teams"].get(str(tid)), st["teams"].get(str(opp))
        if not mine or not other:
            continue
        rec = {"goals": ((f["gh"], f["ga"]) if side == "home" else (f["ga"], f["gh"])) if f.get("gh") is not None else (None, None)}
        for k, fld in FIELD.items():
            ok = fld not in (mine.get("nul") or []) and fld not in (other.get("nul") or [])
            rec[k] = (mine.get(fld), other.get(fld)) if ok and fld in mine and fld in other else (None, None)
        if all(v == (None, None) for k, v in rec.items() if k != "goals"):
            continue                                  # match sans aucune statistique utilisable
        out.append((f["ts"] or 0, side, rec))
    out.sort(key=lambda r: r[0])
    return out


def build_profile(tid, fixtures, cache_):
    """Vues all / home / away (format compact) sur les C.APISTATS_LAST derniers matchs avec statistiques, ou None."""
    from .sources import footballdata as fd
    recs = _records(tid, fixtures, cache_)[-C.APISTATS_LAST:]
    if not recs:
        return None
    view = lambda rs: _pack(fd._avg_view([r[2] for r in rs], keys=KEYS))          # noqa: E731
    last = datetime.fromtimestamp(recs[-1][0], tz=timezone.utc).date().isoformat() if recs[-1][0] else None
    return {"all": view(recs), "home": view([r for r in recs if r[1] == "home"]),
            "away": view([r for r in recs if r[1] == "away"]), "l": last}


def refresh_team(d, tid, name, ctx, budget):
    """Met à jour le profil de l'équipe `tid` (nom donné par The Odds API ou API-Football) : 1 requête pour ses derniers
    matchs + les statistiques manquantes (groupées en 1 requête si possible, sinon un match par requête). `ctx` : {sk, lg}.
    -> True si l'équipe a été traitée, False si le budget ou l'API s'arrête avant."""
    if budget.left() < 2:
        return False
    fx = stats.get("/fixtures", team=tid, last=LAST_LIST)
    if fx is None:
        return False
    fixtures = sorted((stats._entry(f) for f in fx if (f.get("fixture", {}).get("status") or {}).get("short") in stats.DONE),
                      key=lambda e: e.get("ts") or 0, reverse=True)[:C.APISTATS_LAST]
    old = d["teams"].get(str(tid)) or {}
    api_name = next((e["home"] if e["home_id"] == tid else e["away"] for e in fixtures if tid in (e["home_id"], e["away_id"])), name)
    target = C.APISTATS_LAST if covered_team(old) else C.APISTATS_TARGET
    cache_ = stats._stats_cache()
    have = lambda: sum(1 for e in fixtures if (cache_.get(str(e["id"])) or {}).get("teams"))      # noqa: E731
    todo = [e for e in fixtures if not stats.stats_known(e["id"])]
    cache.hit("api-football", len(fixtures) - len(todo))        # statistiques déjà connues : zéro requête
    want = target - have()
    if want > 0 and todo and budget.left() >= 1:
        batch = stats.fixture_stats_batch([e["id"] for e in todo[:want + 2]])
        if batch is None:                             # méthode groupée indisponible : un match par requête
            tries = 0
            for e in todo:
                cache_ = stats._stats_cache()
                if budget.left() < 1 or tries >= want + 2 or have() >= target:
                    break
                stats.fixture_stats(e["id"], players=False)
                tries += 1
    cache_ = stats._stats_cache()
    prof = build_profile(tid, fixtures, cache_)
    n = prof["all"][0] if prof else 0
    pending = [e for e in fixtures if not stats.stats_known(e["id"])]
    absent = sum(1 for e in fixtures if (cache_.get(str(e["id"])) or {}).get("missing"))
    t = {"nm": api_name, "sk": list(dict.fromkeys(old.get("sk", []) + [ctx["sk"]]))[-4:], "lg": ctx.get("lg") or old.get("lg"),
         "d": _today(), "al": list(dict.fromkeys(([name] if name != api_name else []) + (old.get("al") or [])))[:2]}
    empty = [0] + [None] * 14
    t.update(prof or {"all": empty, "home": empty, "away": empty})
    if n < C.APISTATS_MIN_N:
        if not pending or absent >= 4:
            t["none"] = 1                              # rien (ou trop peu) à tirer de l'API pour cette équipe : pas avant quelques jours
        else:
            t["p"] = 1                                 # incomplet (budget épuisé en route) : repris au prochain passage
    d["teams"][str(tid)] = t
    return True


def _fresh(d, tid):
    """L'équipe n'a pas besoin d'être revue : profil récent, ou échec récent (rien à tirer de l'API pour elle)."""
    t = d["teams"].get(str(tid))
    if not t or t.get("p"):
        return False
    age = (datetime.fromisoformat(_today()) - datetime.fromisoformat(t.get("d", "2000-01-01"))).days
    if t.get("none"):
        return age < C.STATS_NO_DATA_DAYS
    return covered_team(t) and age < C.APISTATS_REFRESH_DAYS


# ---------- la file du matin ----------

def rich(f):
    """Vrai si les chiffres d'avant-match `f` ont déjà corners ET cartons d'une autre source que ce module."""
    own = set(f.get("aps_keys") or ())
    return all(k in f and k not in own for k in ("exp_corners", "exp_cards"))


def queue(day, scan, fixtures, now=None):
    """Matchs du jour sans vraies stats, dans l'ordre : ligues du catalogue (région, ligues majeures, nombre de matchs), puis
    Afrique. -> (file, non retrouvés) ; chaque élément : {home, away, hid, aid, sk, lg, ts}. Les sélections nationales sont
    ignorées (leurs noms ressemblent trop à des clubs), comme les matchs déjà terminés."""
    from . import odds
    items, missing, seen = [], 0, 0
    counts_ = {k: len(v) for k, v in (scan or {}).get("leagues", {}).items()}
    for key in catalogue.prioritise(counts_, C.SPORTS):
        if catalogue.region(key) == "monde":
            continue
        for ev in sorted(scan["leagues"][key], key=lambda e: e["commence_time"]):
            if seen >= EVAL_MAX:
                break
            seen += 1
            if rich(odds.match_numbers(ev["home_team"], ev["away_team"], key)):
                continue
            fx = stats.find_fixture(day, ev["home_team"], ev["away_team"], ev["commence_time"])
            if not fx:
                missing += 1
                continue
            items.append({"home": ev["home_team"], "away": ev["away_team"], "hid": fx["home_id"], "aid": fx["away_id"],
                          "sk": key, "lg": catalogue.name(key), "ts": ev["commence_time"]})
    for f in sorted((f for f in fixtures if f.get("lg") and f.get("status") not in stats.DONE), key=lambda f: f.get("ts") or 0):
        items.append({"home": f["home"], "away": f["away"], "hid": f["home_id"], "aid": f["away_id"], "sk": "africa",
                      "lg": f["lg"], "ts": f.get("ts")})
    return items, missing


def fill(day, scan, deadline=None):
    """Remplissage progressif du matin (voir l'en-tête du module). Ne lève jamais. -> rapport {enabled, queued, covered,
    unmatched, teams, requests, aps_today, used_today, stop, err} pour report_lines()."""
    rep = {"enabled": active(), "queued": 0, "covered": 0, "unmatched": 0, "teams": 0, "requests": 0, "stop": None}
    if not rep["enabled"]:
        rep["stop"] = "off" if stats.enabled() else "nokey"
        return rep
    try:
        _fill(day, scan, deadline or time.monotonic() + C.APISTATS_SECONDS, rep)
    except Exception as e:                            # noqa: BLE001 - les stats mondiales sont facultatives
        print(f"[apistats] ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        rep["stop"] = "error"
    q = stats._quota()
    rep["used_today"], rep["aps_today"], rep["err"] = q["n"], q.get("aps", 0), q.get("err")
    return rep


def _fill(day, scan, deadline, rep):
    budget = Budget()
    if budget.left() < 2:
        rep["stop"] = "halt" if stats._halted else "budget"
        return
    fixtures = stats._day_fixtures(str(day))
    if stats._halted or not fixtures:
        rep["stop"] = "halt" if stats._halted else "empty"
        return
    items, rep["unmatched"] = queue(day, scan, fixtures)
    rep["queued"] = len(items)
    d = load()
    stopped, done = None, set()
    for stage in ("new", "stale"):                    # d'abord les équipes inconnues, puis le renouvellement des profils
        for it in items:
            for tid, nm in ((it["hid"], it["home"]), (it["aid"], it["away"])):
                known = covered_team(d["teams"].get(str(tid)))
                if tid in done or _fresh(d, tid) or (stage == "stale") != known:
                    continue
                if time.monotonic() > deadline:
                    stopped = "time"
                elif not refresh_team(d, tid, nm, it, budget):
                    stopped = "halt" if stats._halted else "budget"
                if stopped:
                    break
                done.add(tid)
                rep["teams"] += 1
                save(d)
            if stopped:
                break
        if stopped:
            break
    save(d)
    budget.commit()
    rep["requests"] = budget.spent() - budget.aps0
    rep["covered"] = sum(1 for it in items if covered_team(d["teams"].get(str(it["hid"]))) and covered_team(d["teams"].get(str(it["aid"]))))
    rep["stop"] = stopped
    try:
        from .sources import features
        features.reload()                             # les profils neufs servent tout de suite aux pronostics et aux cotes
    except Exception:                                 # noqa: BLE001
        pass


# ---------- messages (journal du matin, --check) ----------

def plan_error(q=None):
    """Erreur de plan ou de saison d'API-Football relevée aujourd'hui (texte), sinon None."""
    err = (q if q is not None else stats._quota()).get("err")
    return err["msg"] if err and err.get("plan") else None


def report_lines(rep):
    """Lignes françaises du journal du matin : couverture du jour, requêtes, et l'erreur de plan (une seule fois)."""
    if rep.get("stop") in ("nokey", "off"):
        return ["⚪ Stats mondiales : " + ("pas de clé STATS_API_KEY" if rep["stop"] == "nokey" else "désactivées (APISTATS_DAILY=0)")]
    lines = []
    err = plan_error() if rep.get("err") is None else (rep["err"]["msg"] if rep["err"].get("plan") else None)
    if err:
        lines.append(f"⚠️ API-Football : ton forfait ne donne pas accès à ces données ({err[:140]}). Les stats mondiales "
                     "(Brésil, Afrique...) sont suspendues : Kelly continue avec football-data et les cotes. Un forfait "
                     "payant ou une autre saison les débloquerait.")
    total = rep["queued"]
    if err and not total:                             # arrêt avant même d'avoir la liste des matchs : le message d'erreur suffit
        return lines
    lines.append(f"🌍 Stats mondiales : {rep['teams']} équipe(s) mise(s) à jour ({rep['requests']} requêtes ; {rep.get('aps_today', 0)}/"
                 f"{C.APISTATS_DAILY} aujourd'hui pour les profils, {rep.get('used_today', '?')}/{stats._cap()} en tout, "
                 f"{C.FAIR_API_RESERVE} gardées pour le règlement) ; matchs du jour sans stats gratuites : "
                 f"{rep['covered']}/{total} couverts"
                 + (f", {rep['unmatched']} introuvables chez API-Football" if rep.get("unmatched") else "")
                 + {"budget": " — budget du jour atteint, suite demain", "time": " — délai atteint, suite demain",
                    "halt": " — API arrêtée pour aujourd'hui"}.get(rep.get("stop"), ""))
    return lines


def check_lines():
    """Lignes de --check : profils en mémoire, requêtes du jour, erreur de plan éventuelle."""
    n, cov, last = counts()
    q = stats._quota()
    lines = []
    err = plan_error(q)
    if err:
        lines.append(f"   ⚠️ Stats mondiales suspendues : API-Football refuse ({err[:140]}). Forfait ou saison non inclus.")
    if not stats.enabled():
        return ["   Stats mondiales : inactives (STATS_API_KEY absente)"] + lines
    lines.insert(0, f"   Stats mondiales : {cov} équipes couvertes sur {n} suivies (maj {last or '?'}), "
                    f"{q.get('aps', 0)}/{C.APISTATS_DAILY} requêtes de profils aujourd'hui, {C.FAIR_API_RESERVE} gardées pour le règlement")
    return lines
