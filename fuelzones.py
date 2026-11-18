#!/usr/bin/env python3
"""Ahorro de combustible por zonas: aprende en qué tramos de la pista conviene levantar.

La pista se divide en NB tramos (por distancia de vuelta). En cada vuelta limpia se mide, por tramo, el combustible
quemado, el tiempo y el acelerador medio. Con varias vueltas se ajusta, por tramo, cuánto combustible ahorra y cuánto
tiempo cuesta acelerar menos (regresión sobre tus propias variaciones entre vueltas). Se recomiendan los tramos con
más kg ahorrados por segundo perdido. Sin variación suficiente entre vueltas (no has probado a levantar) no se inventan
cifras: solo se marcan "candidatas" (final de recta antes de una frenada fuerte), sin decir cuánto ahorran.
Todo es estadística sobre tus datos: el LLM no interviene.
"""
import statistics

NB = 40
LIFT = 0.25            # cuánto menos acelerador se asume al levantar (fracción del acelerador)
MIN_SD_THR = 0.03      # variación mínima del acelerador entre vueltas para poder medir un tramo
MIN_T = 2.5            # el efecto sobre el consumo debe superar 2.5 veces su error estándar
COST_FLOOR_S = 0.004   # coste mínimo (s por vuelta) al calcular kg/s: evita ganar por dividir entre casi cero


def _slope(xs, ys):
    """(pendiente, error estándar) de la regresión lineal de ys sobre xs; (None, None) si no hay variación."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 1e-12 or n < 4:
        return None, None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    rss = sum((y - my - b * (x - mx)) ** 2 for x, y in zip(xs, ys))
    return b, (rss / (n - 2) / sxx) ** 0.5


class FuelZones:
    def __init__(self, nb=NB, min_laps=6):
        self.nb, self.min_laps = nb, min_laps
        self.lap = None
        self.cur = None
        self.dirty = True
        self.laps = []                 # vueltas limpias completas
        self.track_len = None
        self.saved = None              # zonas aprendidas en otras sesiones (perfil)

    # ------------------------------------------------------------------ muestreo
    def _start(self, lap, track_len):
        nb = self.nb
        self.lap, self.track_len = lap, track_len
        self.cur = {"enter": [None] * nb, "thr": [0.0] * nb, "spd": [0.0] * nb, "brk": [0] * nb, "n": [0] * nb,
                    "last": None, "prev": None}
        self.dirty = lap <= 1

    def _end_lap(self):
        c = self.cur
        if c is None or self.dirty or c["last"] is None:
            return
        nb = self.nb
        if any(e is None for e in c["enter"]) or min(c["n"]) < 3:
            return
        burn, tim = [], []
        for b in range(nb):
            f0, t0 = c["enter"][b]
            f1, t1 = c["enter"][b + 1] if b + 1 < nb else c["last"]
            burn.append(f0 - f1)
            tim.append(t1 - t0)
        if min(burn) < -1e-4 or min(tim) <= 0 or sum(tim) < 20 or c["last"][0] < 0.3:
            return
        tot = sum(burn)                 # una vuelta con consumo muy distinto de las anteriores (sin gasolina, etc.) no sirve
        if self.laps:
            med = statistics.median(sum(l["burn"]) for l in self.laps[-5:])
            if tot < 0.6 * med or tot > 1.6 * med:
                return
        self.laps.append({"burn": burn, "time": tim, "thr": [c["thr"][b] / c["n"][b] for b in range(nb)],
                          "spd": [c["spd"][b] / c["n"][b] for b in range(nb)],
                          "brk": [c["brk"][b] / c["n"][b] for b in range(nb)]})
        if len(self.laps) > 60:
            del self.laps[0]

    def sample(self, lap, dist, track_len, fuel, t, thr, spd, brake, clean):
        if not track_len or lap < 1:
            return
        if lap != self.lap:
            self._end_lap()
            self._start(lap, track_len)
        if not clean:
            self.dirty = True
        b = min(max(int(dist / track_len * self.nb), 0), self.nb - 1)
        c = self.cur
        seg = track_len / self.nb
        prev = c["prev"]
        if c["enter"][0] is None:
            c["enter"][0] = (fuel, t)
        elif prev is not None and dist > prev[0]:            # fronteras de tramo cruzadas entre dos muestras: se interpolan
            for k in range(int(prev[0] / seg) + 1, min(int(dist / seg), self.nb - 1) + 1):
                if c["enter"][k] is None:
                    w = (k * seg - prev[0]) / (dist - prev[0])
                    c["enter"][k] = (prev[1] + w * (fuel - prev[1]), prev[2] + w * (t - prev[2]))
        c["prev"] = (dist, fuel, t)
        c["thr"][b] += thr
        c["spd"][b] += spd
        c["brk"][b] += 1 if brake > 0.1 else 0
        c["n"][b] += 1
        c["last"] = (fuel, t)

    # ------------------------------------------------------------------ análisis
    def analyze(self):
        """{'measured': [...], 'candidates': [...], 'laps': n}. 'measured' ordenado por kg ahorrados por segundo."""
        laps = self.laps
        out = {"measured": [], "candidates": [], "laps": len(laps), "nb": self.nb}
        if len(laps) < self.min_laps or not self.track_len:
            return out
        nb = self.nb
        seg = self.track_len / nb
        for b in range(nb):
            thr = [l["thr"][b] for l in laps]
            if statistics.pstdev(thr) < MIN_SD_THR:
                continue
            sb, seb = _slope(thr, [l["burn"][b] for l in laps])
            st, _ = _slope(thr, [l["time"][b] for l in laps])
            if sb is None or st is None or sb <= 0 or sb / max(seb, 1e-12) < MIN_T:   # más acelerador: más consumo
                continue
            save, cost = sb * LIFT, max(-st * LIFT, COST_FLOOR_S)                       # y (normalmente) menos tiempo
            out["measured"].append({"bin": b, "start_m": round(b * seg), "save_kg": round(save, 4),
                                    "cost_s": round(cost, 4), "kg_per_s": round(save / cost, 3)})
        out["measured"].sort(key=lambda z: -z["kg_per_s"])
        mean_spd = [statistics.mean(l["spd"][b] for l in laps) for b in range(nb)]
        top = max(mean_spd)
        for b in range(nb):
            thr = statistics.mean(l["thr"][b] for l in laps)
            nxt = [statistics.mean(l["brk"][(b + k) % nb] for l in laps) for k in (1, 2)]
            if thr >= 0.97 and mean_spd[b] >= 0.85 * top and max(nxt) >= 0.3:
                out["candidates"].append({"bin": b, "start_m": round(b * seg)})
        return out

    def suggest(self, n=2):
        """Frases (en español) con los mejores tramos. Medidas si hay datos; si no, candidatas sin cifras."""
        a = self.analyze()
        if not a["measured"] and not a["candidates"] and self.saved:
            a = self.saved
        zs = a["measured"][:n]
        if zs:
            return [f"Levanta antes en el tramo {z['bin'] + 1} (~{z['start_m']} m): ahorras ~{z['save_kg']:.3f} kg por vuelta "
                    f"y pierdes ~{z['cost_s']:.3f} s." for z in zs]
        cs = a["candidates"][:n]
        return [f"Candidata a levantar: final de recta hacia ~{z['start_m']} m, antes de frenar (aún sin medir cuánto ahorra)."
                for z in cs]

    # ------------------------------------------------------------------ perfil
    def export(self):
        a = self.analyze()
        return a if (a["measured"] or a["candidates"]) else None

    def load(self, data):
        self.saved = data if data and (data.get("measured") or data.get("candidates")) else None
