"""Écrit CONFIG_ACTIVE.md à la racine : les réglages actifs de Kelly (voir kelly/configactive.py).

    python tools/config_active.py [--stdout]

Charge .env comme le fait bot.py (les valeurs effectives sont celles du téléphone) mais n'écrit jamais un secret."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kelly import envfile  # noqa: E402

envfile.load()
from kelly import configactive  # noqa: E402

if __name__ == "__main__":
    if "--stdout" in sys.argv:
        print(configactive.render())
    else:
        print(configactive.write())
