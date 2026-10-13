"""`kelly erreurs [heures]` : résumé du journal de Kelly (erreurs, avertissements, plantages) des dernières heures, à coller tel quel (kelly/logdigest.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kelly import logdigest  # noqa: E402

if __name__ == "__main__":
    print(logdigest.command(sys.argv[1:]))
