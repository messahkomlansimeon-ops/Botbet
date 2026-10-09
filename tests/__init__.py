"""Paquet des tests de Kelly (aucun réseau). Les suites appellent un faux API-Football en rafale : l'écart de STATS_MIN_GAP secondes entre deux
appels et l'attente de STATS_RATE_WAIT secondes après un HTTP 429 (kelly/stats.py) sont donc coupés ici, avant que kelly.config ne lise
l'environnement. tests/test_sources_fix.py les remet à leur valeur réelle (horloge simulée) pour les tester."""
import os

os.environ.setdefault("STATS_MIN_GAP", "0")
os.environ.setdefault("STATS_RATE_WAIT", "0")
os.environ.setdefault("S365_PROXY", "0")            # les suites 365scores testent la connexion polie ; tests/test_s365.py teste le proxy à part
