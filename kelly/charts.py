"""Graphiques en image PNG (14/10), SANS aucune bibliothèque : un petit dessinateur en Python pur (zlib + struct) avec une police 5x7 intégrée. Rien à installer sur le téléphone, rien
qui puisse casser `kelly update`. Deux courbes, tirées de ce que Kelly a déjà en mémoire :

  - le capital : capital de départ + gains des paris NOTÉS et réglés, jour après jour (money._counted) ;
  - le CLV 1xBet : moyenne cumulée, jour après jour, de « prix pris contre le dernier prix 1xBet » des paris de valeur réglés des voies jouées (lanestats.x_clv), en %.
Textes en majuscules sans accent (la police n'a que ASCII). `courbes()` -> [(chemin du PNG, légende)] ; envoi : telegram.send_photo."""
import math
import os
import struct
import tempfile
import unicodedata
import zlib
from collections import defaultdict
from datetime import date

from . import config as C

W, H = 800, 450
BG, FG, GRID, LINE, ZERO, BAND = (255, 255, 255), (30, 30, 30), (225, 225, 225), (30, 100, 220), (210, 50, 50), (200, 220, 250)

_ROWS = {
    "0": "01110 10001 10011 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110", "2": "01110 10001 00001 00010 00100 01000 11111",
    "3": "11110 00001 00001 01110 00001 00001 11110", "4": "00010 00110 01010 10010 11111 00010 00010", "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110", "7": "11111 00001 00010 00100 01000 01000 01000", "8": "01110 10001 10001 01110 10001 10001 01110",
    "9": "01110 10001 10001 01111 00001 00010 01100", "-": "00000 00000 00000 11111 00000 00000 00000", "+": "00000 00100 00100 11111 00100 00100 00000",
    ".": "00000 00000 00000 00000 00000 00000 00100", ",": "00000 00000 00000 00000 00100 00100 01000", "%": "11001 11010 00010 00100 01000 01011 10011",
    "/": "00001 00010 00010 00100 01000 01000 10000", ":": "00000 00100 00000 00000 00000 00100 00000", " ": "00000 00000 00000 00000 00000 00000 00000",
    "(": "00010 00100 01000 01000 01000 00100 00010", ")": "01000 00100 00010 00010 00010 00100 01000",
    "A": "01110 10001 10001 11111 10001 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110", "C": "01110 10001 10000 10000 10000 10001 01110",
    "D": "11110 10001 10001 10001 10001 10001 11110", "E": "11111 10000 10000 11110 10000 10000 11111", "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111", "H": "10001 10001 10001 11111 10001 10001 10001", "I": "01110 00100 00100 00100 00100 00100 01110",
    "J": "00111 00010 00010 00010 00010 10010 01100", "K": "10001 10010 10100 11000 10100 10010 10001", "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001", "N": "10001 11001 10101 10011 10001 10001 10001", "O": "01110 10001 10001 10001 10001 10001 01110",
    "P": "11110 10001 10001 11110 10000 10000 10000", "Q": "01110 10001 10001 10001 10101 10010 01101", "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110", "T": "11111 00100 00100 00100 00100 00100 00100", "U": "10001 10001 10001 10001 10001 10001 01110",
    "V": "10001 10001 10001 10001 10001 01010 00100", "W": "10001 10001 10001 10101 10101 10101 01010", "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100", "Z": "11111 00001 00010 00100 01000 10000 11111",
}
FONT = {ch: [r for r in rows.split()] for ch, rows in _ROWS.items()}


def _ascii(text):
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().upper()
    return "".join(ch if ch in FONT else " " for ch in t)


class Canvas:
    """Image RGB W x H en mémoire : fond blanc, traits, rectangles, texte 5x7 (échelle entière)."""

    def __init__(self, w=W, h=H):
        self.w, self.h = w, h
        self.px = bytearray(bytes(BG) * (w * h))

    def dot(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(c)

    def rect(self, x0, y0, x1, y1, c):
        for y in range(max(0, int(y0)), min(self.h, int(y1) + 1)):
            for x in range(max(0, int(x0)), min(self.w, int(x1) + 1)):
                i = (y * self.w + x) * 3
                self.px[i:i + 3] = bytes(c)

    def line(self, x0, y0, x1, y1, c, width=1):
        dx, dy = x1 - x0, y1 - y0
        n = int(max(abs(dx), abs(dy))) or 1
        for k in range(n + 1):
            x, y = x0 + dx * k / n, y0 + dy * k / n
            for ox in range(width):
                for oy in range(width):
                    self.dot(int(round(x)) + ox - width // 2, int(round(y)) + oy - width // 2, c)

    def text(self, x, y, s, c=FG, scale=2, anchor="l"):
        s = _ascii(s)
        width = len(s) * 6 * scale - scale
        x = x - width if anchor == "r" else x - width // 2 if anchor == "c" else x
        for k, ch in enumerate(s):
            for ry, row in enumerate(FONT[ch]):
                for rx, bit in enumerate(row):
                    if bit == "1":
                        self.rect(x + (k * 6 + rx) * scale, y + ry * scale, x + (k * 6 + rx + 1) * scale - 1, y + (ry + 1) * scale - 1, c)

    def png(self):
        """Octets d'un PNG RGB 8 bits."""
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3]) for y in range(self.h))

        def chunk(tag, data):
            body = tag + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def _nice(lo, hi, n=5):
    """Bornes et pas « ronds » pour un axe : -> (min, max, pas)."""
    if hi - lo < 1e-9 * max(1.0, abs(hi)):                  # valeurs toutes égales : un axe quand même (±5 %, ±1 près de zéro)
        pad = abs(hi) * 0.05 or 1.0
        lo, hi = lo - pad, hi + pad
    span = hi - lo
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def _fmt(v, step):
    if step >= 1:
        return f"{v:,.0f}".replace(",", " ")
    return f"{v:.{2 if step < 0.1 else 1}f}"


def line_chart(title, days, values, unit="", zero=None, lo_band=None, hi_band=None):
    """Courbe `values` (une valeur par jour de `days`, AAAA-MM-JJ) -> octets PNG. `zero` : ligne de référence rouge (valeur). `lo_band` / `hi_band` : bande d'incertitude (même longueur)."""
    cv = Canvas()
    pts = list(values)
    ys = pts + [v for v in (lo_band or []) + (hi_band or []) if v is not None] + ([zero] if zero is not None else [])
    ymin, ymax, step = _nice(min(ys), max(ys))
    widest = max(len(_ascii(_fmt(v, step) + unit)) for v in (ymin, ymax))
    left, right, top, bottom = max(95, widest * 12 + 20), W - 30, 60, H - 55            # marge gauche : la plus longue étiquette de l'axe tient
    cv.text(left, 18, title, scale=3)
    n = len(pts)
    ords = [date.fromisoformat(d).toordinal() for d in days]               # axe en vraies dates : un jour sans pari est un trou, pas un raccourci
    span = (ords[-1] - ords[0]) or 1

    def px(i):
        return left + (right - left) * ((ords[i] - ords[0]) / span if n > 1 else 0.5)

    def py(v):
        return bottom - (bottom - top) * (v - ymin) / ((ymax - ymin) or 1)
    k = 0
    while ymin + k * step <= ymax + 1e-9:
        v = ymin + k * step
        cv.line(left, py(v), right, py(v), GRID)
        cv.text(left - 8, py(v) - 7, _fmt(v, step) + unit, anchor="r")
        k += 1
    if lo_band and hi_band:
        for i in range(n):
            if lo_band[i] is not None and hi_band[i] is not None:
                cv.line(px(i), py(hi_band[i]), px(i), py(lo_band[i]), BAND, 3 if n > 60 else 5)
    if zero is not None:
        cv.line(left, py(zero), right, py(zero), ZERO, 2)
    cv.line(left, top, left, bottom, FG)
    cv.line(left, bottom, right, bottom, FG)
    for i in range(1, n):
        cv.line(px(i - 1), py(pts[i - 1]), px(i), py(pts[i]), LINE, 3)
    cv.rect(px(n - 1) - 3, py(pts[-1]) - 3, px(n - 1) + 3, py(pts[-1]) + 3, LINE)
    for k in range(5):                                                      # 5 repères de date, régulièrement espacés dans le temps
        d = date.fromordinal(ords[0] + round(span * k / 4)).isoformat() if n > 1 else days[0]
        cv.text(left + (right - left) * (k / 4 if n > 1 else 0.5), bottom + 10, f"{d[8:10]}/{d[5:7]}", anchor="c")
    cv.text(right, 20, (f"{pts[-1]:.2f}" if unit else _fmt(pts[-1], step)) + unit, c=LINE, scale=3, anchor="r")
    return cv.png()


def _daily(pairs):
    """[(jour, valeur)] -> {jour: [valeurs]}, triés."""
    out = defaultdict(list)
    for d, v in pairs:
        out[str(d)[:10]].append(v)
    return dict(sorted(out.items()))


def capital_series(today=None):
    """([jours], [capital à la fin de chaque jour], capital de départ) des paris notés et réglés depuis le début du mode capital (ou le premier pari noté) ; ([], [], None) sans assez de données."""
    from . import money
    st = money.capital_state(today)
    base = money._amount(money.load().get("capital"))
    docs = money._docs()
    since = str(money.load().get("since") or "")[:10]
    counted = [(str(x.get("date") or "")[:10], g) for x, g in money._counted(*docs) if str(x.get("date") or "")[:10] >= since and len(str(x.get("date") or "")) >= 10]
    if not st or not base:
        return [], [], None
    by_day = _daily(counted)
    days, cap, run = [], [], float(base)
    for d, gs in by_day.items():
        run += sum(gs)
        days.append(d)
        cap.append(round(max(0.0, run), 2))             # comme capital_state : jamais sous zéro
    return days, cap, base


def clv_series():
    """([jours], [CLV cumulé en %], [bas], [haut], n lignes) : moyenne cumulée de log(prix pris / dernier prix 1xBet) des paris de valeur réglés (voies sharpgap + stats jouées), jour après jour,
    avec l'intervalle à 95 % de la moyenne cumulée. ([], …, 0) sans au moins 2 jours et C.CHART_MIN_CLV lignes."""
    from . import lanes, lanestats as L
    rows = [r for r in L.lane_rows("sharpgap") + L.lane_rows("stats") if r.get("strategy") == "sharpgap" or not lanes.league_ghost(r.get("sport_key"))]
    trip = [(str(r.get("date"))[:10], x, r.get("id") or i) for i, r in enumerate(rows) for x in [L.x_clv(r)] if x is not None and r.get("date")]
    by_day = defaultdict(list)
    for d, x, g in trip:
        by_day[d].append((x, g))
    by_day = dict(sorted(by_day.items()))
    if len(by_day) < 2 or len(trip) < C.CHART_MIN_CLV:
        return [], [], [], [], len(trip)
    days, mean, lo, hi, acc, ids = [], [], [], [], [], []
    for d, xs in by_day.items():
        acc += [x for x, _ in xs]
        ids += [g for _, g in xs]
        _n, m, h = L.stats(acc, ids)                    # intervalle PAR MATCH (grappes), comme les objectifs : les paris d'un même match sont corrélés
        h = h or 0.0
        days.append(d)
        mean.append(100 * (math.exp(m) - 1))
        lo.append(100 * (math.exp(m - h) - 1))
        hi.append(100 * (math.exp(m + h) - 1))
    return days, mean, lo, hi, len(trip)


def courbes(out_dir=None, today=None):
    """Les courbes disponibles -> [(chemin du PNG, légende HTML)] ; [] sans donnée. Fichiers dans `out_dir` (un dossier temporaire sinon : à supprimer par l'appelant, voir app.courbes_command)."""
    out_dir = out_dir or tempfile.mkdtemp(prefix="kelly-courbes-")
    res = []
    days, cap, base = capital_series(today)
    if len(days) >= 2:
        path = os.path.join(out_dir, "capital.png")
        with open(path, "wb") as f:
            f.write(line_chart("CAPITAL (FCFA)", days, cap, zero=base))
        res.append((path, f"💼 <b>Capital</b> : {cap[-1]:,.0f} FCFA".replace(",", " ") + f" (départ {base:,.0f})".replace(",", " ") + f" · {len(days)} jours de paris notés\n<i>Ligne rouge : ton capital de départ.</i>"))
    days, mean, lo, hi, n = clv_series()
    if days:
        path = os.path.join(out_dir, "clv.png")
        with open(path, "wb") as f:
            f.write(line_chart("CLV 1XBET CUMULE (%)", days, mean, unit="%", zero=0.0, lo_band=lo, hi_band=hi))
        res.append((path, f"🎯 <b>CLV 1xBet cumulé</b> : {mean[-1]:+.2f} % sur {n} paris de valeur réglés\n<i>Bande bleue : intervalle à 95 % (par match, comme tes objectifs). Au-dessus de la ligne rouge (zéro), le prix pris bat le dernier prix 1xBet.</i>"))
    return res
