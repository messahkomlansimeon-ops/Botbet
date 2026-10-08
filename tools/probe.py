"""Sonde : récupère un petit échantillon réel de chaque source (sans crédit payant)
et l'enregistre dans probe/ pour valider les lecteurs hors ligne. Aucun secret écrit."""
import json
import os
import re
import sys
import time

import requests

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "probe")
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


def main():
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
    sys.exit(main())
