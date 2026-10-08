"""Données fictives football-data pour les tests : CSV de saison reproductible (scores, tirs, corners,
fautes, cartons, arbitre, cotes d'ouverture et de clôture). Le signal est connu : la cote Max d'ouverture
s'écarte du consensus (Avg) d'un bruit `stale`, et la cote Max de clôture revient de 80 % de cet écart
(le marché corrige) : valeur contre le consensus => CLV positif."""
import math
import random
from datetime import date, timedelta

TEAMS = ["Arsenal", "Chelsea", "Liverpool", "Man City", "Man United", "Tottenham", "Newcastle", "Aston Villa",
         "Brighton", "West Ham", "Everton", "Fulham", "Brentford", "Wolves", "Crystal Palace", "Bournemouth",
         "Nott'm Forest", "Leeds", "Burnley", "Sunderland"]
REFS = ["A Taylor", "M Oliver", "C Pawson", "S Attwell"]
_r = random.Random(1)
ATT = {t: _r.uniform(0.9, 1.9) for t in TEAMS}          # buts marqués attendus
DEF = {t: _r.uniform(0.8, 1.6) for t in TEAMS}          # buts encaissés attendus
COR = {t: _r.uniform(4.0, 7.5) for t in TEAMS}          # corners
CAR = {t: _r.uniform(1.0, 2.6) for t in TEAMS}          # cartons
COR["Arsenal"], COR["Chelsea"], CAR["Arsenal"], CAR["Chelsea"] = 7.0, 6.0, 2.0, 2.4

HEADER = ("Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,Referee,HS,AS,HST,AST,HF,AF,HC,AC,HY,AY,HR,AR,"
          "PSH,PSD,PSA,MaxH,MaxD,MaxA,AvgH,AvgD,AvgA,P>2.5,P<2.5,Max>2.5,Max<2.5,Avg>2.5,Avg<2.5,"
          "PSCH,PSCD,PSCA,MaxCH,MaxCD,MaxCA,AvgCH,AvgCD,AvgCA,PC>2.5,PC<2.5,MaxC>2.5,MaxC<2.5,AvgC>2.5,AvgC<2.5")


def poisson(rng, mean):
    limit, k, p = math.exp(-mean), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _pmf(mean, k):
    return math.exp(-mean) * mean ** k / math.factorial(k)


def probs(lh, la):
    """(P domicile, P nul, P extérieur, P plus de 2,5 buts) pour deux Poisson indépendants."""
    ph = pd = pa = 0.0
    for i in range(9):
        for j in range(9):
            p = _pmf(lh, i) * _pmf(la, j)
            ph, pd, pa = ph + p * (i > j), pd + p * (i == j), pa + p * (i < j)
    return ph, pd, pa, 1 - sum(_pmf(lh + la, k) for k in range(3))


def rounds(teams):
    """Calendrier aller-retour (méthode du cercle) : liste de journées [(domicile, extérieur)]."""
    ts, n, out = list(teams), len(teams), []
    for r in range(n - 1):
        out.append([(ts[i], ts[n - 1 - i]) if (r + i) % 2 == 0 else (ts[n - 1 - i], ts[i]) for i in range(n // 2)])
        ts = [ts[0], ts[-1]] + ts[1:-1]
    return out + [[(a, h) for h, a in day] for day in out]


def season_csv(start, seed, until=None, div="E0", teams=TEAMS):
    """CSV d'une saison commençant `start` (date) : une journée par semaine. Les matchs après `until` (date)
    ne sont pas joués : ils sont absents du fichier."""
    rng = random.Random(seed)
    lines = [HEADER]
    for k, day in enumerate(rounds(teams)):
        d = start + timedelta(days=7 * k)
        if until and d > until:
            break
        for h, a in day:
            lh, la = ATT[h] * DEF[a] / 1.2, ATT[a] * DEF[h] / 1.2
            hg, ag = poisson(rng, lh), poisson(rng, la)
            hc, ac = poisson(rng, COR[h] * 0.55 + COR[a] * 0.45), poisson(rng, COR[a] * 0.45 + COR[h] * 0.35)
            hy, ay = poisson(rng, CAR[h]), poisson(rng, CAR[a])
            hr, ar = int(rng.random() < 0.04), int(rng.random() < 0.04)
            p = probs(lh, la)
            odds = {"1x2": ([1 / (x * 1.05) for x in p[:3]]), "ou": [1 / (p[3] * 1.05), 1 / ((1 - p[3]) * 1.05)]}
            cols = []
            for key in ("1x2", "ou"):
                avg = [o * (1 + rng.gauss(0, 0.02)) for o in odds[key]]
                stale = [abs(rng.gauss(0, 0.06)) for _ in avg]
                mx = [a_ * (1 + 0.03 + s) for a_, s in zip(avg, stale)]
                ps = [a_ * (1 + rng.gauss(0, 0.02)) for a_ in avg]
                close = {"mx": [m_ * (1 - 0.8 * s + rng.gauss(0, 0.01)) for m_, s in zip(mx, stale)],
                         "avg": avg, "ps": [x * (1 + rng.gauss(0, 0.01)) for x in ps]}
                cols.append((ps, mx, avg, close["ps"], close["mx"], close["avg"]))
            f = lambda xs: [f"{x:.2f}" for x in xs]          # noqa: E731
            (p1, m1, a1, pc1, mc1, ac1), (p2, m2, a2, pc2, mc2, ac2) = cols
            row = [div, d.strftime("%d/%m/%Y"), "15:00", h, a, hg, ag, "H" if hg > ag else "A" if hg < ag else "D",
                   REFS[k % len(REFS)], 12 + hg * 2, 9 + ag * 2, 4 + hg, 3 + ag, poisson(rng, 11), poisson(rng, 11),
                   hc, ac, hy, ay, hr, ar] + f(p1) + f(m1) + f(a1) + f(p2) + f(m2) + f(a2) + f(pc1) + f(mc1) + f(ac1) + \
                  f(pc2) + f(mc2) + f(ac2)
            lines.append(",".join(str(x) for x in row))
    return "\n".join(lines) + "\n"


# Saisons 2223 .. 2627 : chacune commence début août.
STARTS = {"2223": date(2022, 8, 6), "2324": date(2023, 8, 12), "2425": date(2024, 8, 17), "2526": date(2025, 8, 16),
          "2627": date(2026, 8, 15)}


def csv_for(season, until=None):
    return season_csv(STARTS[season], seed=int(season), until=until)
