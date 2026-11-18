#!/usr/bin/env python3
"""Vueltas de TODOS los coches, trazadas por distancia: tiempo, velocidad, acelerador, freno y ERS en NB tramos.

Lo que mide: cada paquete de telemetría aporta, por coche, (distancia en la vuelta, tiempo de vuelta, velocidad, acelerador,
freno, ERS desplegado en la vuelta y batería). Al cerrarse la vuelta se guarda su traza si está completa (cobertura mínima)
y es válida. Una vuelta se queda con las muestras que tuvo: nada se inventa; los huecos entre muestras se interpolan solo
cuando son cortos.
Limitaciones: en online el juego puede ocultar la telemetría de otros jugadores humanos (con IA sí viene).
"""
from bisect import bisect_left
from collections import deque

NB = 200                   # tramos por vuelta (~25 m en una pista de 5 km)
MIN_COVER = 0.85           # fracción de tramos con muestra real para aceptar la vuelta
KEEP = 6                   # vueltas recientes por coche


def interp(xs, ys, x):
    """Interpolación lineal sobre puntos ordenados (se satura en los extremos)."""
    if not xs:
        return None
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    k = bisect_left(xs, x)
    x0, x1 = xs[k - 1], xs[k]
    return ys[k - 1] if x1 == x0 else ys[k - 1] + (ys[k] - ys[k - 1]) * (x - x0) / (x1 - x0)


class LapRec:
    """Traza de una vuelta. t, ers y store en los NB+1 bordes de tramo; speed, thr y brk en el centro de cada tramo."""

    def __init__(self, car, lap, time_ms, timed, valid, cover, t, speed, thr, brk, ers, store, track_m, comp):
        self.car, self.lap, self.time_ms, self.timed, self.valid, self.cover = car, lap, time_ms, timed, valid, cover
        self.t, self.speed, self.thr, self.brk, self.ers, self.store = t, speed, thr, brk, ers, store
        self.track_m, self.comp = track_m, comp
        self.status = None             # reparto de driver_status durante la vuelta (diagnóstico)

    def km(self, b):
        return b * self.track_m / NB / 1000.0


class _Cur:
    def __init__(self, lap):
        self.lap, self.s, self.status, self.invalid, self.n = lap, {}, {}, False, 0
        self.last_d = -1.0
        self.on_track = False          # ya hubo una muestra fuera de boxes en esta vuelta


class TraceBook:
    def __init__(self, nb=NB):
        self.nb = nb
        self.cur = {}
        self.recent = {}               # coche -> deque de LapRec
        self.best = {}                 # coche -> mejor LapRec cronometrada (válida y completa)
        self.done = {}                 # coche -> última LapRec cerrada (la lee quien quiera y la borra con pop_done)
        self.rejects = deque(maxlen=50)   # vueltas descartadas y por qué (para el informe): {car, lap, reason, cover, ...}
        self.track_m = None

    def reset(self):
        self.__init__(self.nb)

    def feed(self, tr):
        """Una muestra por coche desde el estado del RaceTracker (lap_all, sta_all, tel_all)."""
        tl = (tr.ses or {}).get("track_length_m")
        if not tl:
            return
        if self.track_m != tl:
            self.track_m = tl
        tel_all = getattr(tr, "tel_all", None) or []
        for i, la in enumerate(tr.lap_all):
            if not la or la["result_status"] != 2 or i >= len(tel_all) or not tel_all[i]:
                continue
            c = self.cur.get(i)
            if c is None or la["lap"] != c.lap:
                if c is not None and la["lap"] == c.lap + 1:
                    self._close(i, c, la["last_ms"], tr)
                c = self.cur[i] = _Cur(la["lap"])
            d = la["lap_distance"]
            if d is None or d < 0 or d > tl * 1.02 or d < c.last_d - tl * 0.5 or la["pit_status"] != 0:
                if la["pit_status"] != 0 and c.on_track:
                    c.invalid = True               # entrar a boxes en mitad de la vuelta no es una vuelta limpia (salir de ellas, sí)
                continue
            c.on_track = True
            c.last_d = d
            sa, te = tr.sta_all[i], tel_all[i]
            b = min(int(d / tl * self.nb), self.nb - 1)
            c.s[b] = (d, la["current_ms"], te["speed"], te["throttle"], te["brake"],
                      sa["ers_deployed_j"] if sa else 0, sa["ers_store_j"] if sa else 0)
            c.n += 1
            c.status[la["driver_status"]] = c.status.get(la["driver_status"], 0) + 1
            c.invalid = c.invalid or la["lap_invalid"]

    def _close(self, car, c, last_ms, tr):
        nb, tl = self.nb, self.track_m
        cover = len(c.s) / nb
        if last_ms <= 0 or cover < MIN_COVER:
            self.rejects.append({"car": car, "lap": c.lap, "reason": "sin tiempo" if last_ms <= 0 else "cobertura baja",
                                 "cover": round(cover, 2), "last_ms": last_ms, "invalid": c.invalid, "status": dict(c.status)})
            return
        pts = sorted(c.s.values())
        ds = [p[0] for p in pts]
        edges = [k * tl / nb for k in range(nb + 1)]
        t = [interp(ds, [p[1] for p in pts], e) for e in edges]
        t[0], t[-1] = 0.0, float(last_ms)                  # la vuelta empieza en 0 y acaba en el tiempo oficial
        for k in range(1, nb + 1):                         # el tiempo no puede retroceder
            t[k] = max(t[k], t[k - 1])
        ers = [interp(ds, [p[5] for p in pts], e) for e in edges]
        store = [interp(ds, [p[6] for p in pts], e) for e in edges]
        centers = [(k + 0.5) * tl / nb for k in range(nb)]
        speed = [interp(ds, [p[2] for p in pts], x) for x in centers]
        thr = [interp(ds, [p[3] for p in pts], x) for x in centers]
        brk = [interp(ds, [p[4] for p in pts], x) for x in centers]
        n = sum(c.status.values()) or 1
        # cronometrada: válida y sin ser de garaje/entrada/salida (estado 0, 2 o 3 durante la mayor parte de la vuelta)
        idle = (c.status.get(0, 0) + c.status.get(2, 0) + c.status.get(3, 0)) / n
        timed = (not c.invalid) and (tr.is_race() or idle < 0.5)
        sa = tr.sta_all[car]
        rec = LapRec(car, c.lap, last_ms, timed, not c.invalid, cover, t, speed, thr, brk, ers, store, tl,
                     sa["visual_compound"] if sa else None)
        rec.status = dict(c.status)
        self.recent.setdefault(car, deque(maxlen=KEEP)).append(rec)
        self.done[car] = rec
        if timed and (car not in self.best or last_ms < self.best[car].time_ms):
            self.best[car] = rec

    def pop_done(self, car):
        return self.done.pop(car, None)

    def last_timed(self, car):
        for r in reversed(self.recent.get(car, [])):
            if r.timed:
                return r
        return None
