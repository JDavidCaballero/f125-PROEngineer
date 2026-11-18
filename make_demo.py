#!/usr/bin/env python3
"""Genera carreras inventadas (.f1raw) de 22 coches para probar al ingeniero sin abrir el juego.

  python3 make_demo.py all --out captures        # sprint, corta, media
  python3 make_demo.py corta --out captures

Escenarios (circuito ficticio de 4323 m, vuelta de ~75 s):
  sprint : 5 vueltas, sin paradas; un rival más rápido te caza por detrás (huecos, DRS, defensa)
  corta  : 15 vueltas, Blando de alto desgaste, Safety Car en las vueltas 6-7, combustible justo
  media  : 30 vueltas, daño de alerón en la vuelta 10, parada en la 11 y lluvia anunciada y luego caída
Usa los mismos diseños de paquete que f1_parse (validados con una captura real de F1 25 v1.26).
La física es propia de esta demo (no es la del motor de estrategia), así que sirve de contraste.
"""
import argparse, math, random, struct
from pathlib import Path

from f1_parse import HEADER, LAP, STA, DMG, TEL, PART, TSET, HIST_LAP, FINAL, HS, MAGIC, REC

NAMES = ["Alves", "Brandt", "Costa", "Duval", "Esposito", "Fischer", "Garrido", "Hansen", "Ibarra", "Jansen",
         "Keller", "Lombardi", "Moreau", "Novak", "Ortega", "Petrov", "Quintero", "Rossi", "Silva", "Tanaka",
         "Ueda", "Vargas"]
OFF = {16: 0.0, 17: 0.84, 18: 1.68, 7: 6.1, 8: 8.5}
USABLE = {16: 22, 17: 38, 18: 42, 7: 63, 8: 44}
K, CLIFF_W, CLIFF_K = 0.025, 55.0, 0.006
TRACK_M, BASE_LAP, PIT_LOSS, BURN, T0 = 4323, 75.0, 22.0, 1.5, 3.0
SIZES = {1: 753, 2: 1285, 3: 45, 4: 1284, 6: 1352, 7: 1239, 8: 1042, 10: 1041, 11: 1460, 12: 231}
POINTS = (25, 18, 15, 12, 10, 8, 6, 4, 2, 1)

DEFAULT = dict(total=15, player=8, grid_player=9, player_comp=17, wear_soft=3.0, player_pits={}, sc=None, scs=[], vscs=[],
               fuel_short=False, wing_lap=None, rain_lap=None, chaser=False, player_adv=0.0, red_lap=None,
               puncture_lap=None, dt_lap=None, chaser_pit=None, drop=0.0, lifts=False, hz=2)
SCEN = {
    "sprint": dict(total=5, player=6, grid_player=7, player_comp=16, wear_soft=2.4, chaser=True),
    "corta": dict(total=15, player=8, grid_player=9, player_comp=16, wear_soft=4.0, sc=(6, 7), fuel_short=True,
                  player_adv=-0.2),
    "media": dict(total=30, player=10, grid_player=11, player_comp=17, wear_soft=3.0, player_pits={11: 18}, wing_lap=10,
                  rain_lap=24, player_adv=-0.15),
    # --- casos extra para afinar el modelo
    "sc_final": dict(total=10, player_comp=16, wear_soft=2.5, scs=[(8, 9)]),
    "sc_tras_parada": dict(total=14, player_comp=16, wear_soft=3.5, player_pits={4: 18}, scs=[(6, 7)]),
    "vsc": dict(total=14, player_comp=16, wear_soft=3.5, vscs=[(6, 6)]),
    "pinchazo": dict(total=14, player_comp=17, wear_soft=3.0, puncture_lap=7),
    "roja": dict(total=14, player_comp=16, wear_soft=3.5, red_lap=6),
    "sancion": dict(total=14, player_comp=16, wear_soft=3.5, dt_lap=4, scs=[(6, 7)]),
    "rival_fresco": dict(total=15, player_comp=16, wear_soft=3.5, chaser_pit=(5, 16)),
    "datos_perdidos": dict(total=12, player_comp=16, wear_soft=3.5, drop=0.2),
    "ahorro": dict(total=20, player_comp=17, wear_soft=2.5, fuel_short=1.0, lifts=True, hz=5),
}


def scenario(name):
    sc = {**DEFAULT, **SCEN[name]}
    if sc["sc"] and not sc["scs"]:
        sc["scs"] = [sc["sc"]]
    return sc


NZ = 40
GOOD_ZONES = {9, 21, 33}         # tramos donde levantar ahorra mucho y cuesta poco (verdad del mundo simulado)


def cliff(w):
    return CLIFF_K * (w - CLIFF_W) ** 2 if w > CLIFF_W else 0.0


class Car:
    def __init__(self, idx, base, comp, wear_soft):
        self.idx, self.base, self.wear_soft = idx, base, wear_soft
        self.comp, self.wear, self.used = comp, 0.0, {comp}
        self.T, self.lt, self.w_hist = [], [], []   # fin de vuelta (T[0]=salida), tiempos, (desgaste ini, fin)
        self.stints = [(0, comp)]                   # (primera vuelta del juego de neumáticos, compuesto)
        self.pits = []                              # vueltas de entrada a boxes
        self.pending, self.jit, self.grid = 0.0, 0.0, 0

    def rate(self, c):
        return self.wear_soft * USABLE[16] / USABLE[c]


def simulate(sc, seed):
    rng = random.Random(seed)
    total = sc["total"]
    cars = [Car(i, BASE_LAP + rng.uniform(-0.4, 1.8) + 0.08 * i, rng.choices([16, 17, 18], [0.5, 0.4, 0.1])[0],
                sc["wear_soft"] * rng.uniform(0.9, 1.1)) for i in range(22)]
    for c in cars:
        c.jit = rng.uniform(-6, 6)
    me = cars[sc["player"]]
    me.base = BASE_LAP + 0.95 + sc["player_adv"]
    me.comp, me.used, me.stints = sc["player_comp"], {sc["player_comp"]}, [(0, sc["player_comp"])]
    others = sorted((c for c in cars if c is not me), key=lambda c: c.base)
    grid = others[:sc["grid_player"] - 1] + [me] + others[sc["grid_player"] - 1:]
    for pos, c in enumerate(grid):
        c.grid, c.T = pos + 1, [T0 + 0.4 * pos]
    if sc["chaser"]:                          # el coche justo detrás del jugador es más rápido que tú
        grid[sc["grid_player"]].base = me.base - 0.55
    sc_laps = {x for a, b in sc["scs"] for x in range(a, b + 1)}
    vsc_laps = {x for a, b in sc["vscs"] for x in range(a, b + 1)}
    rule = total >= 12
    order = list(grid)
    chaser = grid[sc["grid_player"]] if (sc["chaser"] or sc["chaser_pit"]) else None
    if sc["chaser_pit"]:
        chaser.base = me.base - 0.2
    scripted = {chaser.idx: {sc["chaser_pit"][0]: sc["chaser_pit"][1]}} if sc["chaser_pit"] else {}
    lrng = random.Random(seed * 7 + 1)
    me.lifts = [[(lrng.uniform(0.1, 0.6) if lrng.random() < 0.5 else 0.0) if sc["lifts"] else 0.0 for _ in range(NZ)]
                for _ in range(total)]
    me.zone_burn, me.zone_time = [], []
    punct = False
    for j in range(1, total + 1):
        for c in cars:
            if j in sc_laps:
                lt, w0, w1 = BASE_LAP * 1.35, c.wear, c.wear + c.rate(c.comp) * 0.4
            elif j in vsc_laps:
                w0, w1 = c.wear, c.wear + c.rate(c.comp) * 0.5
                wm = (w0 + w1) / 2
                lt = (c.base + OFF[c.comp] + K * wm + cliff(wm) + rng.gauss(0, 0.1)) * 1.3
            else:
                w0, w1 = c.wear, c.wear + c.rate(c.comp)
                wm = (w0 + w1) / 2
                lt = c.base + OFF[c.comp] + K * wm + cliff(wm) + rng.gauss(0, 0.18) + (2.5 if j == 1 else 0.0)
            if c is me:
                if sc["puncture_lap"] and j >= sc["puncture_lap"] and punct is not None:
                    punct = True
                if punct:
                    lt += 3.0
                if j not in sc_laps and j not in vsc_laps:
                    lift, zb, zt = me.lifts[j - 1], [], []
                    for z in range(NZ):                      # levantar ahorra combustible y cuesta tiempo según el tramo
                        good = z in GOOD_ZONES
                        zt.append(lt / NZ * (1 + (0.015 if good else 0.10) * lift[z]))
                        zb.append(BURN / NZ * (1 - (0.8 if good else 0.15) * lift[z]))
                    lt = sum(zt)
                else:
                    zb, zt = [BURN / NZ] * NZ, [lt / NZ] * NZ
                me.zone_burn.append(zb)
                me.zone_time.append(zt)
            lt += c.pending
            c.pending = 0.0
            c.lt.append(lt)
            c.T.append(c.T[-1] + lt)
            c.w_hist.append((w0, w1))
            c.wear = w1
        if j not in sc_laps:                  # tráfico: sin ventaja de ritmo suficiente no se adelanta (como en el juego)
            for a, b in zip(order, order[1:]):
                if b.T[-1] < a.T[-1] + 0.5 and (a.lt[-1] - b.lt[-1]) < 0.6:
                    shift = a.T[-1] + 0.5 - b.T[-1]
                    b.T[-1] += shift
                    b.lt[-1] += shift
            order = sorted(cars, key=lambda c: c.T[-1])
        if j in sc_laps or j == sc["red_lap"]:    # el pelotón se compacta tras el coche de seguridad / bandera roja
            gap = 1.2 if j in sc_laps else 1.0
            order = sorted(cars, key=lambda c: c.T[-1])
            for a, b in zip(order, order[1:]):
                if b.T[-1] > a.T[-1] + gap:
                    shift = b.T[-1] - (a.T[-1] + gap)
                    b.T[-1] -= shift
                    b.lt[-1] -= shift
            order = sorted(cars, key=lambda c: c.T[-1])
        left = total - j
        for c in cars:                        # decisiones de parada al final de la vuelta j
            go = None
            if c is me:
                go = sc["player_pits"].get(j)
            elif c.idx in scripted:
                go = scripted[c.idx].get(j)
            elif left >= 3:
                must = rule and len(c.used) == 1 and left <= 3
                neutral = j in sc_laps or j + 1 in sc_laps or j in vsc_laps or j + 1 in vsc_laps or j == sc["red_lap"]
                under_sc = neutral and c.wear >= (25 if j == sc["red_lap"] else 20) and left >= (3 if j == sc["red_lap"] else 6) \
                    and (j == sc["red_lap"] or rng.random() < 0.5)
                if c.wear >= 60 + c.jit or must or under_sc:
                    opts = [x for x in (17, 18) if x != c.comp] if c.comp != 16 else [17, 18]
                    go = rng.choice(opts)
            if go and left >= 1:
                factor = 0.0 if j == sc["red_lap"] else (0.5 if (j in sc_laps or j + 1 in sc_laps) else
                                                         (0.6 if (j in vsc_laps or j + 1 in vsc_laps) else 1.0))
                c.pending = PIT_LOSS * factor
                if c is me:
                    punct = None if sc["puncture_lap"] else False      # el pinchazo se arregla al parar
                c.pits.append(j)
                c.comp, c.wear = go, 0.0
                c.used.add(go)
                c.stints.append((j + 1, go))
    return cars, me, sc_laps, vsc_laps


# ---------------------------------------------------------------------------------------------- muestreo
def progress(c, tau, total):
    if tau <= c.T[0]:
        return 0.0
    if tau >= c.T[total]:
        return float(total)
    lo, hi = 0, total
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if c.T[mid] <= tau else (lo, mid)
    return lo + (tau - c.T[lo]) / (c.T[lo + 1] - c.T[lo])


def time_at(c, p, total):
    if p <= 0:
        return c.T[0]
    if p >= total:
        return c.T[total]
    j = int(p)
    return c.T[j] + (p - j) * (c.T[j + 1] - c.T[j])


def stint_state(c, tau, p, total):
    """Compuesto, desgaste (%) y edad (vueltas) del coche en el instante tau (el juego se monta 4 s tras entrar)."""
    jlap = min(int(p), total - 1)
    frac = p - jlap
    lap_no = jlap + 1
    k = max(n for n, (fl, _) in enumerate(c.stints) if n == 0 or tau >= c.T[fl - 1] + 4)
    k_lap = max(n for n, (fl, _) in enumerate(c.stints) if fl <= lap_no)
    if k < k_lap:                              # todavía con los neumáticos viejos en boxes
        wear = c.w_hist[jlap - 1][1] if jlap >= 1 else 0.0
    else:
        w0, w1 = c.w_hist[jlap]
        wear = w0 + (w1 - w0) * frac
    first, comp = c.stints[k]
    return comp, wear, max(0, lap_no - max(first, 1))


def put_ms(ms):
    ms = max(int(ms), 0)
    return ms % 60000, ms // 60000


def pkt(pid, tau, frame, uid, player):
    b = bytearray(SIZES[pid])
    b[:HS] = HEADER.pack(2025, 25, 1, 26, 1, pid, uid, float(tau), frame, frame, player, 255)
    return b


def participants_packet(tau, frame, uid, P):
    b = pkt(4, tau, frame, uid, P)
    b[HS] = 22
    for i in range(22):
        name = ("TU" if i == P else NAMES[i]).upper().encode()
        b[HS + 1 + i * PART.size:HS + 1 + (i + 1) * PART.size] = PART.pack(
            0 if i == P else 1, i, 255, i // 2, 0, 10 + i, 1, name, 1, 1, 0, 0, 0, b"\0" * 12)
    return b


def event_packet(code, payload, tau, frame, uid, P):
    b = pkt(3, tau, frame, uid, P)
    b[HS:HS + 4] = code.encode()
    b[HS + 4:HS + 4 + len(payload)] = payload
    return b


def session_packet(sc, tau, frame, uid, P, sc_status, weather, rain_t, num_red=0):
    b = pkt(1, tau, frame, uid, P)
    body = bytearray(724)
    struct.pack_into("<BbbBHBbBHHBB", body, 0, weather, 32, 26, sc["total"], TRACK_M, 15, 17, 0, 3600, 7200, 80, 0)
    body[18] = 21
    for k in range(21):
        struct.pack_into("<fb", body, 19 + 5 * k, k / 21, 0)
    body[124] = sc_status
    body[678] = num_red
    samples = []
    for m in (0, 5, 10, 15):
        t_abs = tau + m * 60
        if rain_t is None:
            samples.append((m, 0, 3))
        else:
            rain = 75 if t_abs >= rain_t - 120 else (40 if t_abs >= rain_t - 480 else 10)
            samples.append((m, 3 if t_abs >= rain_t else 0, rain))
    body[126] = len(samples)
    for k, (m, w, r) in enumerate(samples):
        struct.pack_into("<BBBbbbbB", body, 127 + 8 * k, 15, m, w, 32, 0, 26, 0, r)
    body[671] = 3 if sc["total"] <= 20 else 4
    body[696] = 2
    struct.pack_into("<ff", body, 716, TRACK_M * 0.33, TRACK_M * 0.66)
    b[HS:] = body
    return b


def history_packet(c, tau, frame, uid, P, total):
    b = pkt(11, tau, frame, uid, P)
    done = [j for j in range(1, total + 1) if tau >= c.T[j]]
    b[HS], b[HS + 1] = c.idx, len(done)
    for k, j in enumerate(done):
        ms = int(c.lt[j - 1] * 1000)
        b[HS + 7 + k * HIST_LAP.size:HS + 7 + (k + 1) * HIST_LAP.size] = HIST_LAP.pack(
            ms, *put_ms(ms * 0.3), *put_ms(ms * 0.4), *put_ms(ms * 0.3), 1)
    comps = [s[1] for s in c.stints]
    pit_laps = [j for j in c.pits if tau >= c.T[j] + 4]
    stints = [(j - 1, comps[k]) for k, j in enumerate(pit_laps)] + [(255, comps[len(pit_laps)])]
    b[HS + 2] = len(stints)
    base = HS + 7 + 100 * HIST_LAP.size
    for k, (e, cmp_) in enumerate(stints[:8]):
        b[base + 3 * k:base + 3 * k + 3] = bytes([e, cmp_, cmp_])
    return b


def tyre_sets_packet(c, tau, frame, uid, P, total):
    b = pkt(12, tau, frame, uid, P)
    b[HS] = c.idx
    comp, wear, _ = stint_state(c, tau, progress(c, tau, total), total)
    comps = [16, 16, 17, 17, 18, 18, 7, 8]
    fitted_idx = comps.index(comp)
    for k, cc in enumerate(comps):
        fitted = k == fitted_idx
        delta = 0 if fitted else (OFF[cc] - OFF[comp] - K * wear - cliff(wear)) * 1000
        life = max(USABLE[cc] - int(wear / 3), 0) if fitted else USABLE[cc]
        b[HS + 1 + k * TSET.size:HS + 1 + (k + 1) * TSET.size] = TSET.pack(
            cc, cc, int(wear) if fitted else 0, 1, 0, life, USABLE[cc], int(delta), 1 if fitted else 0)
    b[HS + 1 + 20 * TSET.size] = fitted_idx
    return b


def final_packet(cars, tau, frame, uid, P, total):
    b = pkt(8, tau, frame, uid, P)
    b[HS] = 22
    for pos, c in enumerate(sorted(cars, key=lambda c: c.T[total]), 1):
        comps = [s[1] for s in c.stints][:8]
        sta = comps + [0] * (8 - len(comps))
        ends = ([j - 1 for j in c.pits] + [255] * 8)[:8]
        b[HS + 1 + c.idx * FINAL.size:HS + 1 + (c.idx + 1) * FINAL.size] = FINAL.pack(
            pos, total, c.grid, POINTS[pos - 1] if pos <= 10 else 0, len(c.pits), 3, 0, int(min(c.lt) * 1000),
            c.T[total], 0, 0, len(comps), *sta, *sta, *ends)
    return b


def generate(name, path, seed=11, hz=2):
    sc = scenario(name)
    hz = sc["hz"] if sc["hz"] != 2 else hz
    cars, me, sc_laps, vsc_laps = simulate(sc, seed)
    total, P = sc["total"], sc["player"]
    uid = 7000 + seed * 10 + list(SCEN).index(name)
    horizon = max(c.T[total] for c in cars) + 3.0
    pit_t = {c.idx: [(c.T[j], j) for j in c.pits] for c in cars}
    windows = [(min(c.T[a - 1] for c in cars), min(c.T[b] for c in cars), 1) for a, b in sc["scs"]] + \
              [(min(c.T[a - 1] for c in cars), min(c.T[b] for c in cars), 2) for a, b in sc["vscs"]]
    red_t = min(c.T[sc["red_lap"]] for c in cars) if sc["red_lap"] else None
    dt_t = min(c.T[sc["dt_lap"] - 1] for c in cars) if sc["dt_lap"] else None
    dt_end = next((tt for tt, _ in pit_t[P] if dt_t is not None and tt >= dt_t), None)
    punct_t = min(c.T[sc["puncture_lap"] - 1] for c in cars) if sc["puncture_lap"] else None
    punct_end = next((tt + 4 for tt, _ in pit_t[P] if punct_t is not None and tt >= punct_t), None)
    drng = random.Random(seed + 99)
    sent_pena = False
    rain_t = min(c.T[sc["rain_lap"] - 1] for c in cars) if sc["rain_lap"] else None
    wing_t = min(c.T[sc["wing_lap"] - 1] for c in cars) if sc["wing_lap"] else None
    short = 0.6 if sc["fuel_short"] is True else (float(sc["fuel_short"]) if sc["fuel_short"] else -1.2)
    total_fuel = BURN * (total - short)

    def me_frac(tau):
        """(vuelta, fracción de distancia) del jugador: tarda más en los tramos donde levanta."""
        p = progress(me, tau, total)
        j = min(int(p), total - 1)
        if p >= total:
            return j, 1.0
        t0, t1 = me.T[j], me.T[j + 1]
        zt = me.zone_time[j]
        scale, acc = (t1 - t0) / sum(zt), 0.0
        for z, tz in enumerate(zt):
            tz *= scale
            if tau - t0 <= acc + tz:
                return j, (z + (tau - t0 - acc) / tz) / NZ
            acc += tz
        return j, 1.0

    def me_fuel(j, f):
        """Combustible del jugador: quema por tramo (con las levantadas del escenario 'ahorro')."""
        z = min(int(f * NZ), NZ - 1)
        done = sum(sum(zb) for zb in me.zone_burn[:j])
        part = sum(me.zone_burn[j][:z]) + (f * NZ - z) * me.zone_burn[j][z]
        return max(total_fuel - done - part, 0.0)

    out, frame, tick, prev_sc, sent_final, rot = [], 0, 0, 0, False, 0
    out.append((0.0, participants_packet(0.0, 0, uid, P)))
    tau, dt = 0.0, 1.0 / hz
    while tau <= horizon:
        frame += 1
        pr = [progress(c, tau, total) for c in cars]
        order = sorted(range(22), key=lambda i: (-pr[i], cars[i].grid))
        rank = {i: k for k, i in enumerate(order)}
        sc_status = next((s for t0, t1, s in windows if t0 <= tau < t1), 0)
        if sc_status != prev_sc:
            out.append((tau, event_packet("SCAR", bytes([sc_status or prev_sc, 0 if sc_status else 2]), tau, frame, uid, P)))
            prev_sc = sc_status
        if dt_t is not None and tau >= dt_t and not sent_pena:
            out.append((tau, event_packet("PENA", bytes([0, 0, P, 255, 0, sc["dt_lap"], 255]), tau, frame, uid, P)))
            sent_pena = True
        dt_on = dt_t is not None and tau >= dt_t and (dt_end is None or tau < dt_end)
        weather = 3 if (rain_t is not None and tau >= rain_t) else 0
        if tick % hz == 0:
            out.append((tau, session_packet(sc, tau, frame, uid, P, sc_status, weather, rain_t,
                                            1 if (red_t is not None and tau >= red_t) else 0)))
        lp = pkt(2, tau, frame, uid, P)
        pit_now = 0
        jm, fm = me_frac(tau)
        for i, c in enumerate(cars):
            p = pr[i]
            fin = tau >= c.T[total]
            j = min(int(p), total - 1)
            frac = 1.0 if fin else (fm if i == P else p - j)
            cur_ms = 0 if (fin or tau < c.T[0]) else (tau - c.T[j]) * 1000
            last_ms = c.lt[-1] * 1000 if fin else (c.lt[j - 1] * 1000 if j >= 1 else 0)
            k = rank[i]
            gf = gl = 0
            if k > 0:
                gf = (time_at(c, pr[order[k - 1]], total) - tau) * 1000
                gl = (time_at(c, pr[order[0]], total) - tau) * 1000
            pit_status, drv, pl_ms, ps_ms = 0, 4, 0, 0
            for tt, jj in pit_t[i]:
                if tt - 6 <= tau < tt:
                    pit_status, drv, pl_ms = 1, 2, int((tau - (tt - 6)) * 1000)
                elif tt <= tau < tt + 14:
                    pit_status, drv, pl_ms = 2, 2, int((tau - (tt - 6)) * 1000)
                    if tt + 3 <= tau < tt + 5.4:
                        ps_ms = int((tau - (tt + 3)) * 1000)
                elif tt + 14 <= tau < c.T[min(jj + 1, total)]:
                    drv = 3
            n_stops = sum(1 for tt, _ in pit_t[i] if tau >= tt + 14)
            if i == P:
                pit_now = pit_status
            lp[HS + i * LAP.size:HS + (i + 1) * LAP.size] = LAP.pack(
                int(last_ms), int(cur_ms), *put_ms(min(cur_ms * 0.33, 27000)), *put_ms(0), *put_ms(gf), *put_ms(gl),
                frac * TRACK_M, (int(p) + frac if (i == P and not fin) else p) * TRACK_M, 0.0, k + 1, min(int(p) + 1, total), pit_status, n_stops,
                0 if frac < 0.33 else (1 if frac < 0.66 else 2), 0, 0, 0, 0, (1 if (i == P and dt_on) else 0), 0, c.grid, drv,
                3 if fin else 2, 1 if pit_status else 0, pl_ms % 65535, ps_ms % 65535, 0, 0.0, 0)
        out.append((tau, lp))
        slow = tick % hz == 0                  # 1 Hz; en 'ahorro' el estado y la telemetría van a todo el ritmo (como en el juego)
        if slow or sc["lifts"]:
            st, te = pkt(7, tau, frame, uid, P), pkt(6, tau, frame, uid, P)
            dm = pkt(10, tau, frame, uid, P)
            for i, c in enumerate(cars):
                p = pr[i]
                comp, wear, age = stint_state(c, tau, p, total)
                fuel = me_fuel(jm, fm) if i == P else max(total_fuel - BURN * p, 0.0)
                k = rank[i]
                gap_f = time_at(c, pr[order[k - 1]], total) - tau if k > 0 else 99.0
                frac = fm if i == P else p - min(int(p), total - 1)
                drs = gap_f < 1.0 and (0.15 < frac < 0.35 or 0.6 < frac < 0.8) and sc_status == 0 and p > 1.3
                ers = max(0.0, 4_000_000 * (0.5 + 0.5 * math.sin(p * 2.1 + i)))
                st[HS + i * STA.size:HS + (i + 1) * STA.size] = STA.pack(
                    2, 1, 1, 58, 0, fuel, 110.0, fuel / BURN, 13000, 4000, 8, 1 if drs else 0, 0, comp, comp, age, 0,
                    0.0, 0.0, ers, 1, 0.0, 0.0, 0.0, 0)
                wing = 35 if (i == P and wing_t is not None and tau >= wing_t
                              and not (me.pits and tau > me.T[me.pits[0]] + 6)) else 0
                punc = i == P and punct_t is not None and tau >= punct_t and (punct_end is None or tau < punct_end)
                if slow:
                    dm[HS + i * DMG.size:HS + (i + 1) * DMG.size] = DMG.pack(
                        wear, wear * 0.95, wear * 0.62, wear * 0.55, *([0, 0, 70, 0] if punc else [0] * 4), *([0] * 8),
                        wing, *([0] * 17))
                if i == P:
                    spd = 60 if pit_now else (int(150 + 120 * math.sin(frac * 6.283 * 3)) if sc_status == 0 else 90)
                    thr = 1.0 - me.lifts[jm][min(int(fm * NZ), NZ - 1)] if (sc_status == 0 and p < total) else 1.0
                    te[HS + P * TEL.size:HS + (P + 1) * TEL.size] = TEL.pack(
                        max(spd, 40), thr, 0.0, 0.0, 0, max(2, min(8, spd // 40)), 9000, 1 if drs else 0, 50, 0,
                        700, 705, 690, 695, 85, 86, 84, 85, 92, 93, 91, 92, 112, 22.0, 22.1, 22.0, 22.1, 0, 0, 0, 0)
            out += [(tau, st), (tau, te)]
            if slow:
                if tick % (2 * hz) == 0:
                    out.append((tau, dm))
                for _ in range(3):            # SessionHistory rota entre coches, como en el juego
                    out.append((tau, history_packet(cars[rot % 22], tau, frame, uid, P, total)))
                    rot += 1
                out.append((tau, tyre_sets_packet(me, tau, frame, uid, P, total)))
        if not sent_final and all(tau >= c.T[total] for c in cars):
            out.append((tau, final_packet(cars, tau, frame, uid, P, total)))
            out.append((tau, event_packet("CHQF", b"", tau, frame, uid, P)))
            sent_final = True
        tau += dt
        tick += 1
    with open(path, "wb") as f:
        f.write(MAGIC)
        for t, d in out:
            if sc["drop"] and d[6] not in (1, 4, 8) and drng.random() < sc["drop"]:     # paquetes perdidos por la red
                continue
            f.write(REC.pack(t, len(d)) + bytes(d))
    ranked = sorted(cars, key=lambda c: c.T[total])
    return {"name": name, "packets": len(out), "seconds": horizon, "player": P, "sc_laps": sc_laps, "vsc_laps": vsc_laps,
            "red_t": red_t, "dt_t": dt_t, "punct_t": punct_t,
            "wing_t": wing_t, "rain_t": rain_t, "final_pos": next(k + 1 for k, c in enumerate(ranked) if c.idx == P),
            "player_pits": list(me.pits), "ai_stops": sum(len(c.pits) for c in cars if c is not me)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=list(SCEN) + ["all"])
    ap.add_argument("--out", default="captures")
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    Path(a.out).mkdir(parents=True, exist_ok=True)
    for name in (SCEN if a.scenario == "all" else [a.scenario]):
        path = Path(a.out) / f"demo_{name}.f1raw"
        i = generate(name, path, a.seed)
        print(f"Listo: {i['packets']} paquetes ({i['seconds']:.0f} s) en {path} | jugador acaba P{i['final_pos']}, "
              f"paradas del jugador {i['player_pits']}, paradas de la IA {i['ai_stops']}")


if __name__ == "__main__":
    main()
