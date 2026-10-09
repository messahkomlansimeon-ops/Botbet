"""Statistiques de match FotMob : touches, hors-jeu, fautes, corners, tirs, tirs cadrés, cartons et buts de chaque équipe, pour toutes
les ligues mappées dans fotmob.py (Europe, Brésil A et B, Argentine, Mexique, MLS, Chili, Libertadores, Égypte...).

Pages HTML /matches/<slug>/<id> autorisées par robots.txt (seul /api/* est interdit, jamais utilisé). La page d'une ligue (téléchargée
le matin par fotmob.refresh, relue ici depuis le cache) liste tous les matchs de la saison avec leur lien : contrairement au forfait
gratuit d'API-Football, Kelly peut donc remonter l'historique d'une équipe. Une page de match pèse ~1 Mo : le remplissage est étalé sur
les passages horaires (fill, FM_PAGES_PER_RUN pages au plus), jamais sur celui du matin, et la page brute n'est jamais gardée.

Fichier data/fm_matches.json (compact) :
  teams  {id FotMob: {nm, sk: [clés de ligue], d: mise à jour, m: [[jour AAMMJJ, domicile 1/0, puis (pour, contre) pour chaque stat de
         KEYS]]}} : FM_KEEP_MATCHES derniers matchs par équipe ; None quand la page n'a pas la stat (ligue mal couverte). Les clés
         ajoutées plus tard (xg, npxg) sont À LA FIN de KEYS : une ligne ancienne, plus courte, se lit avec None aux colonnes manquantes.
  seen   {id du match: jour} : jamais lu deux fois.
  recent {id du match: {d, sk, h, a, s: {stat: [domicile, extérieur]}}} : matchs des FM_RECENT_DAYS derniers jours, pour le règlement
         des paris sur touches, hors-jeu et fautes (match_stats).
Les profils servis (profiles()) ont le format de football-data (n, throws_for, throws_against...) et sources.features les consomme.
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from .. import config as C, store
from . import fotmob, http, names, state

FILE = "fm_matches.json"
KEYS = ("goals", "shots", "sot", "corners", "fouls", "cards", "throws", "offsides", "xg", "npxg")      # nouvelles clés : toujours à la fin
STAT_KEYS = {"total_shots": "shots", "ShotsOnTarget": "sot", "corners": "corners", "fouls": "fouls", "player_throws": "throws",
             "Offsides": "offsides", "yellow_cards": "yc", "red_cards": "rc", "expected_goals": "xg",
             "expected_goals_non_penalty": "npxg"}
FLOAT_KEYS = ("xg", "npxg")                  # valeurs décimales (« 1.62 »), arrondies à 2 décimales
NEVER_ZERO = ("throws", "shots")             # 0-0 sur ces stats = non suivie par FotMob pour ce match, pas un vrai zéro
R_DAY, R_HOME, R_STATS = 0, 1, 2
SOON_DAYS = 3                                # une équipe qui rejoue dans ce délai passe avant les autres
_settle_pages = {"n": 0}


def _today():
    return state.now().date()


def _d2i(iso):
    return int(iso[2:4] + iso[5:7] + iso[8:10])


def _i2d(i):
    return f"20{i // 10000:02d}-{i // 100 % 100:02d}-{i % 100:02d}"


def _int(v):
    try:
        return int(str(v).split(" ")[0])
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        x = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return round(x, 2) if 0 <= x < 20 else None         # nan, négatif ou absurde -> manquant


def _next_data(html):
    m = fotmob.PREFIX.search(html or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    except ValueError:
        return None


# ---------- lecture des pages ----------

def parse_match(html):
    """Page d'un match terminé -> {id, d, home, away, hid, aid, s: {stat: [domicile, extérieur]}} ; None si la page n'est pas celle
    d'un match terminé ou n'a aucune statistique."""
    pp = _next_data(html)
    g = (pp or {}).get("general") or {}
    if not g.get("finished"):
        return None
    teams = ((pp.get("header") or {}).get("teams") or [])
    if len(teams) != 2 or any(t.get("score") is None for t in teams):
        return None
    raw = {}
    for grp in ((((pp.get("content") or {}).get("stats") or {}).get("Periods") or {}).get("All") or {}).get("stats") or []:
        for s in grp.get("stats") or []:
            k, v = STAT_KEYS.get(s.get("key")), s.get("stats")
            if k and k not in raw and isinstance(v, list) and len(v) == 2:
                pair = [_float(v[0]), _float(v[1])] if k in FLOAT_KEYS else [_int(v[0]), _int(v[1])]
                if None not in pair:
                    raw[k] = pair
    st = {"goals": [_int(teams[0]["score"]), _int(teams[1]["score"])]}
    for k in ("shots", "sot", "corners", "fouls", "throws", "offsides"):
        if k in raw and not (k in NEVER_ZERO and raw[k] == [0, 0]):
            st[k] = raw[k]
    for k in FLOAT_KEYS:                             # xG 0.00-0.00 alors qu'il y a des buts : stat non suivie, pas un vrai zéro
        if k in raw and not (raw[k] == [0, 0] and sum(st["goals"]) > 0):
            st[k] = raw[k]
    evs = ((((pp.get("content") or {}).get("matchFacts") or {}).get("events") or {}).get("events")) or []
    if evs:                                          # cartons comptés sur le fil du match : la stat vaut parfois 0-0 à tort (Brésil)
        st["cards"] = [sum(1 for e in evs if e.get("type") == "Card" and bool(e.get("isHome")) == home) for home in (True, False)]
    elif "yc" in raw and raw["yc"] != [0, 0]:
        st["cards"] = [raw["yc"][0] + (raw.get("rc") or [0, 0])[0], raw["yc"][1] + (raw.get("rc") or [0, 0])[1]]
    if len(st) == 1:
        return None
    return {"id": str(g.get("matchId") or ""), "d": str(g.get("matchTimeUTCDate") or "")[:10], "home": teams[0].get("name"),
            "away": teams[1].get("name"), "hid": str(teams[0].get("id")), "aid": str(teams[1].get("id")), "s": st}


def league_matches(sport_key):
    """Matchs d'une ligue d'après sa page FotMob en cache (aucun téléchargement : fotmob.refresh l'a lue le matin) :
    [{id, url, d, home, away, hid, aid, done}]."""
    lgid = fotmob.EXTRA_LEAGUES.get(sport_key) or fotmob.SPORT_TO_LEAGUE.get(sport_key)
    res = http.cached(f"{fotmob.BASE}/leagues/{lgid}") if lgid else None
    pp = _next_data(res.text) if res and res.ok else None
    out = []
    for m in ((pp or {}).get("fixtures") or {}).get("allMatches") or []:
        st, h, a = m.get("status") or {}, m.get("home") or {}, m.get("away") or {}
        if st.get("cancelled") or not m.get("pageUrl") or not h.get("id") or not a.get("id"):
            continue
        out.append({"id": str(m.get("id")), "url": fotmob.BASE + m["pageUrl"].split("#")[0], "d": str(st.get("utcTime") or "")[:10],
                    "home": h.get("name"), "away": a.get("name"), "hid": str(h["id"]), "aid": str(a["id"]),
                    "done": bool(st.get("finished"))})
    return out


# ---------- stockage ----------

def load():
    d = store.load(FILE, {})
    d = d if isinstance(d, dict) else {}
    for k in ("teams", "seen", "recent"):
        if not isinstance(d.get(k), dict):
            d[k] = {}
    return d


def save(d):
    """Écrit le fichier en oubliant les équipes non mises à jour depuis FM_KEEP_DAYS jours, les matchs « vus » trop vieux pour revenir,
    et les matchs récents au-delà de FM_RECENT_DAYS jours."""
    today = _today()
    cut = str(today - timedelta(days=C.FM_KEEP_DAYS))
    d["teams"] = {k: v for k, v in d["teams"].items() if v.get("d", "0") >= cut and v.get("m")}
    floor = _d2i(cut)
    d["seen"] = {k: v for k, v in d["seen"].items() if v >= floor}
    rcut = str(today - timedelta(days=C.FM_RECENT_DAYS))
    d["recent"] = {k: v for k, v in d["recent"].items() if v.get("d", "0") >= rcut}
    d["v"] = 1
    store.save(FILE, d, compact=True)


def n_matches(t):
    return len((t or {}).get("m") or [])


def record(d, m, sport_key):
    """Ajoute le match lu `m` (parse_match) aux deux équipes et aux matchs récents. -> True s'il est neuf."""
    if not m or not m["id"] or m["id"] in d["seen"]:
        return False
    day = _d2i(m["d"] or str(_today()))
    d["seen"][m["id"]] = day
    d["recent"][m["id"]] = {"d": m["d"], "sk": sport_key, "h": m["home"], "a": m["away"], "s": m["s"]}
    for tid, name, home in ((m["hid"], m["home"], 1), (m["aid"], m["away"], 0)):
        row = [day, home]
        for k in KEYS:
            pair = m["s"].get(k)
            row += (pair if home else pair[::-1]) if pair else [None, None]
        t = d["teams"].setdefault(tid, {"m": []})
        t.update(nm=name, d=str(_today()), sk=list(dict.fromkeys([x for x in t.get("sk", []) if x != sport_key] + [sport_key]))[-4:])
        t["m"] = sorted(t["m"] + [row], key=lambda r: r[R_DAY])[-C.FM_KEEP_MATCHES:]
    return True


# ---------- profils ----------

def _views(rows):
    from . import footballdata as fd
    cell = lambda r, j: r[j] if j < len(r) else None          # noqa: E731 - ligne ancienne plus courte : colonne manquante = None
    recs = [{k: (cell(r, R_STATS + 2 * i), cell(r, R_STATS + 2 * i + 1)) for i, k in enumerate(KEYS)} for r in rows]
    n = C.APISTATS_LAST

    def view(rs):
        v = fd._avg_view(rs, keys=KEYS, min_count=min(C.APISTATS_MIN_N, len(rs)))
        v["xg_n"] = sum(1 for r in rs if r["xg"][0] is not None and r["xg"][1] is not None)      # matchs qui portent un xG
        return v

    return {"all": view(recs[-n:]), "home": view([x for x, r in zip(recs, rows) if r[R_HOME]][-n:]),
            "away": view([x for x, r in zip(recs, rows) if not r[R_HOME]][-n:])}


def profiles():
    """{"teams": {nom: {id, sk, all/home/away: {n, throws_for...}}}, "updated"} : équipes avec au moins APISTATS_MIN_N matchs."""
    d = load()
    teams, updated = {}, ""
    for tid, t in d["teams"].items():
        if n_matches(t) < C.APISTATS_MIN_N:
            continue
        p = {"id": tid, "nm": t.get("nm"), "sk": t.get("sk", []), "last": _i2d(t["m"][-1][R_DAY]), "d": t.get("d")} | _views(t["m"])
        updated = max(updated, t.get("d", ""))
        if t.get("nm") and (t["nm"] not in teams or n_matches(t) > teams[t["nm"]]["all"]["n"]):
            teams[t["nm"]] = p
    return {"teams": teams, "updated": updated}


def summary(d=None):
    d = d or load()
    ns = [n_matches(t) for t in d["teams"].values()]
    return {"tracked": len(ns), "usable": sum(1 for n in ns if n >= C.APISTATS_MIN_N), "good": sum(1 for n in ns if n >= C.APISTATS_GOOD_N)}


# ---------- remplissage (passages horaires) ----------

def leagues():
    """Ligues à remplir : celles que fotmob.refresh actualise le matin (leur page est en cache)."""
    return fotmob.leagues_for(C.SPORTS)


def candidates(d, today=None):
    """Matchs terminés à lire, du plus utile au moins utile : équipes qui rejouent bientôt ; parmi elles, d'abord celles qui n'ont pas
    encore APISTATS_MIN_N matchs (le minimum d'un pronostic), dans l'ordre de priorité du catalogue (5 grands championnats, puis le
    reste de l'Europe, les Amériques...) ; puis les moins fournies, puis les matchs les plus récents. Seuls les FM_KEEP_MATCHES
    derniers matchs de chaque équipe comptent, dans la fenêtre FM_KEEP_DAYS. Les ligues du catalogue (C.LEAGUE_KEYS) et les autres
    (championnats 1xBet : des centaines d'équipes à moins de 3 matchs, toutes prioritaires) alternent : les ligues établies gardent au
    moins la moitié des pages de chaque fill()."""
    today = today or _today()
    oldest, soon_end = str(today - timedelta(days=C.FM_KEEP_DAYS)), str(today + timedelta(days=SOON_DAYS))
    out, ids = [], set()
    rank = {k: i for i, k in enumerate(C.LEAGUE_KEYS)}             # ordre du catalogue : la priorité des ligues
    for sk in leagues():
        ms = league_matches(sk)
        soon = {t for m in ms if not m["done"] and str(today) <= m["d"] <= soon_end for t in (m["hid"], m["aid"])}
        done = sorted((m for m in ms if m["done"] and m["d"] >= oldest), key=lambda m: m["d"], reverse=True)
        per_team = {}
        for m in done:
            for t in (m["hid"], m["aid"]):
                per_team[t] = per_team.get(t, 0) + 1
            if m["id"] in d["seen"] or m["id"] in ids:          # un match de coupe peut figurer dans deux ligues
                continue
            fresh = [t for t in (m["hid"], m["aid"]) if per_team[t] <= C.FM_KEEP_MATCHES]
            if not fresh:
                continue
            have = min(n_matches(d["teams"].get(t)) for t in (m["hid"], m["aid"]))
            pri = (0 if any(t in soon for t in (m["hid"], m["aid"])) else 1, 0 if have < C.APISTATS_MIN_N else 1, rank.get(sk, len(rank)),
                   min(have, C.APISTATS_GOOD_N), -_d2i(m["d"]))
            ids.add(m["id"])
            out.append((pri, sk, m))
    ordered = [(sk, m) for _, sk, m in sorted(out, key=lambda x: x[0])]
    est = lambda sk: sk in C.LEAGUE_KEYS or any(sk.startswith(k) for k in C.LEAGUE_KEYS if k.endswith("_"))      # noqa: E731
    core, other = [x for x in ordered if est(x[0])], [x for x in ordered if not est(x[0])]
    mixed = []
    for i in range(max(len(core), len(other))):
        mixed += core[i:i + 1] + other[i:i + 1]
    return mixed


def _read_page(url, budget=None):
    """Télécharge et lit une page de match, sans garder la page brute (~1 Mo) dans le cache disque."""
    res = http.fetch(url, fotmob.NAME, http.FOREVER)
    http.invalidate(url)
    if not res.ok:
        return None, res.error
    return parse_match(res.text), None


def fill(max_pages=None, seconds=None):
    """Lit au plus `max_pages` pages de match (FM_PAGES_PER_RUN) en `seconds` secondes (FM_SECONDS). Ne lève jamais.
    -> {added, pages, left, errors, tracked, usable, good}."""
    rep = {"added": 0, "pages": 0, "left": 0, "errors": []}
    if not fotmob.enabled() or not C.FM_PAGES_PER_RUN:
        return rep | {"off": True}
    max_pages = C.FM_PAGES_PER_RUN if max_pages is None else max_pages
    deadline = time.monotonic() + (C.FM_SECONDS if seconds is None else seconds)
    d = load()
    try:
        todo = candidates(d)
        rep["left"] = len(todo)
        for sk, m in todo:
            if rep["pages"] >= max_pages or time.monotonic() > deadline or state.is_blocked(fotmob.NAME):
                break
            rep["pages"] += 1
            got, err = _read_page(m["url"])
            if err:
                rep["errors"].append(err)
                if len(rep["errors"]) >= 3:
                    break
                continue
            if got is None:                          # page sans statistiques : notée « vue » pour ne pas la relire
                d["seen"][m["id"]] = _d2i(m["d"] or str(_today()))
                continue
            got["id"] = got["id"] or m["id"]
            rep["added"] += record(d, got, sk)
        rep["left"] -= rep["pages"]
    except Exception as e:                            # noqa: BLE001 - facultatif
        rep["errors"].append(f"erreur interne ({type(e).__name__}: {e})")
    finally:
        save(d)
    return rep | summary(d)


def report_line(rep):
    if rep.get("off"):
        return "FotMob (stats de match) : désactivé"
    line = (f"FotMob (stats de match : touches, hors-jeu, fautes, tirs...) : {rep['added']} match(s) ajouté(s) en {rep['pages']} page(s), "
            f"{rep.get('left', 0)} en attente · {rep.get('tracked', 0)} équipes suivies, {rep.get('usable', 0)} utilisables, "
            f"{rep.get('good', 0)} bonnes")
    return line + (f" · erreurs : {'; '.join(rep['errors'][:2])}" if rep["errors"] else "")


# ---------- règlement ----------

def _same(a, b):
    return names.team_sim(a or "", b or "") >= 0.8


def _find_recent(d, b):
    for mid, r in d["recent"].items():
        if abs((datetime.fromisoformat(r["d"]) - datetime.fromisoformat(b["commence"][:10])).days) <= 1 \
                and _same(r["h"], b["home"]) and _same(r["a"], b["away"]):
            return r
    return None


def match_stats(b):
    """Totaux du match d'un pari (touches, hors-jeu, fautes, corners, tirs, cartons), au format de stats.fixture_stats (« side » : par
    équipe) ; None si FotMob ne l'a pas. Lit d'abord les matchs déjà accumulés, sinon télécharge la page du match (au plus
    FM_SETTLE_PAGES par passage, ligue mappée seulement)."""
    d = load()
    r = _find_recent(d, b)
    if r is None and _settle_pages["n"] < C.FM_SETTLE_PAGES:
        m = next((m for m in league_matches(b.get("sport_key", "")) if m["done"] and _same(m["home"], b["home"])
                  and _same(m["away"], b["away"]) and abs((datetime.fromisoformat(m["d"]) -
                                                            datetime.fromisoformat(b["commence"][:10])).days) <= 1), None)
        if m:
            _settle_pages["n"] += 1
            got, _ = _read_page(m["url"])
            if got:
                got["id"] = got["id"] or m["id"]
                record(d, got, b.get("sport_key", ""))
                save(d)
                r = d["recent"].get(got["id"])
    if not r:
        return None
    s = r["s"]
    ks = [k for k in s if k != "goals" and k not in FLOAT_KEYS]       # le xG ne sert à régler aucun pari
    out = {k: s[k][0] + s[k][1] for k in ks}
    out["side"] = {side: {k: s[k][i] for k in ks} for i, side in ((0, "home"), (1, "away"))}
    out["players"] = {}
    return out
