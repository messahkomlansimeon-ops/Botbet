import os
import re
from html.parser import HTMLParser

from .. import store
from . import http, state

NAME = "fbref"
LABEL = "FBref"
BASE = "https://fbref.com/en/comps"
PROFILE_FILE = "src_fbref.json"
MIN_90S = 3.0
KINDS = tuple(k for k in re.split(r"[,\s]+", os.environ.get("FBREF_KINDS") or "shooting,misc") if k)

SPORT_TO_COMP = {
    "soccer_epl": (9, "Premier-League"), "soccer_spain_la_liga": (12, "La-Liga"),
    "soccer_italy_serie_a": (11, "Serie-A"), "soccer_germany_bundesliga": (20, "Bundesliga"),
    "soccer_france_ligue_one": (13, "Ligue-1"), "soccer_netherlands_eredivisie": (23, "Eredivisie"),
    "soccer_portugal_primeira_liga": (32, "Primeira-Liga"), "soccer_efl_champ": (10, "Championship"),
    "soccer_uefa_champs_league": (8, "Champions-League"),
}
EXPECTED = {"shooting": ("shots", "shots_on_target"), "misc": ("fouls", "cards_yellow"),
            "passing_types": ("corner_kicks",)}


def enabled():
    return (os.environ.get("FBREF_ENABLED") or "1").strip().lower() not in ("0", "false", "non", "no")


def max_pages():
    try:
        return int(os.environ.get("FBREF_MAX_PAGES") or 6)
    except ValueError:
        return 6


def url(comp, kind):
    cid, slug = comp
    return f"{BASE}/{cid}/{kind}/{slug}-Stats"


class _Tables(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = {}
        self._tid, self._rows, self._row, self._skip = None, None, None, False
        self._head, self._stat, self._txt, self._pid, self._n = False, None, [], None, 0

    def handle_comment(self, data):
        if "<table" in data:
            sub = _Tables()
            sub.feed(data)
            sub.close()
            for k, v in sub.tables.items():
                self.tables.setdefault(k, v)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._n += 1
            self._tid, self._rows = a.get("id") or f"table{self._n}", []
        elif self._rows is None:
            return
        elif tag in ("thead", "tfoot"):
            self._head = True
        elif tag == "tbody":
            self._head = False
        elif tag == "tr":
            cls = a.get("class") or ""
            self._row, self._skip = {}, self._head or "thead" in cls or "spacer" in cls
        elif tag in ("th", "td") and self._row is not None and a.get("data-stat"):
            self._stat, self._txt, self._pid = a["data-stat"], [], a.get("data-append-csv")

    def handle_data(self, data):
        if self._stat is not None:
            self._txt.append(data)

    def handle_endtag(self, tag):
        if tag in ("th", "td") and self._stat is not None:
            self._row[self._stat] = "".join(self._txt).strip()
            if self._pid:
                self._row["_id"] = self._pid
            self._stat = None
        elif tag == "tr" and self._row is not None:
            if self._row and not self._skip:
                self._rows.append(self._row)
            self._row = None
        elif tag in ("thead", "tfoot"):
            self._head = False
        elif tag == "table" and self._rows is not None:
            if self._rows:
                self.tables.setdefault(self._tid, self._rows)
            self._tid, self._rows = None, None


def parse_tables(html):
    p = _Tables()
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        pass
    return p.tables


def num(v):
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _team(row):
    t = row.get("team") or row.get("squad") or ""
    return re.sub(r"^vs\s+", "", t).strip()


def _tables_of(tables, kind, squads):
    if squads:
        return {side: tables.get(f"stats_squads_{kind}_{side}", []) for side in ("for", "against")}
    return tables.get(f"stats_{kind}", [])


def player_rows(tables, kind):
    return [r for r in _tables_of(tables, kind, False) if r.get("player")]


def _per(total, base):
    return round(total / base, 2) if total is not None and base else None


def build_players(pages):
    merged = {}
    for kind, tables in pages.items():
        for r in player_rows(tables, kind):
            team, name = _team(r), r["player"]
            m90 = num(r.get("minutes_90s"))
            if not team or not m90 or m90 < MIN_90S:
                continue
            rec = merged.setdefault((team, name), {"min90": round(m90, 1)})
            if kind == "shooting":
                rec["shots90"] = _per(num(r.get("shots")), m90)
                rec["sot90"] = _per(num(r.get("shots_on_target")), m90)
            elif kind == "misc":
                rec["fouls90"] = _per(num(r.get("fouls")), m90)
                rec["fouled90"] = _per(num(r.get("fouled")), m90)
                y, red, yr = num(r.get("cards_yellow")), num(r.get("cards_red")), num(r.get("cards_yellow_red"))
                if y is not None:
                    rec["yc90"] = _per(y, m90)
                    rec["cards90"] = _per(y + (red or 0), m90)
    out = {}
    for (team, name), rec in merged.items():
        out.setdefault(team, {})[name] = {k: v for k, v in rec.items() if v is not None}
    return out


_SQUAD_STATS = {"shooting": (("shots", "shots"), ("sot", "shots_on_target")),
                "misc": (("fouls", "fouls"), ("fouled", "fouled"), ("yellow", "cards_yellow"), ("red", "cards_red")),
                "passing_types": (("corners", "corner_kicks"),)}


def build_squads(pages):
    acc = {}
    for kind, tables in pages.items():
        for side, rows in _tables_of(tables, kind, True).items():
            for r in rows:
                team = _team(r)
                games = num(r.get("minutes_90s")) or num(r.get("games"))
                if not team or not games:
                    continue
                d = acc.setdefault(team, {"games": round(games, 1)})
                rec = d.setdefault(side, {})
                for key, col in _SQUAD_STATS.get(kind, ()):
                    v = _per(num(r.get(col)), games)
                    if v is not None:
                        rec[key + "_pm"] = v
                if kind == "misc":
                    y, red = num(r.get("cards_yellow")), num(r.get("cards_red"))
                    if y is not None:
                        rec["cards_pm"] = _per(y + (red or 0), games)
    return acc


def check_structure(kind, tables):
    exp = EXPECTED.get(kind, ())
    if not tables:
        return [f"structure de la page modifiée : aucun tableau trouvé ({kind})"]
    rows = [r for t in (f"stats_{kind}", f"stats_squads_{kind}_for") for r in tables.get(t, [])]
    if not rows:
        return [f"structure de la page modifiée : tableaux stats_{kind} / stats_squads_{kind}_for absents"]
    seen = set().union(*(r.keys() for r in rows))
    miss = [c for c in exp if c not in seen]
    return [f"structure de la page modifiée : colonne(s) {', '.join(miss)} absente(s) ({kind})"] if miss else []


def fetch_page(comp, kind):
    u = url(comp, kind)
    res = http.fetch(u, NAME, http.TTL_FBREF)
    tag = f"{comp[1]} {kind}"
    if not res.ok:
        return None, [f"{tag} : {res.error}"], True
    tables = parse_tables(res.text)
    probs = check_structure(kind, tables)
    if probs:
        http.invalidate(u)
        return None, [f"{tag} : {p}" for p in probs], not res.from_cache
    errs = [f"{tag} : {res.error} (données en cache)"] if res.error else []
    return tables, errs, not res.from_cache


def comps_for(sport_keys=None):
    keys = list(SPORT_TO_COMP) if sport_keys is None else [k for k in sport_keys if k in SPORT_TO_COMP]
    return list(dict.fromkeys(keys))


def refresh(sport_keys=None):
    try:
        keys = comps_for(sport_keys)
        if not keys or not enabled():
            return health()
        prof = store.load(PROFILE_FILE, {})
        prof = prof if isinstance(prof, dict) else {}
        prof.setdefault("leagues", {})
        errors, rows, budget = [], 0, max_pages()
        for key in keys:
            if state.is_blocked(NAME):
                break
            comp, pages = SPORT_TO_COMP[key], {}
            for kind in KINDS:
                if state.is_blocked(NAME):
                    break
                if budget <= 0 and not http.is_cached(url(comp, kind), http.TTL_FBREF):
                    continue
                tables, errs, used = fetch_page(comp, kind)
                budget -= 1 if used else 0
                errors += errs
                if tables is not None:
                    pages[kind] = tables
            if pages:
                players, squads = build_players(pages), build_squads(pages)
                if players or squads:
                    prof["leagues"][key] = {"comp": comp[0], "squads": squads, "players": players,
                                            "kinds": sorted(pages)}
                    rows += len(squads) + sum(len(p) for p in players.values())
        prof["leagues"] = {k: v for k, v in prof["leagues"].items() if k in SPORT_TO_COMP}
        if rows:
            prof["updated"] = state.now().isoformat(timespec="minutes")
            if state.save_profile(PROFILE_FILE, prof) > state.MAX_PROFILE_BYTES:
                errors.append("profil trop volumineux (réduire les ligues suivies)")
        return state.record_run(NAME, rows, errors)
    except Exception as e:
        return state.record_run(NAME, 0, [f"erreur interne ({type(e).__name__}: {e})"])


def health():
    return state.health(NAME)


def profiles():
    p = store.load(PROFILE_FILE, {})
    return p if isinstance(p, dict) else {}
