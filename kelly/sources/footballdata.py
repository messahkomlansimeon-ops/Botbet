import csv
import io
import re
from datetime import date, timedelta

from .. import store
from . import http, names, state

NAME = "footballdata"
LABEL = "football-data"
BASE = "https://football-data.co.uk/mmz4281"
PROFILE_FILE = "src_footballdata.json"
DEFAULT_DIVS = ("E0", "SP1", "I1", "D1", "F1")

SPORT_TO_DIV = {
    "soccer_epl": "E0", "soccer_efl_champ": "E1", "soccer_england_league1": "E2", "soccer_england_league2": "E3",
    "soccer_spain_la_liga": "SP1", "soccer_spain_segunda_division": "SP2",
    "soccer_italy_serie_a": "I1", "soccer_italy_serie_b": "I2",
    "soccer_germany_bundesliga": "D1", "soccer_germany_bundesliga2": "D2",
    "soccer_france_ligue_one": "F1", "soccer_france_ligue_two": "F2",
    "soccer_netherlands_eredivisie": "N1", "soccer_portugal_primeira_liga": "P1",
    "soccer_belgium_first_div": "B1", "soccer_turkey_super_league": "T1",
    "soccer_spl": "SC0", "soccer_greece_super_league": "G1",
}
NEW_BASE = "https://football-data.co.uk/new"
SPORT_TO_NEW = {
    "soccer_brazil_campeonato": "BRA", "soccer_argentina_primera_division": "ARG", "soccer_usa_mls": "USA",
    "soccer_mexico_ligamx": "MEX", "soccer_japan_j_league": "JPN", "soccer_china_superleague": "CHN",
    "soccer_denmark_superliga": "DNK", "soccer_norway_eliteserien": "NOR", "soccer_sweden_allsvenskan": "SWE",
    "soccer_poland_ekstraklasa": "POL", "soccer_austria_bundesliga": "AUT", "soccer_switzerland_superleague": "SWZ",
    "soccer_league_of_ireland": "IRL", "soccer_finland_veikkausliiga": "FIN",
}
NEW_DIVS = frozenset(SPORT_TO_NEW.values())
NEW_KEEP_DAYS = 400
SPORT_TO_DIV = SPORT_TO_DIV | SPORT_TO_NEW
DIV_TO_SPORT = {v: k for k, v in SPORT_TO_DIV.items()}
SLIM_KEYS = ("goals", "corners", "cards")

ALIASES = {"Home": "HomeTeam", "Away": "AwayTeam", "HG": "FTHG", "AG": "FTAG", "Res": "FTR"}
REQUIRED = ("Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG")
STAT_COLS = {"hs": "HS", "as": "AS", "hst": "HST", "ast": "AST", "hf": "HF", "af": "AF", "hc": "HC", "ac": "AC",
             "hy": "HY", "ay": "AY", "hr": "HR", "ar": "AR"}
STATS_EXPECTED = ("HS", "HC", "HF", "HY")


def _odds_columns():
    cols = {}
    for book, p in (("avg", "Avg"), ("max", "Max"), ("ps", "PS"), ("b365", "B365")):
        cols[("1x2", "open", book)] = (f"{p}H", f"{p}D", f"{p}A")
        cols[("1x2", "close", book)] = (f"{p}CH", f"{p}CD", f"{p}CA")
    for book, o, c in (("avg", "Avg", "AvgC"), ("max", "Max", "MaxC"), ("ps", "P", "PC"), ("b365", "B365", "B365C")):
        cols[("ou25", "open", book)] = (f"{o}>2.5", f"{o}<2.5")
        cols[("ou25", "close", book)] = (f"{c}>2.5", f"{c}<2.5")
        cols[("ah", "open", book)] = (f"{o}AHH", f"{o}AHA")
        cols[("ah", "close", book)] = (f"{c}AHH", f"{c}AHA")
    return cols


ODDS_COLUMNS = _odds_columns()
AH_LINE = {"open": "AHh", "close": "AHCh"}


def current_season(today=None):
    d = today or state.now().date()
    y = d.year if d.month >= 7 else d.year - 1
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


def previous_season(code):
    a = (int(code[:2]) - 1) % 100
    return f"{a:02d}{int(code[:2]):02d}"


def default_seasons(today=None):
    cur = current_season(today)
    return [cur, previous_season(cur)]


def url(div, season):
    return f"{NEW_BASE}/{div}.csv" if div in NEW_DIVS else f"{BASE}/{season}/{div}.csv"


def divs_for(sport_keys=None):
    if sport_keys is None:
        return list(DEFAULT_DIVS)
    return list(dict.fromkeys(SPORT_TO_DIV[k] for k in sport_keys if k in SPORT_TO_DIV))


def _f(v):
    try:
        x = float(str(v).strip().replace(",", "."))
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _i(v):
    x = _f(v)
    return int(x) if x is not None else None


def parse_date(s):
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})", s)
    if m:
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        if y < 100:
            y += 2000 if y < 70 else 1900
    else:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
        if not m:
            return None
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _odds(row):
    out = {}
    for (mkt, phase, book), cols in ODDS_COLUMNS.items():
        vals = [_f(row.get(c)) for c in cols]
        if all(v is not None and v > 1.0 for v in vals):
            out.setdefault(mkt, {}).setdefault(phase, {})[book] = vals
    for phase, col in AH_LINE.items():
        line = _f(row.get(col))
        if line is not None and phase in out.get("ah", {}):
            out["ah"][phase]["line"] = line
    return out


def parse_csv(text, div=None, season=None):
    problems = []
    text = (text or "").lstrip("\ufeff")
    if text.startswith("\u00ef\u00bb\u00bf"):
        text = text[3:]
    if not text.strip():
        return [], ["fichier vide"]
    if text.lstrip()[:1] == "<":
        return [], ["structure du fichier modifiée : page HTML reçue au lieu d'un CSV"]
    reader = csv.DictReader(io.StringIO(text))
    names = [(h or "").strip() for h in (reader.fieldnames or [])]
    reader.fieldnames = [ALIASES.get(h, h) for h in names]
    miss = [c for c in REQUIRED if c not in reader.fieldnames]
    if miss:
        return [], [f"structure du CSV modifiée : colonnes manquantes {', '.join(miss)}"]
    absent = [c for c in STATS_EXPECTED if c not in reader.fieldnames]
    if absent:
        problems.append(f"colonnes de stats absentes ({', '.join(absent)})")
    out, bad = [], 0
    for row in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items() if isinstance(k, str)}
        if not any(row.values()):
            continue
        d, home, away = parse_date(row.get("Date")), row.get("HomeTeam"), row.get("AwayTeam")
        hg, ag = _i(row.get("FTHG")), _i(row.get("FTAG"))
        if not home and not away and d is None:
            continue
        if not d or not home or not away:
            bad += 1
            continue
        if hg is None or ag is None:
            continue
        m = {"date": d, "div": row.get("Div") or div, "season": season, "home": home, "away": away,
             "hg": hg, "ag": ag, "ftr": row.get("FTR") or ("H" if hg > ag else "A" if hg < ag else "D"),
             "referee": row.get("Referee") or None}
        for k, col in STAT_COLS.items():
            m[k] = _i(row.get(col))
        m["odds"] = _odds(row)
        out.append(m)
    if bad and bad * 10 > len(out) + bad:
        problems.append(f"{bad} ligne(s) ignorée(s) (date ou équipe illisible)")
    return out, problems


def no_vig(odds):
    inv = [1 / o for o in odds if o and o > 1]
    s = sum(inv)
    return [x / s for x in inv] if s and len(inv) == len(odds) else None


def fetch_season(div, season):
    u = url(div, season)
    ttl = http.TTL_CSV_CURRENT if season == current_season() or div in NEW_DIVS else http.FOREVER
    res = http.fetch(u, NAME, ttl)
    tag = div if div in NEW_DIVS else f"{div} {season}"
    if not res.ok:
        return [], [f"{tag} : {res.error}"]
    rows, probs = parse_csv(res.text, div, None if div in NEW_DIVS else season)
    if not rows:
        http.invalidate(u)
        probs = probs or ["aucune ligne exploitable"]
        return [], [f"{tag} : {p}" for p in probs]
    strict = season == current_season() and div in DEFAULT_DIVS
    errs = [f"{tag} : {p}" for p in probs if strict or not p.startswith("colonnes de stats absentes")]
    if res.error:
        errs.append(f"{tag} : {res.error} (données en cache)")
    return rows, errs


def _recent(rows):
    cut = (state.now().date() - timedelta(days=NEW_KEEP_DAYS)).isoformat()
    return [m for m in rows if m["date"] >= cut]


def _load(divs, seasons):
    matches, errors = [], []
    seasons = [str(s) for s in (seasons or default_seasons())]
    for div in divs:
        got = 0
        if div in NEW_DIVS:
            rows, errs = fetch_season(div, seasons[0])
            matches += _recent(rows)
            errors += errs
            continue
        for s in seasons:
            rows, errs = fetch_season(div, s)
            got += len(rows)
            if not rows and s == current_season() and any("HTTP 404" in e for e in errs) and len(seasons) > 1:
                errs = []
            matches += rows
            errors += errs
    matches.sort(key=lambda m: (m["date"], m["home"]))
    return matches, errors


def load_history(divs=None, seasons=None):
    try:
        return _load(list(divs or DEFAULT_DIVS), seasons)[0]
    except Exception:
        return []


def cached_history(divs=None, seasons=None):
    out = []
    for div in divs or DEFAULT_DIVS:
        if div in NEW_DIVS:
            res = http.cached(url(div, ""))
            if res:
                out += _recent(parse_csv(res.text, div)[0])
            continue
        for s in seasons or default_seasons():
            res = http.cached(url(div, str(s)))
            if res:
                out += parse_csv(res.text, div, str(s))[0]
    return out


def find_match(matches, home, away, day):
    try:
        d = date.fromisoformat(str(day)[:10])
    except ValueError:
        return None
    wanted = {(d + timedelta(days=k)).isoformat(): abs(k) for k in (-1, 0, 1)}
    best, best_key = None, None
    for m in matches:
        gap = wanted.get(m["date"])
        if gap is None:
            continue
        sh, sa = names.team_sim(m["home"], home), names.team_sim(m["away"], away)
        if sh < 0.8 or sa < 0.8 or sh + sa < 1.75:
            continue
        key = (round(sh + sa, 2), -gap)
        if best_key is None or key > best_key:
            best, best_key = m, key
    return best


def match_stats(m):
    out = {"players": {}}
    pairs = {"corners": ("hc", "ac"), "shots": ("hs", "as"), "sot": ("hst", "ast"), "fouls": ("hf", "af")}
    for k, (h, a) in pairs.items():
        if m.get(h) is not None and m.get(a) is not None:
            out[k] = m[h] + m[a]
    side = {s: {k: m[c] for k, c in (("shots", p + "s"), ("sot", p + "st")) if m.get(c) is not None}
            for s, p in (("home", "h"), ("away", "a"))}
    if side["home"] or side["away"]:
        out["side"] = side
    if m.get("hy") is not None and m.get("ay") is not None:
        out["cards"] = m["hy"] + m["ay"] + (m.get("hr") or 0) + (m.get("ar") or 0)
    return out


def _cards(y, r):
    return None if y is None else y + (r or 0)


def _side(m, side):
    me, op = ("h", "a") if side == "home" else ("a", "h")
    g = lambda k: m.get(k)
    pair = {
        "goals": (g("hg" if me == "h" else "ag"), g("hg" if op == "h" else "ag")),
        "shots": (g(me + "s"), g(op + "s")), "sot": (g(me + "st"), g(op + "st")),
        "corners": (g(me + "c"), g(op + "c")), "fouls": (g(me + "f"), g(op + "f")),
        "yellow": (g(me + "y"), g(op + "y")),
        "cards": (_cards(g(me + "y"), g(me + "r")), _cards(g(op + "y"), g(op + "r"))),
    }
    return pair


def _avg_view(recs, keys=("goals", "shots", "sot", "corners", "fouls", "cards", "yellow"), min_count=1):
    """Moyennes pour / contre sur les matchs `recs` ; une stat relevée sur moins de `min_count` matchs n'est pas donnée (la qualité du
    profil se juge sur n, le nombre de matchs : une moyenne de touches sur 1 ou 2 matchs sur 10 la tromperait)."""
    out = {"n": len(recs)}
    for k in keys:
        f = [r[k][0] for r in recs if r[k][0] is not None]
        a = [r[k][1] for r in recs if r[k][1] is not None]
        if f and len(f) >= min_count:              # jamais de moyenne sans relevé (une équipe qui n'a joué qu'à domicile)
            out[k + "_for"] = round(sum(f) / len(f), 2)
        if a and len(a) >= min_count:
            out[k + "_against"] = round(sum(a) / len(a), 2)
    return out


def team_profiles(matches, last_n=10, max_age_days=200):
    ms = sorted(matches, key=lambda m: m["date"])
    per = {}
    for m in ms:
        for team, side in ((m["home"], "home"), (m["away"], "away")):
            per.setdefault(team, []).append((m["date"], side, m.get("div"), _side(m, side)))
    latest = ms[-1]["date"] if ms else None
    teams = {}
    for team, recs in per.items():
        if latest and (date.fromisoformat(latest) - date.fromisoformat(recs[-1][0])).days > max_age_days:
            continue
        div = recs[-1][2]
        kw = {} if div in DEFAULT_DIVS or div is None else {"keys": SLIM_KEYS}
        teams[team] = {"div": div, "last": recs[-1][0],
                       "all": _avg_view([r[3] for r in recs[-last_n:]], **kw),
                       "home": _avg_view([r[3] for r in recs if r[1] == "home"][-last_n:], **kw),
                       "away": _avg_view([r[3] for r in recs if r[1] == "away"][-last_n:], **kw)}
    refs = {}
    for m in ms:
        if not m.get("referee"):
            continue
        cards = None if m.get("hy") is None or m.get("ay") is None else (m["hy"] + m["ay"] + (m.get("hr") or 0) + (m.get("ar") or 0))
        fouls = None if m.get("hf") is None or m.get("af") is None else m["hf"] + m["af"]
        refs.setdefault(m["referee"], []).append((cards, fouls))
    referees = {}
    for r, recs in refs.items():
        recs = recs[-40:]
        c = [x[0] for x in recs if x[0] is not None]
        f = [x[1] for x in recs if x[1] is not None]
        if c or f:
            referees[r] = {"n": len(recs)} | ({"cards": round(sum(c) / len(c), 2)} if c else {}) | (
                {"fouls": round(sum(f) / len(f), 2)} if f else {})
    leagues = {}
    for div in {m.get("div") for m in ms if m.get("div")}:
        sub = [m for m in ms if m.get("div") == div][-300:]
        tot = [(_side(m, "home"), _side(m, "away")) for m in sub]
        lg = {"n": len(sub)}
        for k in ("goals", "shots", "corners", "fouls", "cards"):
            v = [h[k][0] + a[k][0] for h, a in tot if h[k][0] is not None and a[k][0] is not None]
            if v:
                lg[k] = round(sum(v) / len(v), 2)
        leagues[div] = lg
    return {"teams": teams, "referees": referees, "leagues": leagues}


def refresh(sport_keys=None, seasons=None):
    try:
        divs = divs_for(sport_keys)
        if not divs:
            return health()
        matches, errors = _load(divs, seasons)
        if matches:
            prof = team_profiles(matches)
            prof["updated"] = state.now().isoformat(timespec="minutes")
            prof["matches"] = len(matches)
            if state.save_profile(PROFILE_FILE, prof) > state.MAX_PROFILE_BYTES:
                errors.append("profil trop volumineux (réduire les divisions suivies)")
        elif not errors:
            errors.append("structure du site modifiée : aucun match trouvé")
        return state.record_run(NAME, len(matches), errors)
    except Exception as e:
        return state.record_run(NAME, 0, [f"erreur interne ({type(e).__name__}: {e})"])


def health():
    return state.health(NAME)


def profiles():
    p = store.load(PROFILE_FILE, {})
    return p if isinstance(p, dict) else {}
