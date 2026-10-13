"""Marchés par championnat (coupes comprises) et par sens, dans le terminal : python tools/marches.py [tirs | corners | ldc | premier…]
(kelly marches). Même rapport que « marchés détail » sur Telegram. Lecture seule."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kelly import markets, telegram  # noqa: E402


def main():
    print(telegram.plain(markets.by_league(["détail", *sys.argv[1:]])))


if __name__ == "__main__":
    main()
