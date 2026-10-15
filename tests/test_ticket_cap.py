"""Plafond de tickets par jour (MAX_TICKETS_DAY, hors ligne). Lancer : python -m tests.test_ticket_cap"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-discreet-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-discreet-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY", "KELLY_UA"):
    os.environ.pop(k, None)

from kelly import config as C, money  # noqa: E402


def test_ticket_cap():
    items = [{"kind": "pari", "tier": "x", "prob": 0.5 + i * 0.05, "odds": 2.2} for i in range(5)]
    base = money.allocate(items, budget=20000)[0]
    assert sum(1 for s in base if s) >= 4, base
    C.MAX_TICKETS_DAY = 2
    try:
        capped = money.allocate(items, budget=20000)[0]
    finally:
        C.MAX_TICKETS_DAY = 0
    assert sum(1 for s in capped if s) == 2 and all(c == b or c == 0 for c, b in zip(capped, base)) and set(i for i, s in enumerate(capped) if s) == set(sorted(range(5), key=lambda i: -base[i])[:2])
    print("ok  plafond de tickets : seules les 2 plus grosses mises restent, 0 = pas de plafond")


def main():
    test_ticket_cap()
    print("tests plafond de tickets : OK")


if __name__ == "__main__":
    main()
