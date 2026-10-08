"""Sonde : récupère un petit échantillon réel de chaque source (sans crédit payant)
et l'enregistre dans probe/ pour valider les lecteurs hors ligne. Aucun secret écrit.

  python tools/probe.py            échantillons Understat (endpoint JSON)
  python tools/probe.py --full     + football-data, FBref, The Odds API (gratuit), API-Football, Telegram
  python tools/probe.py --sources  test de bout en bout de kelly.sources (refresh_all + amorçage) -> probe/sources_report.json"""
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kelly import envfile  # noqa: E402

envfile.load()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("PROBE_OUT") or os.path.join(ROOT, "probe")
UA = {"User-Agent": "KellyBot/1.0 (usage personnel)"}
SECRETS = [v for k, v in os.environ.items() if k in ("ODDS_API_KEY", "STATS_API_KEY", "TELEGRAM_TOKEN") and v]


def scrub(text):
    for s in SECRETS:
        text = text.replace(s, "***")
    return text


def save(name, text, limit=None):
    os.makedirs(OUT, exist_ok=True)
    text = scrub(text if limit is None else text[:limit])
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(text)
    print(f"✅ {name} ({len(text)} car.)")


def fetch(name, url, **kw):
    try:
        r = requests.get(url, timeout=30, **kw)
        meta = {"url": scrub(r.url), "status": r.status_code,
                "headers": {k: v for k, v in r.headers.items() if k.lower().startswith(("x-", "content-type", "server", "cf-"))}}
        save(name + ".meta.json", json.dumps(meta, indent=1))
        return r
    except requests.RequestException as e:
        save(name + ".meta.json", json.dumps({"error": scrub(str(e))}))
        print(f"❌ {name} : {scrub(str(e))}")
        return None


def probe_bookmaker():
    """1xBet : règles robots et flux public des cotes (aucune authentification)."""
    report = {}
    for host in ("1xbet.com", "1xbet.ci", "1xbet.ng"):
        r = fetch(f"bk_{host}_robots", f"https://{host}/robots.txt", headers=UA, allow_redirects=True)
        if r is not None:
            save(f"bk_{host}_robots.txt", r.text, limit=4000)
            report[host] = {"robots_status": r.status_code, "final_url": r.url}
        time.sleep(3)
    feeds = {
        "line_sports": "https://{h}/LineFeed/GetSportsShortZip?sports=1&lng=fr&country=1&partner=1&virtualSports=true",
        "line_champs": "https://{h}/LineFeed/GetChampsZip?sport=1&lng=fr&country=1&partner=1",
        "line_games": "https://{h}/LineFeed/Get1x2_VZip?sports=1&count=20&lng=fr&mode=4&country=1&partner=1&getEmpty=true",
    }
    for host in ("1xbet.com", "1xbet.ci"):
        for name, url in feeds.items():
            r = fetch(f"bk_{host}_{name}", url.format(h=host), headers=UA | {"Accept": "application/json"})
            if r is not None:
                save(f"bk_{host}_{name}.json", r.text, limit=5000)
                report.setdefault(host, {})[name] = {"status": r.status_code, "type": r.headers.get("Content-Type"),
                                                     "bytes": len(r.content)}
            time.sleep(3)
    save("bookmaker_report.json", json.dumps(report, indent=1))


def probe_apif_world():
    """API-Football gratuit : liste d'un jour passé, puis statistiques groupées par ids (2 requêtes)."""
    key = os.environ.get("STATS_API_KEY")
    if not key:
        return 0
    h = {"x-apisports-key": key}
    out = {}
    r = fetch("apif_day_past", "https://v3.football.api-sports.io/fixtures", headers=h,
              params={"date": "2026-10-04", "timezone": "UTC"})
    ids = []
    if r is not None:
        body = r.json() if r.ok else {}
        resp = body.get("response") or []
        fin = [f for f in resp if (f.get("fixture", {}).get("status") or {}).get("short") in ("FT", "AET", "PEN")]
        big = [f for f in fin if f.get("league", {}).get("country") in ("Brazil", "England", "Spain", "Argentina")]
        ids = [f["fixture"]["id"] for f in (big or fin)][:3]
        out["day_past"] = {"status": r.status_code, "errors": body.get("errors"), "n": len(resp), "finished": len(fin),
                           "sample": [(f["league"]["name"], f["teams"]["home"]["name"], f["teams"]["away"]["name"]) for f in (big or fin)[:3]]}
    if ids:
        r = fetch("apif_ids", "https://v3.football.api-sports.io/fixtures", headers=h, params={"ids": "-".join(map(str, ids))})
        if r is not None:
            body = r.json() if r.ok else {}
            resp = body.get("response") or []
            out["ids"] = {"status": r.status_code, "errors": body.get("errors"), "n": len(resp),
                          "with_statistics": sum(1 for f in resp if f.get("statistics")),
                          "stat_types": sorted({s["type"] for f in resp for t in (f.get("statistics") or []) for s in t.get("statistics", [])})[:20]}
    save("apif_world.json", json.dumps(out, indent=1, ensure_ascii=False))
    return 0


def main():
    present = {k: bool(os.environ.get(k)) for k in ("ODDS_API_KEY", "STATS_API_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")}
    save("secrets_presence.json", json.dumps(present, indent=1))     # noms seulement, jamais les valeurs

    # Understat : la page ne contient plus les données ; endpoints JSON utilisés par le site
    for name, url in (("understat_api_league", "https://understat.com/getLeagueData/EPL/2025"),):
        r = fetch(name, url, headers=UA | {"X-Requested-With": "XMLHttpRequest", "Referer": "https://understat.com/league/EPL"})
        if r is not None:
            save(name + ".json", r.text, limit=6000)
        time.sleep(3)
    r = fetch("understat_EPL_full", "https://understat.com/league/EPL", headers=UA)
    if r is not None and r.ok:
        save("understat_EPL.scripts.json", json.dumps(re.findall(r"<script[^>]*>(.{0,300})", r.text, re.S), indent=1))
        save("understat_EPL.js_srcs.json", json.dumps(re.findall(r'<script[^>]*src="([^"]+)"', r.text), indent=1))
    return 0


def _count(obj, *path):
    """Nombre d'éléments d'un dict imbriqué (0 si absent)."""
    for k in path:
        obj = obj.get(k) if isinstance(obj, dict) else None
    return len(obj) if isinstance(obj, (dict, list)) else 0


def main_sources():
    """Test de bout en bout EN CONDITIONS RÉELLES (GitHub Actions) du paquet kelly.sources : actualise les sources
    gratuites (football-data, Understat, FBref) et l'amorçage historique dans un dossier temporaire (aucune donnée
    du bot touchée, aucun secret lu), puis écrit probe/sources_report.json : lignes par source, nombre d'équipes /
    joueurs / arbitres, erreurs, tailles, un exemple de chiffres d'avant-match. Court par construction."""
    tmp = tempfile.mkdtemp(prefix="kelly-probe-")
    os.environ["DATA_DIR"], os.environ["SOURCES_CACHE_DIR"] = os.path.join(tmp, "data"), os.path.join(tmp, "cache")
    sys.path.insert(0, ROOT)
    from kelly import bootstrap, config, sources, store                      # import après DATA_DIR
    from kelly.sources import features
    store.DATA_DIR = os.environ["DATA_DIR"]
    t0 = time.time()
    leagues = sources.soccer_leagues(config.SPORTS)
    health = sources.refresh_all(leagues)
    boot = bootstrap.ensure(leagues, force=True)
    features.reload()
    cut = lambda x: (x or "")[:300]                                          # noqa: E731
    rep = {"when": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seconds": round(time.time() - t0, 1),
           "leagues": leagues,
           "sources": {n: {"ok": h["ok"], "rows": h["rows"], "error": cut(h["last_error"]), "blocked_until": h["blocked_until"]}
                       for n, h in health.items()}}
    prof = {}
    for fn, keys in (("src_footballdata.json", ("teams", "referees", "leagues")), ("src_understat.json", ("leagues",)),
                     ("src_fbref.json", ("leagues",))):
        data = store.load(fn, {})
        prof[fn] = {"bytes": os.path.getsize(store.path(fn)) if os.path.exists(store.path(fn)) else 0} | {
            k: _count(data, k) for k in keys} | ({"matches": data.get("matches")} if data.get("matches") else {})
    us = (store.load("src_understat.json", {}).get("leagues") or {})
    prof["src_understat.json"]["teams"] = sum(_count(d, "teams") for d in us.values())
    prof["src_understat.json"]["players"] = sum(sum(len(p) for p in (d.get("players") or {}).values()) for d in us.values())
    rep["profiles"] = prof
    rep["bootstrap"] = {"rows": len(boot.get("rows", [])), "seasons": boot.get("seasons"), "matches": boot.get("matches"),
                        "bytes": os.path.getsize(store.path("bootstrap.json")) if os.path.exists(store.path("bootstrap.json")) else 0}
    files = [os.path.join(d, f) for d, _, fs in os.walk(os.environ["SOURCES_CACHE_DIR"]) for f in fs]
    rep["cache"] = {"files": len(files), "bytes": sum(os.path.getsize(f) for f in files)}
    teams = sorted(t for t, v in (store.load("src_footballdata.json", {}).get("teams") or {}).items() if v.get("div") == "E0")
    if len(teams) >= 2:
        f = features.match_features(teams[0], teams[1], "soccer_epl")
        rep["sample_match"] = {"match": f"{teams[0]} - {teams[1]}", "sources": f["sources"], "fields": len(f),
                               **{k: f[k] for k in ("exp_corners", "exp_cards", "exp_fouls", "exp_shots_home", "exp_xg_total") if k in f}}
        rep["sample_team"] = sorted((features.team_profile(teams[0]) or {}).keys())
    first = next(((t, next(iter(ps))) for d in us.values() for t, ps in (d.get("players") or {}).items() if ps), None)
    if first:
        pf = features.player_features(first[1], first[0])
        rep["sample_player"] = sorted(pf) if pf else None
    save("sources_report.json", json.dumps(rep, ensure_ascii=False, indent=1))
    print(json.dumps({k: rep[k] for k in ("seconds", "sources", "bootstrap")}, ensure_ascii=False))
    return 0


def main_full():
    # football-data.co.uk : CSV de la saison en cours (Premier League) et d'une saison passée
    for season in ("2526", "2425"):
        r = fetch(f"fd_E0_{season}", f"https://www.football-data.co.uk/mmz4281/{season}/E0.csv", headers=UA)
        if r is not None and r.ok:
            save(f"fd_E0_{season}.csv", "\n".join(r.content.decode("latin-1").splitlines()[:25]))
        time.sleep(3)

    # Understat : page ligue (variables JSON.parse embarquées)
    r = fetch("understat_EPL", "https://understat.com/league/EPL", headers=UA)
    if r is not None and r.ok:
        vars_ = {m.group(1): m.group(2)[:3000] for m in re.finditer(r"var\s+(\w+)\s*=\s*JSON\.parse\('(.*?)'\)", r.text, re.S)}
        save("understat_EPL.vars.json", json.dumps(vars_, indent=1))
        save("understat_EPL.head.html", r.text, limit=4000)
    time.sleep(3)

    # FBref : stats de tir des équipes de Premier League (tables parfois dans des commentaires HTML)
    r = fetch("fbref_epl_shooting", "https://fbref.com/en/comps/9/shooting/Premier-League-Stats", headers=UA)
    if r is not None:
        html = r.text
        tables = re.findall(r'<table[^>]*id="([^"]+)"', html)
        i = html.find("<table")
        save("fbref_epl_shooting.tables.json", json.dumps({"status": r.status_code, "table_ids": tables}, indent=1))
        save("fbref_epl_shooting.sample.html", html[i:i + 20000] if i >= 0 else html[:6000])
    time.sleep(6)
    r = fetch("fbref_epl_misc", "https://fbref.com/en/comps/9/misc/Premier-League-Stats", headers=UA)
    if r is not None:
        html = r.text
        save("fbref_epl_misc.tables.json", json.dumps({"status": r.status_code,
                                                       "table_ids": re.findall(r'<table[^>]*id="([^"]+)"', html)}, indent=1))
        i = html.find('id="stats_misc"')
        save("fbref_epl_misc.sample.html", html[max(0, i - 500):i + 20000] if i >= 0 else html[:6000])

    # The Odds API : uniquement les endpoints gratuits
    key = os.environ.get("ODDS_API_KEY")
    if key:
        r = fetch("odds_sports", "https://api.the-odds-api.com/v4/sports", params={"apiKey": key})
        if r is not None and r.ok:
            save("odds_sports.json", json.dumps([s for s in r.json() if s["key"].startswith("soccer_")][:40], indent=1))
        r = fetch("odds_events_epl", "https://api.the-odds-api.com/v4/sports/soccer_epl/events", params={"apiKey": key})
        if r is not None:
            save("odds_events_epl.json", r.text, limit=6000)

    # API-Football : statut du compte + matchs d'une date passée (1 requête chacun)
    skey = os.environ.get("STATS_API_KEY")
    if skey:
        h = {"x-apisports-key": skey}
        r = fetch("apif_status", "https://v3.football.api-sports.io/status", headers=h)
        if r is not None:
            save("apif_status.json", r.text, limit=4000)
        r = fetch("apif_fixtures", "https://v3.football.api-sports.io/fixtures",
                  headers=h, params={"date": "2026-10-04", "league": 39, "season": 2026, "timezone": "UTC"})
        if r is not None:
            save("apif_fixtures.json", r.text, limit=8000)

    # Telegram : identité du bot (sans envoyer de message)
    tok = os.environ.get("TELEGRAM_TOKEN")
    if tok:
        r = fetch("telegram_getme", f"https://api.telegram.org/bot{tok}/getMe")
        if r is not None:
            save("telegram_getme.json", r.text, limit=2000)
    return 0


if __name__ == "__main__":
    if "--apif-world" in sys.argv:
        sys.exit(probe_apif_world())
    if "--bookmaker" in sys.argv:
        sys.exit(probe_bookmaker())
    sys.exit(main_full() if "--full" in sys.argv else main_sources() if "--sources" in sys.argv else main())
