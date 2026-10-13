"""Champs d'audit écrits à la clôture d'un pari suivi (track.close) : `close_book` (avis des books de référence à la clôture) et `xclose` (prix 1xBet LU
DANS LE CACHE DISQUE, avec l'âge du relevé), sans aucune requête en plus ; un prix absent ou illisible n'empêche jamais la clôture ni le CLV ; une ligne
sans référence 1xBet n'a pas de `xclose`. Hors ligne. Lancer : python -m tests.test_close_audit_fields"""
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-closefields-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-closefields-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import track  # noqa: E402
from kelly.sources import state, xbet  # noqa: E402

NOW = datetime(2030, 1, 1, 18, 0, tzinfo=timezone.utc)


def row(key, xref=None):
    return {"key": key, "closed_at": None, "odds": 2.0, "xref": xref, "xodds": 2.4 if xref else None}


def pick(key, **kw):
    return {"key": key, "odds": 1.9, "prob": 0.52} | kw


def test_fields():
    old = xbet.cached_cell
    try:
        xbet.cached_cell = lambda ref: xbet.Cell(2.3, state.clock() - 600, False)          # relevé vieux de 10 minutes
        rows = [row("a", [901, 1, 0]), row("b")]
        n = track.close(rows, [pick("a", prob_book=0.5234, prob_src="pinnacle", n_sharp=1), pick("b", prob_book=0.4, prob_src="consensus")], NOW)
        a, b = rows
        assert n == 2 and a["clv"] is not None and b["clv"] is not None
        assert a["close_book"] == {"p": 0.5234, "src": "pinnacle", "n_sharp": 1}, a
        assert a["xclose"] == {"odds": 2.3, "age_min": 10, "susp": False}, a
        assert b["close_book"]["src"] == "consensus" and "xclose" not in b, "sans référence 1xBet : pas de prix 1xBet de clôture"

        xbet.cached_cell = lambda ref: xbet.Cell(None, state.clock() - 60, False)          # le relevé en cache ne propose plus ce pari
        rows = [row("c", [901, 1, 0])]
        track.close(rows, [pick("c")], NOW)
        assert rows[0]["xclose"] == {"odds": None, "age_min": 1, "susp": False} and "close_book" not in rows[0], rows[0]

        xbet.cached_cell = lambda ref: None                                                    # match absent du cache
        rows = [row("d", [901, 1, 0])]
        track.close(rows, [pick("d")], NOW)
        assert "xclose" not in rows[0] and rows[0]["clv"] is not None

        def boom(ref):
            raise RuntimeError("cache illisible")
        xbet.cached_cell = boom                                                                # ne bloque jamais la clôture
        rows = [row("e", [901, 1, 0])]
        assert track.close(rows, [pick("e", prob_book=0.5)], NOW) == 1
        assert rows[0]["clv"] is not None and rows[0]["closed_at"] and rows[0]["close_book"]["p"] == 0.5 and "xclose" not in rows[0], rows[0]
    finally:
        xbet.cached_cell = old
    print("ok  clôture : avis des books de référence et prix 1xBet du cache (avec son âge) notés ; absent ou illisible : la clôture et le CLV passent quand même")


def main():
    test_fields()
    print("tests champs d'audit de clôture : OK")


if __name__ == "__main__":
    main()
