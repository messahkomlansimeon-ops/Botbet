"""Stats mondiales via API-Football : historique d'équipe pour les championnats que football-data ne couvre pas (Brésil, Argentine,
MLS, Asie, Afrique, sélections...), accumulé jour après jour avec le FORFAIT GRATUIT.

Le forfait gratuit d'API-Football refuse « last » et « ids » et ne donne que hier, aujourd'hui et demain pour /fixtures?date= :
impossible d'aller chercher les derniers matchs d'une équipe. Kelly fait donc l'inverse, chaque matin :

1. il lit la liste de TOUS les matchs d'hier (une requête, ~230 matchs dans le monde, celle du règlement : stats._day_fixtures ne la
   télécharge jamais deux fois) et celle d'aujourd'hui (les matchs terminés dans la nuit : Amériques, Asie) ;
2. il choisit les matchs terminés qui valent une requête (apileagues : ligues du catalogue sans corners ni cartons chez football-data,
   Afrique, sélections, ligues hors catalogue en dernier), dans l'ordre : catégorie de ligue (une équipe « saturée », avec déjà
   APISTATS_SAT_N matchs, passe après), puis équipes qui rejouent aujourd'hui, puis équipes les moins fournies ;
3. il demande /fixtures/statistics?fixture=ID un match à la fois (jamais /fixtures/players), dans la limite de APISTATS_DAILY requêtes
   par jour et jamais sous la réserve FAIR_API_RESERVE du règlement ; les statistiques d'un match sont gardées pour toujours
   (stats.fixture_stats, cache du disque) et un match déjà ajouté n'est jamais redemandé (clé « seen ») ;
4. il ajoute le match à l'historique compact des DEUX équipes (buts, tirs, tirs cadrés, corners, fautes, cartons et buts attendus
   (xG) pour et contre, domicile / extérieur, date, ligue), garde les APISTATS_KEEP_MATCHES derniers matchs par équipe. Le xG vient
   de la même requête /fixtures/statistics (« expected_goals » : zéro requête de plus) ; il n'est pas rattrapable (forfait gratuit :
   hier..demain seulement), il s'accumule à partir de maintenant.

Une équipe est « utilisable » dès APISTATS_MIN_N (3) matchs, avec une confiance réduite (features.aps_quality, poids du modèle
atténué dans prematch, pronostics seuls), et « bonne » dès APISTATS_GOOD_N (6) : confiance pleine, admise dans les combinés à cote
juste. Les profils servis (profiles()) ont le format de football-data (n, goals_for, goals_against, shots_for ...), moyennes sur les
APISTATS_LAST derniers matchs, et sources.features les consomme tels quels.

Si le forfait refuse les statistiques (erreur de plan), tous les appels de la journée s'arrêtent (stats.get), le journal du matin et
--check le disent une fois, en français, et le bot continue avec football-data et les cotes.

Cadence : le forfait gratuit limite à ~10 requêtes par minute. stats.get espace deux appels d'au moins STATS_MIN_GAP (6,2 s) et, sur un HTTP 429
quand même, attend STATS_RATE_WAIT (60 s) avant UN nouvel essai ; tout cela dans le délai APISTATS_SECONDS du passage (360 s : 55 requêtes
espacées de 6,2 s tiennent en ~340 s). Un second 429 arrête l'API « pour ce passage » (pas pour la journée) ; le délai épuisé s'appelle « time ».

Stockage compact : data/apistats_profiles.json, < 300 Ko. Une ligne de match = [jour AAMMJJ, ligue, domicile (1/0), buts pour, contre,
tirs pour, contre, tirs cadrés, corners, fautes, cartons, xG (chaque fois pour, contre)] ; None quand l'API n'a pas la statistique.
Les clés ajoutées plus tard (xg, puis yellow : cartons jaunes seuls) sont À LA FIN de KEYS : une ligne ancienne, plus courte, se lit avec None aux colonnes manquantes (xG
inconnu, jamais 0), et une ligne neuve sans xG est écrite sans ces deux colonnes (le fichier reste petit). Aucune migration.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

from . import apileagues, catalogue, config as C, stats, store

FILE = "apistats_profiles.json"
KEYS = ("goals", "shots", "sot", "corners", "fouls", "cards", "xg", "yellow")  # ordre des paires dans une ligne de match ; nouvelles clés : toujours à la fin
FIELD = {"shots": "shots", "sot": "sot", "corners": "corners", "fouls": "fouls", "cards": "cards", "xg": "xg", "yellow": "yel"}
OPTIONAL = ("xg", "yellow")                                          # colonnes de fin de ligne omises tant qu'elles sont inconnues
FLOAT_KEYS = ("xg",)                                                 # valeurs décimales, arrondies à 2 décimales dans une ligne
R_DAY, R_LG, R_HOME, R_STATS = 0, 1, 2, 3                            # indices d'une ligne ; les paires (pour, contre) suivent
SEEN_DAYS = 5                                                        # un match ne reste visible que 2 jours dans les listes de l'API
SOON_BONUS = 15                                                      # par équipe qui rejoue aujourd'hui
SAT_PENALTY = 60                                                     # priorité retirée aux matchs de deux équipes saturées
MISS_STOP = 3                                                        # matchs d'une ligue sans statistiques (sans aucun succès) avant d'abandonner la ligue
EVAL_MAX = 150                                                       # matchs du relevé examinés au plus pour la couverture du jour


def active():
    """Vrai si l'accumulation des profils est possible (clé API-Football et budget APISTATS_DAILY non nul)."""
    return stats.enabled() and C.APISTATS_DAILY > 0


def _today():
    return stats._today()


# ---------- stockage ----------

def _d2i(iso):
    return int(iso[2:4] + iso[5:7] + iso[8:10])


def _i2d(i):
    return f"20{i // 10000:02d}-{i // 100 % 100:02d}-{i % 100:02d}"


def _num(x, nd=1):
    return None if x is None else (int(x) if float(x) == int(x) else round(float(x), nd))


def _cell(r, j):
    """Colonne `j` d'une ligne ; None si la ligne est plus courte (ligne écrite avant l'ajout de la clé : donnée inconnue, jamais 0)."""
    return r[j] if j < len(r) else None


def load():
    d = store.load(FILE, {})
    d = d if isinstance(d, dict) else {}
    teams = d.get("teams")
    d["teams"] = {k: t for k, t in teams.items() if isinstance(t, dict) and isinstance(t.get("m"), list)} if isinstance(teams, dict) else {}
    if not isinstance(d.get("lg"), list):
        d["lg"] = []
    if not isinstance(d.get("seen"), dict):
        d["seen"] = {}
    return d


def save(d):
    """Écrit le fichier en oubliant les équipes qui n'ont pas été mises à jour depuis APISTATS_KEEP_DAYS jours, puis les moins récentes
    au-delà de APISTATS_MAX_TEAMS (le fichier reste sous 300 Ko), et les matchs « déjà vus » trop anciens pour revenir dans une liste."""
    today = datetime.fromisoformat(_today())
    cut = str((today - timedelta(days=C.APISTATS_KEEP_DAYS)).date())
    teams = {k: v for k, v in d["teams"].items() if v.get("d", "0") >= cut and v.get("m")}
    if len(teams) > C.APISTATS_MAX_TEAMS:
        keep = sorted(teams, key=lambda k: (teams[k].get("d", ""), len(teams[k]["m"])), reverse=True)[:C.APISTATS_MAX_TEAMS]
        teams = {k: teams[k] for k in keep}
    d["teams"] = teams
    floor = _d2i(str((today - timedelta(days=SEEN_DAYS)).date()))
    d["seen"] = {k: v for k, v in d.get("seen", {}).items() if v >= floor}
    d["v"] = 2
    while len(d["teams"]) > 1 and len(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode()) > C.APISTATS_MAX_BYTES:
        old = sorted(d["teams"], key=lambda k: (d["teams"][k].get("d", ""), len(d["teams"][k]["m"])))      # les xG ajoutent ~15 % par ligne : jamais > 300 Ko
        for k in old[:max(1, len(old) // 20)]:
            del d["teams"][k]
    store.save(FILE, d, compact=True)


def n_matches(t):
    return len((t or {}).get("m") or [])


def covered_team(t):
    """Équipe « utilisable » : au moins APISTATS_MIN_N matchs avec statistiques."""
    return n_matches(t) >= C.APISTATS_MIN_N


def _league_index(d, label):
    if label not in d["lg"]:
        d["lg"].append(label)
    return d["lg"].index(label)


def _usable(side, f):
    """La statistique `f` d'une équipe (stats._team_values) est-elle connue ? Un « nul » est une donnée absente, sauf corners et cartons
    quand l'équipe a d'autres statistiques (tirs ou fautes) : API-Football envoie null pour zéro. Les entrées de cache écrites avant
    cette règle marquaient ces zéros « nul » : les écarter faisait perdre le match entier à l'équipe qui n'avait aucun carton."""
    if f not in side:
        return False
    nul = side.get("nul") or []
    return f not in nul or (f in ("corners", "cards", "yel") and any(k not in nul for k in ("shots", "fouls")))


def record(d, e, st):
    """Ajoute le match `e` (entrée de stats._day_fixtures, terminé) dont `st` donne les statistiques (stats.fixture_stats) à l'historique
    des deux équipes, et le note « vu ». -> nombre d'équipes mises à jour. Un match déjà vu n'est jamais ajouté deux fois."""
    fid = str(e["id"])
    if fid in d["seen"]:
        return 0
    ts = e.get("ts")
    day = _d2i(datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat() if ts else _today())
    d["seen"][fid] = day
    teams = (st or {}).get("teams") or {}
    label = e.get("ln") or e.get("lg") or "?"
    lgi, n = _league_index(d, label), 0
    for tid, opp, home, name in ((e["home_id"], e["away_id"], 1, e["home"]), (e["away_id"], e["home_id"], 0, e["away"])):
        mine, other = teams.get(str(tid)), teams.get(str(opp))
        if not mine or not other:
            continue
        pairs = [(e.get("gh"), e.get("ga")) if home else (e.get("ga"), e.get("gh"))]
        for k in KEYS[1:]:
            f = FIELD[k]
            ok = _usable(mine, f) and _usable(other, f)
            pairs.append((mine[f], other[f]) if ok else (None, None))
        xg = pairs[KEYS.index("xg")]
        if xg == (0, 0) and (e.get("gh") or 0) + (e.get("ga") or 0) > 0:
            pairs[KEYS.index("xg")] = (None, None)   # xG 0 - 0 alors qu'il y a des buts : non suivi par l'API pour ce match, pas un vrai zéro
        if all(p == (None, None) for k, p in zip(KEYS[1:], pairs[1:]) if k not in FLOAT_KEYS and k != "yellow"):
            continue                                  # match sans aucune statistique utilisable (le xG ou les jaunes seuls ne suffisent pas)
        row = [day, lgi, home] + [_num(x, 2 if k in FLOAT_KEYS else 1) for k, p in zip(KEYS, pairs) for x in p]
        first_opt = R_STATS + 2 * min(KEYS.index(k) for k in OPTIONAL)
        while len(row) > first_opt and row[-2:] == [None, None]:      # xG / jaunes inconnus en fin de ligne : colonnes omises, même lecture qu'une ligne ancienne
            del row[-2:]
        t = d["teams"].setdefault(str(tid), {"m": []})
        lk = e.get("lk")
        t.update(nm=name, lg=label, d=_today(), sk=list(dict.fromkeys([k for k in t.get("sk", []) if k != lk] + ([lk] if lk else [])))[-4:])
        t["m"] = sorted(t["m"] + [row], key=lambda r: r[R_DAY])[-C.APISTATS_KEEP_MATCHES:]
        n += 1
    return n


def _views(rows):
    from .sources import footballdata as fd
    recs = lambda rs: [{k: (_cell(r, R_STATS + 2 * i), _cell(r, R_STATS + 2 * i + 1)) for i, k in enumerate(KEYS)} for r in rs]   # noqa: E731

    def view(rs):
        rc = recs(rs)
        v = fd._avg_view(rc, keys=KEYS, min_count=min(C.APISTATS_MIN_N, len(rs)))
        v["xg_n"] = sum(1 for r in rc if r["xg"][0] is not None and r["xg"][1] is not None)      # matchs qui portent un xG (comme fmmatch)
        return v

    n = C.APISTATS_LAST
    return {"all": view(rows[-n:]), "home": view([r for r in rows if r[R_HOME]][-n:]), "away": view([r for r in rows if not r[R_HOME]][-n:])}


def profiles():
    """Profils au format de football-data : {"teams": {nom: {id, sk, lg, last, all/home/away: {n, goals_for...}}}, "updated"}.
    Une équipe par nom (et par alias : le nom donné par The Odds API) ; seules les équipes utilisables (3+ matchs) sont servies."""
    d = load()
    teams, updated = {}, ""
    for tid, t in d["teams"].items():
        if not covered_team(t):
            continue
        p = {"id": int(tid), "nm": t.get("nm"), "sk": t.get("sk", []), "lg": t.get("lg"), "last": _i2d(t["m"][-1][R_DAY]),
             "d": t.get("d")} | _views(t["m"])
        updated = max(updated, t.get("d", ""))
        for name in [t.get("nm")] + list(t.get("al") or []):
            if name and (name not in teams or n_matches(t) > teams[name]["all"]["n"]):
                teams[name] = p
    return {"teams": teams, "updated": updated}


def averages(tid):
    """Moyennes corners / cartons pour et contre d'une équipe sur ses APISTATS_LAST derniers matchs (stats.team_averages) ; None
    sous APISTATS_MIN_N matchs avec ces statistiques. Lecture du fichier : aucune requête."""
    rows = (load()["teams"].get(str(tid)) or {}).get("m") or []
    ic, ik = R_STATS + 2 * KEYS.index("corners"), R_STATS + 2 * KEYS.index("cards")
    rows = [r for r in rows if None not in (r[ic], r[ic + 1], r[ik], r[ik + 1])][-C.APISTATS_LAST:]
    if len(rows) < C.APISTATS_MIN_N:
        return None
    n = len(rows)
    return {"corners_for": sum(r[ic] for r in rows) / n, "corners_against": sum(r[ic + 1] for r in rows) / n,
            "cards_for": sum(r[ik] for r in rows) / n, "cards_against": sum(r[ik + 1] for r in rows) / n, "n": n,
            "day": _i2d(rows[-1][R_DAY])}


def summary(d=None):
    """Avancement de l'accumulation : équipes suivies, utilisables (3+ matchs), bonnes (6+), dernière mise à jour."""
    d = d or load()
    ns = [n_matches(t) for t in d["teams"].values()]
    return {"tracked": len(ns), "usable": sum(1 for n in ns if n >= C.APISTATS_MIN_N), "good": sum(1 for n in ns if n >= C.APISTATS_GOOD_N),
            "last": max((t.get("d", "") for t in d["teams"].values()), default="")}


def counts():
    """(équipes suivies, équipes utilisables, dernière mise à jour)."""
    s = summary()
    return s["tracked"], s["usable"], s["last"]


# ---------- budget ----------

class Budget:
    """Requêtes API-Football encore permises pour les profils aujourd'hui : APISTATS_DAILY moins ce qui a déjà servi aux
    profils (clé « aps » de stats_quota.json, listes de matchs comprises), et jamais sous la réserve du règlement."""

    def __init__(self):
        q = stats._quota()
        self.n0, self.aps0 = q["n"], q.get("aps", 0)

    def spent(self):
        return self.aps0 + max(0, stats._quota()["n"] - self.n0)

    def left(self):
        if stats._halted or stats._calls >= stats.BUDGET:
            return 0
        return max(0, min(C.APISTATS_DAILY - self.spent(), stats.left_today() - C.FAIR_API_RESERVE))

    def commit(self):
        q = stats._quota()
        q["aps"] = self.spent()
        store.save("stats_quota.json", q)


# ---------- choix des matchs ----------

def gain(n):
    """Ce qu'un match de plus apporte à une équipe de `n` matchs : beaucoup jusqu'à la qualité « bonne », peu ensuite."""
    return 4 * max(0, C.APISTATS_GOOD_N - n) + max(0, C.APISTATS_SAT_N - n)


def rank(d, e, soon):
    """Clé de tri d'un match terminé à payer (plus petit = d'abord) : catégorie de ligue (saturé = plus bas), puis équipes qui rejouent
    aujourd'hui et équipes les moins fournies, puis l'heure."""
    n_h, n_a = n_matches(d["teams"].get(str(e["home_id"]))), n_matches(d["teams"].get(str(e["away_id"])))
    pri = apileagues.priority(e.get("lk")) - (SAT_PENALTY if min(n_h, n_a) >= C.APISTATS_SAT_N else 0)
    worth = SOON_BONUS * sum(1 for t in (e["home_id"], e["away_id"]) if t in soon) + gain(n_h) + gain(n_a)
    return (-pri, -worth, e.get("ts") or 0)


def candidates(d, lists, today):
    """Matchs terminés des listes `lists` {jour: matchs} (hier, aujourd'hui) à ajouter : ligue retenue (« lk »), pas encore vus.
    -> (à coûter une requête triés, à ajouter gratuitement car leurs statistiques sont déjà en cache, nombre sans statistiques connues)."""
    soon = {t for f in lists.get(today, []) if f.get("status") in stats.PENDING for t in (f["home_id"], f["away_id"])}
    cache_ = stats._stats_cache()
    paid, free, none = [], [], 0
    for day in sorted(lists):
        for e in lists[day]:
            if e.get("status") not in stats.DONE or not e.get("lk") or str(e["id"]) in d["seen"]:
                continue
            e = dict(e, day=day)
            if (cache_.get(str(e["id"])) or {}).get("teams"):
                free.append(e)
            elif stats.stats_known(e["id"]):          # l'API n'a pas de statistiques pour ce match (mémorisé quelques jours)
                none += 1
            else:
                paid.append(e)
    return sorted(paid, key=lambda e: rank(d, e, soon)), free, none


def rich(f):
    """Vrai si les chiffres d'avant-match `f` ont déjà corners ET cartons d'une autre source que ce module."""
    own = set(f.get("aps_keys") or ())
    return all(k in f and k not in own for k in ("exp_corners", "exp_cards"))


def today_matches(day, scan, fixtures):
    """Matchs du jour sans vraies stats gratuites : ceux du relevé gratuit des ligues (hors matchs déjà détaillés par football-data) et les
    matchs africains de la liste du jour. -> (liste, non retrouvés) ; chaque élément : {home, away, hid, aid, sk, lg, ts}."""
    from . import odds
    items, missing, seen = [], 0, 0
    counts_ = {k: len(v) for k, v in (scan or {}).get("leagues", {}).items()}
    for key in catalogue.prioritise(counts_, C.SPORTS):
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


def _alias(d, items):
    """Retient le nom donné par The Odds API (« Nautico PE ») et la ligue du catalogue sur les équipes déjà suivies."""
    for it in items:
        for tid, nm in ((it["hid"], it["home"]), (it["aid"], it["away"])):
            t = d["teams"].get(str(tid))
            if not t:
                continue
            if nm != t.get("nm") and nm not in (t.get("al") or []):
                t["al"] = ([nm] + (t.get("al") or []))[:2]
            if it["sk"] not in t.get("sk", []):
                t["sk"] = (t.get("sk", []) + [it["sk"]])[-4:]


# ---------- le passage du matin ----------

def fill(day, scan, deadline=None):
    """Accumulation du matin (voir l'en-tête du module). Ne lève jamais. -> rapport {enabled, added, requests, tracked, usable, good, cov, tot,
    unmatched, lost, nostats, stop, err, used_today, aps_today} pour report_lines()."""
    rep = {"enabled": active(), "added": 0, "requests": 0, "tracked": 0, "usable": 0, "good": 0, "cov": 0, "tot": 0, "unmatched": 0,
           "lost": 0, "nostats": 0, "stop": None}
    if not rep["enabled"]:
        rep["stop"] = "off" if stats.enabled() else "nokey"
        return rep
    deadline = deadline or stats._clock() + C.APISTATS_SECONDS
    stats.set_deadline(deadline)                      # get() n'attend (écart de STATS_MIN_GAP entre deux appels, 60 s après un 429) jamais au-delà
    try:
        _fill(str(day)[:10], scan, deadline, rep)
    except Exception as e:                            # noqa: BLE001 - les stats mondiales sont facultatives
        print(f"[apistats] ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        rep["stop"] = "error"
    finally:
        stats.set_deadline(None)                      # le règlement et les autres appels de la journée ne sont pas bornés par ce délai
    q = stats._quota()
    rep["used_today"], rep["aps_today"], rep["err"] = q["n"], q.get("aps", 0), q.get("err")
    return rep


def _fill(day, scan, deadline, rep):
    budget, d = Budget(), load()
    yday = str((datetime.fromisoformat(day) - timedelta(days=1)).date())
    try:
        if plan_error():                              # refus de plan déjà constaté aujourd'hui : on ne réessaie pas avant demain
            rep["stop"] = "halt"
            return
        lists = {}
        for dd in (yday, day):                        # une requête chacune au plus (celle du règlement, en cache), jamais hors hier..demain
            lists[dd] = stats._day_fixtures(dd) if budget.left() >= 1 else stats._fx_load()[0].get(dd) or []
        if stats._halted:
            rep["stop"] = "halt"
            return
        if not lists.get(yday) and not lists.get(day):
            rep["stop"] = "budget" if budget.left() < 1 else "time" if stats.deferred() else "empty"
        else:
            _accumulate(d, lists, day, budget, deadline, rep)
        items, rep["unmatched"] = today_matches(day, scan, lists[day]) if lists.get(day) else ([], 0)      # sans liste du jour : aucune requête de plus
        rep["tot"] = len(items)
        rep["cov"] = sum(1 for it in items if covered_team(d["teams"].get(str(it["hid"]))) and covered_team(d["teams"].get(str(it["aid"]))))
        _alias(d, items)
    finally:
        s = summary(d)
        rep.update(tracked=s["tracked"], usable=s["usable"], good=s["good"], requests=budget.spent() - budget.aps0)
        d["rep"] = {"day": day, "added": rep["added"], "requests": rep["requests"], "cov": rep["cov"], "tot": rep["tot"], "stop": rep["stop"]}
        save(d)
        budget.commit()
        try:
            from .sources import features
            features.reload()                         # les profils neufs servent tout de suite aux pronostics et aux cotes
        except Exception:                             # noqa: BLE001
            pass


def _accumulate(d, lists, day, budget, deadline, rep):
    paid, free, _ = candidates(d, lists, day)
    cache_ = stats._stats_cache()
    for e in free:                                    # statistiques déjà en cache (règlement, passage précédent) : zéro requête
        if record(d, e, cache_.get(str(e["id"]))):
            rep["added"] += 1
    miss, ok = {}, {}                                 # par ligue : matchs sans statistiques / avec
    dead = lambda lk: miss.get(lk, 0) >= MISS_STOP and not ok.get(lk)        # noqa: E731 - ligue sans statistiques chez l'API aujourd'hui

    def stopped(why, rest):
        """Arrêt du passage (délai, budget, API arrêtée) : les matchs d'HIER qui restent ne reviendront pas demain (la liste d'hier sort de
        la fenêtre du forfait gratuit), quelle que soit la cause ; ceux d'aujourd'hui, si. Les ligues abandonnées (sans statistiques) ne comptent pas."""
        rep["stop"] = why
        rep["lost"] = sum(1 for x in rest if x["day"] != day and not dead(x["lk"]))

    for i, e in enumerate(paid):
        stop = "time" if stats._clock() > deadline else "halt" if stats._halted else "budget" if budget.left() < 1 else None
        if stop:
            stopped(stop, paid[i:])
            return
        if dead(e["lk"]):
            continue
        st = stats.fixture_stats(e["id"], players=False)
        if st and record(d, e, st):
            rep["added"] += 1
            ok[e["lk"]] = ok.get(e["lk"], 0) + 1
        elif stats._halted:
            stopped("halt", paid[i:])
            return
        elif stats.deferred():                        # pas le temps d'attendre l'écart entre deux appels ou la fin d'une limite de fréquence
            stopped("time", paid[i:])
            return
        else:
            rep["nostats"] += 1
            miss[e["lk"]] = miss.get(e["lk"], 0) + 1


# ---------- messages (journal du matin, --check) ----------

def plan_error(q=None):
    """Erreur de plan ou de saison d'API-Football relevée aujourd'hui (texte), sinon None."""
    err = (q if q is not None else stats._quota()).get("err")
    return err["msg"] if err and err.get("plan") else None


def _plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def _levels(tracked, usable, good):
    return (f"{tracked} équipes suivies, {usable} avec {C.APISTATS_MIN_N}+ matchs (utilisables), "
            f"{good} avec {C.APISTATS_GOOD_N}+ (bonnes)")


def progress_line(rep):
    """Ligne d'avancement honnête : matchs ajoutés aujourd'hui, requêtes, équipes suivies / utilisables / bonnes, couverture du jour."""
    added = rep["added"]
    got = "aucun nouveau match d'hier" if not added else _plural(added, "match d'hier ajouté", "matchs d'hier ajoutés")
    line = (f"🌍 Stats mondiales (accumulation jour après jour, forfait gratuit) : {got} ({_plural(rep['requests'], 'requête', 'requêtes')}"
            + (f", {rep['nostats']} sans statistiques chez l'API" if rep.get("nostats") else "") + ") · "
            + _levels(rep["tracked"], rep["usable"], rep["good"]) + " · "
            + (f"couverture des matchs du jour : {rep['cov']}/{rep['tot']}" if rep["tot"] else "aucun match du jour à couvrir"))
    if rep.get("unmatched"):
        line += f" ({rep['unmatched']} introuvables chez API-Football)"
    line += {"budget": f" — budget du jour atteint ({C.APISTATS_DAILY} requêtes), suite demain", "time": " — délai atteint, suite demain",
             "halt": " — API arrêtée pour ce passage, suite demain", "empty": " — liste des matchs vide"}.get(rep.get("stop"), "")
    if rep.get("lost"):
        why = {"time": "faute de temps", "halt": "l'API s'étant arrêtée"}.get(rep.get("stop"), "faute de budget")
        line += (f" ; {_plural(rep['lost'], 'match d’hier perdu', 'matchs d’hier perdus')} {why} (le forfait gratuit ne "
                 "remonte pas plus loin)")
    return line


def report_lines(rep):
    """Lignes françaises du journal du matin : avancement de l'accumulation, requêtes, seuils de qualité, et l'erreur de plan (une fois)."""
    if rep.get("stop") in ("nokey", "off"):
        return ["⚪ Stats mondiales : " + ("pas de clé STATS_API_KEY" if rep["stop"] == "nokey" else "désactivées (APISTATS_DAILY=0)")]
    err = plan_error() if rep.get("err") is None else (rep["err"]["msg"] if rep["err"].get("plan") else None)
    if err:
        path = (rep.get("err") or stats._quota().get("err") or {}).get("path") or ""
        what = "aux statistiques de match" if "statistics" in path else "à ces données"
        return [f"⚠️ API-Football : ton forfait n'a pas accès {what} ({err[:140]}). L'accumulation des stats mondiales (Brésil, Afrique...) "
                "s'arrête pour aujourd'hui : Kelly continue avec football-data et les cotes. Un forfait payant la débloquerait.",
                f"🌍 Stats mondiales suspendues : {_levels(rep['tracked'], rep['usable'], rep['good'])}, rien d'ajouté aujourd'hui"]
    return [progress_line(rep),
            f"   Qualité : {C.APISTATS_MIN_N} à {C.APISTATS_GOOD_N - 1} matchs = utilisable (confiance réduite, pronostics seuls), "
            f"{C.APISTATS_GOOD_N}+ = bon (confiance pleine, combinés à cote juste) ; API-Football aujourd'hui : {rep.get('used_today', '?')}/"
            f"{stats._cap()} requêtes (profils {rep.get('aps_today', 0)}/{C.APISTATS_DAILY}, {C.FAIR_API_RESERVE} gardées pour le règlement)"]


def check_lines():
    """Lignes de --check : avancement de l'accumulation, dernier passage du matin, requêtes du jour, erreur de plan éventuelle."""
    d = load()
    s, q = summary(d), stats._quota()
    lines = []
    err = plan_error(q)
    if err:
        lines.append(f"   ⚠️ Stats mondiales suspendues : API-Football refuse ({err[:140]}). Forfait ou saison non inclus.")
    if not stats.enabled():
        return ["   Stats mondiales : inactives (STATS_API_KEY absente)"] + lines
    rp = d.get("rep")
    last = (f" · dernier passage du matin ({rp['day']}) : {rp['added']} matchs ajoutés ({rp['requests']} requêtes), couverture des matchs du "
            f"jour {rp['cov']}/{rp['tot']}") if rp else " · aucun passage du matin enregistré"
    lines.insert(0, f"   Stats mondiales (accumulation jour après jour, forfait gratuit) : {_levels(s['tracked'], s['usable'], s['good'])}"
                    f"{last}")
    lines.insert(1, f"   {q.get('aps', 0)}/{C.APISTATS_DAILY} requêtes de profils aujourd'hui, {C.FAIR_API_RESERVE} gardées pour le règlement")
    return lines
