"""Vueltas sintéticas para probar trazas, análisis de vuelta y ERS sin necesidad de una captura real."""
import math

from f1_parse import NCARS

TRACK = 5000.0
CORNERS = ((800.0, 90.0), (2000.0, 70.0), (3600.0, 110.0))      # (distancia del vértice m, velocidad mínima km/h)
VMAX = 300.0
STEP = 15.0


def speed_at(d, corners=CORNERS, slow=None):
    """Velocidad (km/h) en d: VMAX con un valle triangular de 300 m de ancho alrededor de cada vértice. slow={n: km/h menos}."""
    v = VMAX
    for n, (c, vmin) in enumerate(corners):
        m = vmin - (slow or {}).get(n, 0.0)
        w = abs(d - c)
        if w < 300:
            v = min(v, m + (VMAX - m) * w / 300.0)
    return v


def lap_samples(corners=CORNERS, slow=None, deploy=250.0, harvest=120.0, store0=3.2e6, cap=4e6, step=STEP, skip=None):
    """[(d, t_ms, speed, thr, brk, ers_cum_j, store_j)] de una vuelta; deploy/harvest en J por metro."""
    out, t, dep, store, d = [], 0.0, 0.0, store0, 0.0
    while d < TRACK:
        v = speed_at(d, corners, slow)
        vn = speed_at(d + step, corners, slow)
        thr = 1.0 if vn >= v - 0.01 and v > 150 else 0.2
        brk = 1.0 if vn < v - 0.5 else 0.0
        if thr >= 0.95:
            use = min(deploy * step, store)
            dep += use
            store -= use
        if brk > 0.5:
            store = min(cap, store + harvest * step)
        if not (skip and skip[0] <= d < skip[1]):
            out.append((d, t, v, thr, brk, dep, store))
        t += step / (v / 3.6) * 1000.0
        d += step
    return out, t


def drive(tr, car, samples, total_ms, lap, status=1, invalid=False, pit=False, comp=16, position=1):
    """Mete las muestras de una vuelta en el RaceTracker `tr` (como si llegaran paquetes) y cierra la vuelta con la siguiente."""
    for k, (d, t, v, thr, brk, ers, store) in enumerate(samples):
        set_car(tr, car, lap, d, t, v, thr, brk, ers, store, status, invalid, pit, comp, position, last_ms=0)
        tr.traces.feed(tr)
    d, t, v, thr, brk, ers, store = samples[-1]
    # primera muestra de la vuelta siguiente: cierra la anterior con su tiempo oficial
    set_car(tr, car, lap + 1, 5.0, 100.0, v, thr, brk, 0, store, status, False, False, comp, position, last_ms=int(round(total_ms)))
    tr.traces.feed(tr)


def set_car(tr, car, lap, d, t, v, thr, brk, ers, store, status, invalid, pit, comp, position, last_ms):
    tr.lap_all[car] = {"result_status": 2, "lap": lap, "last_ms": last_ms, "lap_distance": d, "current_ms": int(t),
                       "pit_status": 1 if pit else 0, "lap_invalid": invalid, "driver_status": status, "position": position,
                       "pit_stops": 0, "delta_front_ms": 0, "grid": 1}
    tr.sta_all[car] = {"ers_deployed_j": ers, "ers_store_j": store, "visual_compound": comp, "tyre_age_laps": 3, "fuel_kg": 30.0,
                       "fia_flag": 0, "drs_allowed": False, "ers_mode": 2, "fuel_mix": 0}
    tr.tel_all[car] = {"speed": v, "throttle": thr, "brake": brk}


def config(tr, session_type=5, track_m=TRACK, player=0):
    tr.player = player
    tr.ses = {"session_type": session_type, "track_length_m": track_m, "total_laps": 0, "weather": 0}
    return tr


def new_tracker(session_type=5, track_m=TRACK, player=0):
    """RaceTracker vacío con una sesión ya configurada."""
    import state
    return config(state.RaceTracker({}, None), session_type, track_m, player)
