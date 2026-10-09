"""Code coupon 1xBet d'un combiné Kelly (test autorisé par l'utilisateur : règle Bash(python tools/xbet_coupon.py:*)).

Kelly ne place JAMAIS de pari : ce script enregistre seulement un ticket SANS mise chez 1xBet (/service-api/LiveBet/Open/SaveCoupon,
sans connexion), via le proxy de Kelly, et affiche le code à charger dans l'appli 1xBet (« Charger le coupon »).

  python tools/xbet_coupon.py --format <fichier.js>   noms des champs du ticket lus dans le script du site (déjà téléchargé)
  python tools/xbet_coupon.py --test                  2 paris 💎 du jour (marché principal) : SaveCoupon (Summ 0), réponse brute,
                                                      code extrait, puis relecture GetCoupon — l'utilisateur vérifie dans l'appli.
"""
import json
import math
import os
import re
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAVE_URL = "https://1xbet.ci/service-api/LiveBet/Open/SaveCoupon"
GET_URL = "https://1xbet.ci/service-api/LiveBet/Open/GetCoupon"
MAIN = ("totals", "btts", "double_chance", "h2h")


def show_format(path):
    """Paramètres de SaveCoupon / GetCoupon dans le script du site : définition de chaque variable (nom réel du champ JSON)."""
    t = open(path, encoding="utf-8").read()
    for ep in ("Open/SaveCoupon", "Open/GetCoupon"):
        m = re.search(r'url:"/service-api/LiveBet/' + re.escape(ep) + r'",method:"POST",expectedParams:\{([^}]*)\}', t)
        print(f"== {ep} :", m.group(1) if m else "introuvable")
        if not m:
            continue
        for v in sorted(set(re.findall(r":([A-Za-z_$][\w$]*)", m.group(1))) | set(re.findall(r"\.\.\.([A-Za-z_$][\w$]*)\(", m.group(1)))):
            d = re.search(r"[,;\s]" + re.escape(v) + r"=([^;]{0,220})", t)
            print(f"   {v} = {d.group(1)[:220] if d else '?'}")


def pick_test_legs(n=2):
    """Jusqu'à `n` paris 💎 à venir du jour Kelly, marché principal seulement (buts, double chance, les 2 marquent, 1X2),
    un par match : la cote est relue chez 1xBet et la référence [GameId, Type, Param] prise fraîche."""
    from kelly import store, xprice
    from kelly.sources import xbet
    today = store.load("today.json", {})
    now = datetime.now(timezone.utc)
    min_time = now + timedelta(minutes=60)
    cand = [c for c in today.get("pronos", []) if c.get("xodds") and c["market"] in MAIN and c.get("xgid")
            and datetime.fromisoformat(c["commence"].replace("Z", "+00:00")) > min_time]
    cand.sort(key=lambda c: c["commence"])
    out, seen = [], set()
    for c in cand:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        out.append(c)
        if len(out) == n:
            break
    if not out:
        return []
    events = [(c["sport_key"], {"id": c["id"], "home_team": c["home"], "away_team": c["away"], "commence_time": c["commence"],
                                "xgid": c["xgid"]}) for c in out]
    found = xbet.fetch_day(events, wanted={c["id"]: {c["market"]} for c in out})
    legs = []
    for c in out:
        m = found.get(c["id"])
        if not m or not m.get("refs"):
            print(f"! {c['home']} - {c['away']} : cotes non relues", file=sys.stderr)
            continue
        c2 = dict(c)
        r = xprice.price(c2, m["px"], m["refs"])
        if r and c2.get("xref") and (c2["market"], c2["name"], c2["point"]) == (c["market"], c["name"], c["point"]):
            legs.append(c2)
        else:
            print(f"! {c['home']} - {c['away']} : pari absent ou ligne changée chez 1xBet ({r})", file=sys.stderr)
    return legs


def event1(c):
    """Élément de Events d'un pari du ticket : référence 1xBet (GameId, Type, Param) + cote.
    Kind=3 (LINE/avant-match, Ot.LINE=3 dans le client v3 ; Kind=1 correspond à LIVE)."""
    gid, t, p = c["xref"]
    return {"GameId": gid, "Type": int(t), "Coef": c["xodds"], "Param": 0 if p is None else float(p),
            "PV": None, "PlayerId": 0, "Kind": 3, "InstrumentId": 0, "Seconds": 0, "Price": 0, "Expired": 0,
            "PlayersDuel": None}


def run_test():
    from kelly.sources import http, xbet
    legs = pick_test_legs()
    if not legs:
        print("aucun paris 💎 à venir du jour (marché principal) : rien à tester")
        return
    for c in legs:
        print(f"- {c['home']} - {c['away']} : {c['pick']} @ {c['xodds']}  xref={c['xref']}")

    # Corps plat exact construit par le client v3
    body = {
        "Events": [event1(c) for c in legs],
        "Summ": 0,
        "Vid": 1,
        "Kind": 3,
        "partner": 286,
        "notWait": True,
        "CheckCf": 1,
    }
    ses = xbet._Session()
    if ses.why:
        print(f"1xBet indisponible : {ses.why}")
        return
    try:
        r = ses.call(http.post, SAVE_URL, body)
        print(f"SaveCoupon -> HTTP {r.status}")
        print(json.dumps(json.loads(r.content or b"{}"), ensure_ascii=False, indent=2) if r.ok else (r.error or "réponse vide"))
        try:
            resp = json.loads(r.content)
        except ValueError:
            return
        code = resp.get("Value") if isinstance(resp.get("Value"), str) else (resp.get("Value") or {}).get("Guid")
        if not code:
            print("pas de code dans la réponse")
            return
        print(f"CODE : {code}")
        g = ses.call(http.post, GET_URL, {"Guid": code, "Lng": "fr", "partner": 286})
        print(f"GetCoupon -> HTTP {g.status}")
        print(json.dumps(json.loads(g.content or b"{}"), ensure_ascii=False, indent=2) if g.ok else (g.error or "réponse vide"))
    except Exception as e:                          # noqa: BLE001 - un test ne casse jamais rien
        print(f"échec : {type(e).__name__}: {e}")
    finally:
        ses.close("test du coupon")


def read_coupon(code):
    """Relit un ticket déjà enregistré (GetCoupon, lecture seule : rien n'est créé) : 1xbet.ci le connaît-il, avec quels paris ?"""
    from kelly.sources import http, xbet
    ses = xbet._Session()
    if ses.why:
        print(f"1xBet indisponible : {ses.why}")
        return
    try:
        for body in ({"Guid": code, "CfView": 0, "Lng": "fr", "partner": xbet.COMMON["ref"]}, {"Guid": code, "Lng": "fr"}):
            r = ses.call(http.post, GET_URL, body)
            print(f"GetCoupon {sorted(body)} -> HTTP {r.status}")
            try:
                print(json.dumps(json.loads(r.content or b"{}"), ensure_ascii=False, indent=1)[:2500])
            except ValueError:
                print((r.content or b"")[:500])
    finally:
        ses.close("relecture du coupon")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--format":
        show_format(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "--get":
        read_coupon(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "--test":
        run_test()
    else:
        print(__doc__)
