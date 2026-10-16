"""📈 Alerte « la cote a monté » : « 💎 Valeur » seulement si le prix bat vraiment le marché ; une jambe jouable à valeur négative est dite telle quelle. Hors ligne.
Lancer : python -m tests.test_rise_label"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-rise-label-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-rise-label-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY"):
    os.environ.pop(k, None)

from kelly import xalerts  # noqa: E402

C = {"id": "m1", "home": "H", "away": "A", "event": "H - A", "league": "L", "start": "19:00", "pick": "Plus de 22,5 fautes"}


def test_label():
    pos = xalerts._rise_text(C, 1.5, 1.7, 0.04, "10:00")
    assert "💎 Valeur +4 %" in pos and "ne bat pas" not in pos, pos
    neg = xalerts._rise_text(C, 1.5, 1.7, -0.03, "10:00")
    assert "💎" not in neg and "⚖️ Valeur −3 %" in neg and "ne bat pas le marché" in neg, neg
    print("ok  alerte 📈 : 💎 seulement avec une valeur positive, sinon « ⚖️ … ne bat pas le marché »")


if __name__ == "__main__":
    test_label()
    print("tests étiquette 📈 : OK")
