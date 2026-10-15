"""Courbes en image (hors ligne, sans bibliothèque) : PNG valide dessiné en Python pur, police 5x7 (accents retirés), axes lisibles, courbe du capital et du CLV 1xBet tirées des données,
commande « courbes » (images envoyées sans sonnerie, réponse vide) et message « pas encore de courbe ». Lancer : python -m tests.test_charts"""
import math
import os
import struct
import tempfile
import zlib
from datetime import date, timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-charts-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-charts-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from kelly import app, charts, config as C, ensemble, lanestats as L, money, store, telegram  # noqa: E402


def decode(png):
    """(largeur, hauteur, pixels RGB [ligne][colonne]) d'un PNG RGB 8 bits sans filtre, lu à la main."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", None
    while pos < len(png):
        n, tag = struct.unpack(">I4s", png[pos:pos + 8])
        data = png[pos + 8:pos + 8 + n]
        assert zlib.crc32(tag + data) & 0xFFFFFFFF == struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])[0], f"CRC {tag}"
        if tag == b"IHDR":
            size = struct.unpack(">IIBBBBB", data)
        elif tag == b"IDAT":
            idat += data
        pos += 12 + n
    w, h, depth, ctype, *_ = size
    assert (depth, ctype) == (8, 2)
    raw = zlib.decompress(idat)
    rows = []
    for y in range(h):
        line = raw[y * (w * 3 + 1):(y + 1) * (w * 3 + 1)]
        assert line[0] == 0
        rows.append([tuple(line[1 + 3 * x:4 + 3 * x]) for x in range(w)])
    return w, h, rows


def test_font_and_png():
    for ch, rows in charts.FONT.items():
        assert len(rows) == 7 and all(len(r) == 5 and set(r) <= {"0", "1"} for r in rows), ch
    assert charts._ascii("Capital é à ç % 1 234,5 € ✓") == "CAPITAL E A C % 1 234,5  "
    cv = charts.Canvas(40, 20)
    cv.text(2, 2, "A1", scale=1)
    w, h, rows = decode(cv.png())
    assert (w, h) == (40, 20) and rows[0][0] == charts.BG
    ink = [(x, y) for y in range(h) for x in range(w) if rows[y][x] == charts.FG]
    assert ink and min(x for x, _ in ink) >= 2 and max(y for _, y in ink) <= 8, "le texte reste dans son cadre 5x7"
    assert rows[2][3] == charts.FG and rows[2][2] != charts.FG, "premier trait du A : 2e colonne de la 1re ligne « 01110 »"
    print("ok  police 5x7 complète (chiffres, lettres, signes), accents retirés, PNG valide (CRC, dimensions, pixels)")


def test_axes_and_line():
    lo, hi, step = charts._nice(48123, 54500)
    assert (lo, hi, step) == (48000, 56000, 2000), (lo, hi, step)
    lo, hi, step = charts._nice(-3.2, 4.1)
    assert lo <= -3.2 and hi >= 4.1 and lo % step == 0 and hi % step == 0 and (hi - lo) / step <= 8, (lo, hi, step)
    lo, hi, step = charts._nice(7.0, 7.0)                           # valeurs toutes égales : un axe quand même
    assert lo < hi and step > 0
    lo, hi, step = charts._nice(0.001, 0.004)
    assert lo <= 0.001 and hi >= 0.004 and step > 0
    days = [str(date(2026, 9, 1) + timedelta(days=i)) for i in range(10)]
    png = charts.line_chart("CAPITAL (FCFA)", days, [50000 + 1000 * i for i in range(10)], zero=50000)
    w, h, rows = decode(png)
    assert (w, h) == (charts.W, charts.H)
    flat = [px for r in rows for px in r]
    assert charts.LINE in flat and charts.ZERO in flat and charts.GRID in flat, "courbe, ligne de départ rouge et grille"
    # la courbe monte : le dernier point est plus haut (y plus petit) que le premier
    ys = {x: [y for y in range(h) if rows[y][x] == charts.LINE] for x in (100, 760)}
    assert ys[100] and ys[760] and min(ys[760]) < min(ys[100]), ys
    # axe en vraies dates : un jour sans pari est un trou (le 2e point est tout près du 1er, pas au milieu)
    d0 = date(2026, 9, 1)
    gap_days = [str(d0), str(d0 + timedelta(days=1)), str(d0 + timedelta(days=9))]
    even_days = [str(d0 + timedelta(days=k)) for k in (0, 1, 2)]

    def top_y(days_, x):
        w_, h_, rows_ = decode(charts.line_chart("X", days_, [1.0, 2.0, 3.0]))
        return min(y for y in range(h_) if rows_[y][x] == charts.LINE)
    assert top_y(gap_days, 432) < top_y(even_days, 432), "au milieu de l'image, la courbe aux jours espacés a déjà presque atteint son dernier point"
    # grandes valeurs : la marge gauche s'agrandit, les étiquettes ne touchent pas le bord de l'image
    w, h, rows = decode(charts.line_chart("X", gap_days, [1_000_000.0, 2_500_000.0, 3_000_000.0]))
    assert all(rows[y][x] == charts.BG for y in range(h) for x in range(0, 4)), "rien ne déborde à gauche"
    print("ok  courbe : axes « ronds », grille, ligne de départ, la courbe suit les valeurs, axe en vraies dates")


def test_series_and_command():
    from kelly import fair, track
    store.save(money.FILE, {})
    assert charts.courbes() == [], "rien à tracer"
    out = app.courbes_command()
    assert "Pas encore de courbe" in telegram.plain(out)
    # capital : 3 jours de paris notés et réglés
    today = date.today()
    d = [str(today - timedelta(days=k)) for k in (3, 2, 1)]
    money.set_daily(0)
    store.save(money.FILE, {"capital": 50000, "since": d[0]})
    store.save("bets.json", {"picks": [], "combos": [{"tier": f"t{i}", "keys": ["k"], "odds": 2.0, "prob": 0.5, "stake": None, "date": day, "result": "win" if i != 1 else "loss",
                                                       "played": {"odds": 2.0, "stake": 1000, "at": "x"}} for i, day in enumerate(d)]})
    days, cap, base = charts.capital_series()
    assert days == d and cap == [51000.0, 50000.0, 51000.0] and base == 50000, (days, cap)
    # CLV : 30 lignes de valeur réglées sur 3 jours, prix pris 2,10 contre 2,00 à la clôture (+5 %)
    rows = []
    for i in range(30):
        rows.append({"key": f"k{i}", "id": f"m{i}", "date": d[i % 3], "market": "alternate_totals_corners", "sport_key": "soccer_epl", "strategy": "sharpgap", "result": "win",
                     "xodds": 2.1, "xclose": {"odds": 2.0, "lead_min": 60, "susp": False}})
    store.save(ensemble.LOG_FILE, rows)
    L._memo["key"] = None
    cdays, mean, lo, hi, n = charts.clv_series()
    assert cdays == d and n == 30 and all(abs(m - 5.0) < 1e-9 for m in mean) and all(a - 1e-9 <= 5.0 <= b + 1e-9 for a, b in zip(lo, hi)), (mean, lo, hi)
    res = charts.courbes()
    assert [os.path.basename(p) for p, _ in res] == ["capital.png", "clv.png"] and all(decode(open(p, "rb").read())[0] == charts.W for p, _ in res)
    assert "51 000 FCFA" in res[0][1] and "+5.00 %" in res[1][1] and "30 paris" in res[1][1], res
    # valeurs variées : la bande est l'intervalle PAR MATCH de lanestats.stats (grappes), centré sur la moyenne exacte
    var = []
    for i in range(30):
        var.append({"key": f"v{i}", "id": f"g{i // 2}", "date": d[i % 3], "market": "alternate_totals_corners", "sport_key": "soccer_epl", "strategy": "sharpgap", "result": "win",
                    "xodds": 2.0 + 0.04 * (i % 7) - 0.05, "xclose": {"odds": 2.0, "lead_min": 60, "susp": False}})
    store.save(ensemble.LOG_FILE, var)
    L._memo["key"] = None
    vdays, vmean, vlo, vhi, vn = charts.clv_series()
    xs = [L.x_clv(r) for r in var]
    n_, m_, h_ = L.stats(xs, [r["id"] for r in var])
    assert vdays == d and vn == 30 and abs(vmean[-1] - 100 * (math.exp(m_) - 1)) < 1e-9, (vmean[-1], m_)
    assert abs(vhi[-1] - 100 * (math.exp(m_ + h_) - 1)) < 1e-9 and abs(vlo[-1] - 100 * (math.exp(m_ - h_) - 1)) < 1e-9 and vlo[-1] < vmean[-1] < vhi[-1], (vlo[-1], vmean[-1], vhi[-1])
    store.save(ensemble.LOG_FILE, rows)
    L._memo["key"] = None
    # capital jamais sous zéro (comme /capital), jour sans date écarté
    store.save(money.FILE, {"capital": 1000, "since": d[0]})
    store.save("bets.json", {"picks": [], "combos": [{"tier": "t0", "keys": ["k"], "odds": 2.0, "prob": 0.5, "stake": None, "date": d[0], "result": "loss", "played": {"odds": 2.0, "stake": 5000, "at": "x"}},
                                                       {"tier": "t1", "keys": ["k"], "odds": 2.0, "prob": 0.5, "stake": None, "date": d[1], "result": "win", "played": {"odds": 2.0, "stake": 100, "at": "x"}},
                                                       {"tier": "t2", "keys": ["k"], "odds": 2.0, "prob": 0.5, "stake": None, "date": "", "result": "win", "played": {"odds": 2.0, "stake": 100, "at": "x"}}]})
    cdays2, cap2, _b = charts.capital_series()
    assert cdays2 == d[:2] and cap2[0] == 0.0 and min(cap2) >= 0, (cdays2, cap2)
    store.save(money.FILE, {"capital": 50000, "since": d[0]})
    store.save("bets.json", {"picks": [], "combos": [{"tier": f"t{i}", "keys": ["k"], "odds": 2.0, "prob": 0.5, "stake": None, "date": day, "result": "win" if i != 1 else "loss",
                                                       "played": {"odds": 2.0, "stake": 1000, "at": "x"}} for i, day in enumerate(d)]})
    # la commande : les deux images partent (sans sonnerie), la réponse est vide
    sent = []
    real = telegram.send_photo
    telegram.send_photo = lambda path, caption=None, chat_id=None, silent=True: sent.append((os.path.basename(path), caption, silent))
    try:
        assert app.handle_command("courbes") == "" and [s[0] for s in sent] == ["capital.png", "clv.png"] and all(s[2] for s in sent), sent
        sent.clear()
        telegram.send_photo = lambda *a, **k: (_ for _ in ()).throw(telegram.TelegramError("réseau : coupé"))
        assert "Courbe non envoyée" in app.handle_command("/courbes")
    finally:
        telegram.send_photo = real
    # moins de lignes de CLV que le seuil : pas de courbe de CLV
    store.save(ensemble.LOG_FILE, rows[:C.CHART_MIN_CLV - 1])
    L._memo["key"] = None
    assert charts.clv_series()[0] == []
    print("ok  courbes du capital et du CLV : séries tirées des paris notés et du journal, commande « courbes », images sans sonnerie, seuil de lignes")


def test_send_photo():
    """Multipart, légende HTML, sans sonnerie, jeton jamais dans l'erreur, refus de l'API."""
    import requests
    calls = []
    real = (requests.post, telegram.creds)
    telegram.creds = lambda: ("SECRETTOKEN", "42")

    class R:
        def __init__(self, body):
            self.body, self.status_code = body, 200

        def json(self):
            return self.body
    path = os.path.join(tempfile.mkdtemp(prefix="kelly-photo-"), "x.png")
    open(path, "wb").write(charts.Canvas(8, 8).png())
    try:
        requests.post = lambda url, data=None, files=None, timeout=None: calls.append((url, data, files)) or R({"ok": True, "result": {"message_id": 5}})
        assert telegram.send_photo(path, "légende <b>x</b>") == {"message_id": 5}
        url, data, files = calls[0]
        assert url.endswith("/sendPhoto") and data["chat_id"] == "42" and data["parse_mode"] == "HTML" and data["disable_notification"] == "true" and "photo" in files
        calls.clear()
        telegram.send_photo(path, "x", silent=False)
        assert "disable_notification" not in calls[0][1], "silent=False : avec sonnerie"
        requests.post = lambda *a, **k: R({"ok": False, "description": "Bad Request: chat not found", "error_code": 400})
        try:
            telegram.send_photo(path, "x")
            raise AssertionError("refus de l'API")
        except telegram.TelegramError as e:
            assert "chat not found" in str(e) and e.code == 400

        def boom(*a, **k):
            raise requests.ConnectionError("https://api.telegram.org/botSECRETTOKEN/sendPhoto : coupé")
        requests.post = boom
        try:
            telegram.send_photo(path, "x")
            raise AssertionError("réseau")
        except telegram.TelegramError as e:
            assert "SECRETTOKEN" not in str(e), e
    finally:
        requests.post, telegram.creds = real
    print("ok  send_photo : multipart, légende HTML, sans sonnerie, refus de l'API, jeton jamais dans l'erreur")


def main():
    test_font_and_png()
    test_axes_and_line()
    test_series_and_command()
    test_send_photo()
    print("tests courbes : OK")


if __name__ == "__main__":
    main()
