"""A2 (audit du 17/10) — comment 365scores écrit un match terminé APRÈS PROLONGATION ou aux tirs au but ? Lecture SEULE du cache disque des sources (aucune requête, aucun réseau, rien d'écrit).

    python tools/audit_s365_status.py [--cache-dir /chemin/.cache/sources]

Sur le téléphone : `python tools/audit_s365_status.py` (cache par défaut : .cache/sources à la racine du dépôt). Parcourt les pages en cache (« <clé>.bin » + « <clé>.json ») dont l'adresse est chez
365scores, compte les `statusText` / `statusGroup` des matchs de football (sportId 1) et liste ceux qui ne sont ni « Ended » ni « Postponed » ni « Suspended » (candidats : prolongation, tirs au but) avec le score et l'adresse.
kelly/sources/s365.py n'accepte que statusText == « ended » (statusGroup 4) : si un match après prolongation s'écrit « Ended », les stats de 120 minutes sont lues comme du temps réglementaire."""
import argparse
import json
import os
import sys
from collections import Counter


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache-dir", default=os.environ.get("SOURCES_CACHE_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "sources"))
    a = ap.parse_args()
    if not os.path.isdir(a.cache_dir):
        sys.exit(f"cache introuvable : {a.cache_dir}")
    seen, odd, pages = Counter(), [], 0
    for name in sorted(os.listdir(a.cache_dir)):
        if not name.endswith(".json"):
            continue
        try:
            meta = json.load(open(os.path.join(a.cache_dir, name)))
            url = str(meta.get("url") or "")
            if "365scores" not in url:
                continue
            body = json.loads(open(os.path.join(a.cache_dir, name[:-5] + ".bin"), "rb").read())
        except (OSError, ValueError, AttributeError):
            continue
        games = body.get("games") if isinstance(body, dict) else None
        if not isinstance(games, list):
            continue
        pages += 1
        for g in games:
            if not isinstance(g, dict) or g.get("sportId") != 1:
                continue
            k = (g.get("statusGroup"), str(g.get("statusText")))
            seen[k] += 1
            if str(g.get("statusText")).strip().lower() not in ("ended", "postponed", "suspended", "not started", "scheduled") and g.get("statusGroup") in (3, 4, 5):
                odd.append((k, (g.get("homeCompetitor") or {}).get("name"), (g.get("awayCompetitor") or {}).get("name"), (g.get("homeCompetitor") or {}).get("score"), (g.get("awayCompetitor") or {}).get("score"), url[:90]))
    print(f"cache : {a.cache_dir} — {pages} page(s) 365scores avec des matchs")
    for (grp, txt), n in seen.most_common():
        print(f"  statusGroup {grp} statusText {txt!r} : {n}")
    print(f"matchs à statut inhabituel (ni Ended, ni Postponed / Suspended) : {len(odd)}")
    for o in odd[:25]:
        print("  ", o)
    if not odd:
        print("  aucun : pas d'exemple de prolongation / tirs au but dans ce cache (rien ne permet de conclure).")


if __name__ == "__main__":
    main()
