#!/usr/bin/env python3
"""Seguimiento del estado de la carrera a partir de los paquetes UDP (solo librería estándar).

RaceTracker.on_packet(t, datos) actualiza el estado de los 22 coches y devuelve señales
(("lap", n), ("sc", nuevo, viejo), ("pit_in",), ("pit_out", ...), ("tyres", compuesto), ("event", {...}), ...).
build_inputs() convierte ese estado en la entrada del motor de estrategia (strategy.py).
"""
import difflib, re, statistics, unicodedata
from collections import deque
from dataclasses import replace

from f1_parse import (header, lap_data, car_telemetry, car_status, car_damage, session, event, participants,
                      tyre_sets, session_history, final_classification, RACE_TYPES, TIME_TRIAL, NCARS, tyre_extra_damage)
import copy
import strategy as S
from fuelzones import FuelZones
from traces import TraceBook

COMMON_WORDS = {"queda", "quedan", "quiero", "quinto", "quinta", "cuanto", "cuenta", "cuento", "vamos", "tengo", "puedo",
                "delante", "detras", "explica", "estoy", "hueco", "ritmo", "puesto", "parar", "boxes", "gomas"}
WHEELS = ["trasero izquierdo", "trasero derecho", "delantero izquierdo", "delantero derecho"]


def _slope(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return None if den < 1e-9 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def measure_pit_loss(hist):
    """Coste de una parada en s: (vuelta de entrada + vuelta de salida) - 2 vueltas de referencia, donde la
    referencia es la media de las vueltas limpias de antes y de después (así se cancela el cambio de compuesto).
    hist: {coche: session_history}. Se ignoran paradas en las vueltas 1-2 (la salida distorsiona)."""
    out = []
    # Vueltas lentas para TODOS (Safety Car, bandera roja): no sirven de referencia. Se detectan solo con los coches
    # que no paran en esa vuelta, para no confundir una parada masiva con un Safety Car.
    stops_of = {c: {s["end_lap"] for s in h["stints"] if s["end_lap"] != 255} for c, h in hist.items()}
    meds = {}
    for j in range(1, 100):
        v = [h["laps"][j]["ms"] for c, h in hist.items() if len(h["laps"]) > j and h["laps"][j]["ms"] > 0
             and j not in stops_of[c] and j - 1 not in stops_of[c]]
        if len(v) >= 3:
            meds[j] = statistics.median(v)
    slow = {j for j, m in meds.items() if m > min(meds.values()) * 1.12}
    for h in hist.values():
        laps = h["laps"]
        stops = [s["end_lap"] for s in h["stints"] if s["end_lap"] != 255]
        for e in stops:
            if e < 2 or e + 1 >= len(laps) or laps[e]["ms"] <= 0 or laps[e + 1]["ms"] <= 0 or {e, e + 1} & slow:
                continue

            def clean(lo, hi):
                v = [laps[j]["ms"] for j in range(max(lo, 1), min(hi, len(laps))) if laps[j]["ms"] > 0
                     and j not in stops and j - 1 not in stops and j not in slow]
                if not v:
                    return None
                lo_ms = min(v)
                return statistics.median([x for x in v if x <= lo_ms * 1.07])
            before, after = clean(e - 3, e), clean(e + 2, e + 5)
            refs = [r for r in (before, after) if r is not None]
            if not refs:
                continue
            loss = (laps[e]["ms"] + laps[e + 1]["ms"] - 2 * sum(refs) / len(refs)) / 1000
            if 8 < loss < 60:
                out.append(loss)
    return out


class RaceTracker:
    def __init__(self, cfg, profile=None):
        self.cfg = cfg
        self.params = S.Params.from_cfg(cfg)
        self.profile = profile or {"tracks": {}}
        g = self.profile.get("global", {})
        if g.get("laps", 0) >= 100 and g.get("races", 0) >= 3:      # frecuencia de SC aprendida de tus carreras
            emp = g.get("sc_periods", 0) / g["laps"]
            self.params.sc_prob_lap = (0.0, 0.5 * emp, emp, 1.75 * emp)
        an = g.get("ai_next", {})                                    # qué compuesto monta la IA después de cada uno
        table = {}
        for a, nxt in an.items():
            tot = sum(nxt.values())
            if tot >= 8:
                table[int(a)] = {int(b): n / tot for b, n in nxt.items()}
        self.params.ai_next = table
        self.reset()

    # ------------------------------------------------------------------ estado
    def reset(self):
        self.epoch = getattr(self, "epoch", 0) + 1      # cambia con cada sesión nueva: el ingeniero reinicia su propio estado
        self.uid = None
        self.year = None
        self.fmt = None
        self.player = None
        self.t = 0.0
        self.ses = {}
        self.lap_all = [None] * NCARS
        self.sta_all = [None] * NCARS
        self.dmg_all = [None] * NCARS
        self.tel = None
        self.tel_all = [None] * NCARS
        self.traces = TraceBook()
        self.stint_log = []          # tandas ya cerradas del jugador: {"comp", "age", "wear"}
        self.names = {}
        self.team = {}              # coche -> id de equipo (para el compañero)
        self.hist = {}
        self.sets = {}
        self.events = deque(maxlen=200)
        self.final = None
        self.burn = []
        self.lap_sc = False
        self.dmg_onset, self.dmg_aero = None, 0.0       # vuelta del último golpe aerodinámico y su daño
        self.fuel_lap_start = None
        self.cur_lap = 0
        self.stint = []             # (edad_en_vueltas, desgaste_max)
        self.last_age = None
        self.last_comp = None
        self.gap_hist = {}          # vuelta -> {"front": s, "behind": s}
        self.ahead_hist = {}        # coche -> {vuelta: hueco (s) con el coche de delante al cerrar el jugador esa vuelta}
        self.pit_log = []
        self.k_pairs = []           # (desgaste_max, delta_ms de un juego nuevo) con el mismo montado
        self.k_fitted = None
        self.sc = 0
        self.weather = None
        self.num_red = 0
        self.prev_pit = 0
        self._pl = self._ps = 0
        self.best_s1 = self.best_s2 = None
        self.last_advice = None
        self.stint_first_lap = 0
        self.gaps_by_lap = {}       # vuelta -> {coche: tiempo acumulado respecto al líder}
        self.felt_grip = False      # el piloto dijo que no tiene agarre (se borra al cambiar neumáticos)
        self.limit_delta = 0.0      # puntos de desgaste que se restan al límite por lo que dice el piloto
        self.red_active = False
        self.red_t = 0.0
        self.fz = FuelZones()
        self._fz_loaded = False

    # ------------------------------------------------------------------ paquetes
    def on_packet(self, t, d):
        sig = []
        h = header(d)
        if h["session_uid"] == 0:
            return sig            # paquetes sueltos de menús con session_uid 0 (captura real: solo eventos / historial): no son una sesión
        ended = None
        if self.uid is not None and h["session_uid"] != self.uid:
            ended = copy.copy(self)                     # el estado anterior sigue vivo en la copia (reset() crea objetos nuevos)
            self.prev_kind = self.session_kind() if self.ses else getattr(self, "prev_kind", None)
            self.reset()
        self.uid, self.year, self.player = h["session_uid"], h["year"], h["player"]
        self.fmt = h["format"]
        if 0 <= h["session_time"] < 1e6:               # al acabar la sesión el juego manda 3.4e38 (máximo de float): se ignora
            self.t = h["session_time"]
        pid = h["id"]
        if ended is not None:
            sig.append(("session_end", ended))
        if pid == 2:
            self._lap(d, sig)
        elif pid == 7:
            self._status(d, sig)
        elif pid == 10:
            self._damage(d)
        elif pid == 6:
            self.tel_all = [car_telemetry(d, i) for i in range(NCARS)]
            self.traces.feed(self)
            if self.player < NCARS:
                self.tel = car_telemetry(d, self.player)
                if self.red_active and self.t - self.red_t > 5 and self.tel["speed"] > 100:
                    self.red_active = False        # heurística: el coche vuelve a rodar a ritmo de carrera
                me, sa = self.lap_all[self.player], self.sta_all[self.player]
                if me and sa and self.ses.get("track_length_m"):
                    clean = (me["pit_status"] == 0 and me["driver_status"] in (1, 4) and self.sc == 0
                             and me["result_status"] == 2 and sa["fia_flag"] not in (2, 3))
                    self.fz.sample(me["lap"], me["lap_distance"], self.ses["track_length_m"], sa["fuel_kg"], self.t,
                                   self.tel["throttle"], self.tel["speed"], self.tel["brake"], clean)
        elif pid == 1:
            self._session(d, sig)
        elif pid == 3:
            e = event(d)
            self.events.append((self.t, e))
            sig.append(("event", e))
        elif pid == 4:
            for c in participants(d)["cars"]:
                self.names[c["idx"]] = c["name"].title()
                self.team[c["idx"]] = c["team_id"]
        elif pid == 11:
            x = session_history(d)
            self.hist[x["car"]] = x
        elif pid == 12:
            x = tyre_sets(d)
            self.sets[x["car"]] = x
            if x["car"] == self.player:
                self._k_sample(x)
        elif pid == 8:
            self.final = final_classification(d)
            sig.append(("final", self.final))
        return sig

    def _session(self, d, sig):
        s = session(d)
        old = self.ses
        self.ses = s
        if not self._fz_loaded:                      # zonas de ahorro aprendidas en otras sesiones de esta pista
            self._fz_loaded = True
            self.fz.load(self.profile.get("tracks", {}).get(str(s["track_id"]), {}).get("fuel_zones"))
        if s["safety_car_status"] != self.sc:
            sig.append(("sc", s["safety_car_status"], self.sc))
            self.sc = s["safety_car_status"]
        if old and s["weather"] != old.get("weather"):
            sig.append(("weather", s["weather"]))
        if s["num_red"] > self.num_red:
            self.red_active, self.red_t = True, self.t
            sig.append(("red_flag",))
        self.num_red = s["num_red"]

    def _lap(self, d, sig):
        self.lap_all = [lap_data(d, i) for i in range(NCARS)]
        me = self.lap_all[self.player] if self.player < NCARS else None
        if not me:
            return
        sta = self.sta_all[self.player]
        if self.sc:
            self.lap_sc = True                        # vuelta con Safety Car/VSC: gasta mucho menos y no sirve para el margen
        if me["lap"] != self.cur_lap and me["lap"] > 0:
            fuel = sta["fuel_kg"] if sta else None
            if self.fuel_lap_start is not None and fuel is not None and self.cur_lap > 0 and self.prev_pit == 0 and not self.lap_sc:
                used = self.fuel_lap_start - fuel
                if 0 < used < 15:
                    self.burn.append(used)
            self.fuel_lap_start = fuel
            self.lap_sc = bool(self.sc)
            prev, self.cur_lap = self.cur_lap, me["lap"]
            self._record_gaps(prev)
            sig.append(("lap", me["lap"], prev))
        pit = 0 if me["pit_status"] == 0 else 1
        if pit:
            self._pl, self._ps = max(self._pl, me["pit_lane_ms"]), max(self._ps, me["pit_stop_ms"])
        if pit and not self.prev_pit:
            sig.append(("pit_in",))
        elif self.prev_pit and not pit:
            rec = {"lane_s": self._pl / 1000, "stop_s": self._ps / 1000, "lap": me["lap"]}
            self.pit_log.append(rec)
            self._pl = self._ps = 0
            sig.append(("pit_out", rec))
        self.prev_pit = pit

    def _status(self, d, sig):
        self.sta_all = [car_status(d, i) for i in range(NCARS)]
        if self.player >= NCARS:
            return
        sa = self.sta_all[self.player]
        age, comp = sa["tyre_age_laps"], sa["visual_compound"]
        if self.last_age is not None and (age < self.last_age or comp != self.last_comp):
            if self.stint and self.last_age >= 1:
                self.stint_log.append({"comp": self.last_comp, "age": self.last_age, "wear": self.stint[-1][1]})
            self.stint = []
            self.k_pairs, self.k_fitted = [], None
            self.stint_first_lap = self.cur_lap       # la vuelta de salida y la anterior no cuentan para medir ritmo
            self.felt_grip, self.limit_delta = False, 0.0
            sig.append(("tyres", comp))
        self.last_age, self.last_comp = age, comp

    def _damage(self, d):
        self.dmg_all = [car_damage(d, i) for i in range(NCARS)]
        sa = self.sta_all[self.player] if self.player < NCARS else None
        if sa is None:
            return
        dm = self.dmg_all[self.player]
        aero = max(dm["front_left_wing"], dm["front_right_wing"], dm["rear_wing"], dm["floor"], dm["diffuser"], dm["sidepod"])
        if aero >= self.dmg_aero + 5:                   # golpe nuevo: desde esta vuelta se mide cuánto pierdes
            self.dmg_onset, self.dmg_aero = self.cur_lap, aero
        elif aero < self.dmg_aero - 5:                  # reparado en boxes
            self.dmg_onset, self.dmg_aero = None, aero
        wear = max(self.dmg_all[self.player]["tyre_wear_pct"])
        age_f = sa["tyre_age_laps"] + self.lap_frac()
        if not self.stint or age_f >= self.stint[-1][0] + 0.04:
            self.stint.append((age_f, wear))
            if len(self.stint) > 900:
                del self.stint[:100]

    def _k_sample(self, x):
        """Mide cuánto ritmo se pierde por % de desgaste: el delta de un juego nuevo respecto al montado
        baja a medida que el montado se gasta (en tu captura: ~27 ms por % de desgaste)."""
        if self.dmg_all[self.player] is None:
            return
        fidx = x["fitted_idx"]
        if fidx != self.k_fitted:
            self.k_fitted, self.k_pairs = fidx, []
        fitted = x["sets"][fidx] if fidx < len(x["sets"]) else None
        ref = [s for i, s in enumerate(x["sets"]) if s["available"] and not s["fitted"] and s["visual"] in S.DRY]
        if not ref or not fitted:
            return
        wear = max(self.dmg_all[self.player]["tyre_wear_pct"])
        # se usa siempre el juego de referencia con el delta más estable: el primero disponible
        self.k_pairs.append((wear, ref[0]["lap_delta_ms"] / 1000))
        if len(self.k_pairs) > 400:
            del self.k_pairs[:100]

    # ------------------------------------------------------------------ derivados
    def session_kind(self):
        """race | quali | practice | tt | other, según el tipo de sesión del juego (F1 25: 1-4 práctica, 5-14 clasificación, 15-17 carrera)."""
        st = (self.ses or {}).get("session_type")
        if st in RACE_TYPES:
            return "race"
        if st in range(5, 15):
            return "quali"
        if st in range(1, 5):
            return "practice"
        return "tt" if st == TIME_TRIAL else "other"

    def is_race(self):
        return bool(self.ses) and self.ses.get("session_type") in RACE_TYPES

    def lap_frac(self):
        me = self.lap_all[self.player] if self.player is not None and self.player < NCARS else None
        tl = self.ses.get("track_length_m") if self.ses else None
        if not me or not tl:
            return 0.0
        return min(max(me["lap_distance"] / tl, 0.0), 1.0)

    def laps_left(self):
        me = self.lap_all[self.player]
        total = self.ses.get("total_laps", 0)
        if not me or not total:
            return None
        return max(total - (me["lap"] - 1) - self.lap_frac(), 0.01)

    def fuel_margin(self):
        sa = self.sta_all[self.player]
        ll = self.laps_left()
        if not sa or ll is None or not self.burn:
            return None
        avg = statistics.median(self.burn[-5:])         # mediana de las últimas vueltas en verde: una vuelta rara no mueve el margen
        return sa["fuel_kg"] / avg - ll if avg > 0 else None

    def stint_rate(self):
        """%/vuelta de desgaste de la peor rueda en el stint actual (None hasta tener ~1.5 vueltas)."""
        s = self.stint[-300:]
        if len(s) < 8 or s[-1][0] - s[0][0] < 1.5:
            return None
        sl = _slope([a for a, _ in s], [w for _, w in s])
        return sl if sl and sl > 0.05 else None

    def pace_loss_k(self):
        pairs = self.k_pairs
        if len(pairs) < 6 or max(w for w, _ in pairs) - min(w for w, _ in pairs) < 3:
            return None
        sl = _slope([w for w, _ in pairs], [d for _, d in pairs])
        return -sl if sl is not None and 0.005 < -sl < 0.08 else None

    def pace(self, i):
        """Mediana de las últimas 3 vueltas limpias del stint actual del coche i (s), o None."""
        h = self.hist.get(i)
        if h and h["laps"]:
            laps = h["laps"]
            stops = [s["end_lap"] for s in h["stints"] if s["end_lap"] != 255]
            first = max([e + 2 for e in stops], default=1)
            if i == self.player:                      # el historial llega con retraso tras una parada
                first = max(first, self.stint_first_lap)
            cand = [l["ms"] for j, l in enumerate(laps) if l["ms"] > 0 and j >= max(first, 1)]
            if not cand and not stops:
                cand = [l["ms"] for j, l in enumerate(laps) if l["ms"] > 0 and j >= 1]
            if cand:
                lo = min(cand)
                ok = [x for x in cand if x <= lo * 1.07][-3:]
                return statistics.median(ok) / 1000
        return None

    def gaps(self):
        """Tiempo acumulado (s) respecto al líder, encadenando el hueco con el coche de delante."""
        cars = sorted((c["position"], i) for i, c in enumerate(self.lap_all)
                      if c and c["result_status"] == 2 and c["position"] > 0)
        g, acc = {}, 0.0
        for k, (_, i) in enumerate(cars):
            if k:
                acc += self.lap_all[i]["delta_front_ms"] / 1000
            g[i] = acc
        return g, [i for _, i in cars]

    def neighbours(self):
        """(coche delante, hueco s), (coche detrás, hueco s) del jugador; None si no hay."""
        g, order = self.gaps()
        if self.player not in order:
            return None, None
        k = order.index(self.player)
        ahead = (order[k - 1], g[self.player] - g[order[k - 1]]) if k > 0 else None
        behind = (order[k + 1], g[order[k + 1]] - g[self.player]) if k + 1 < len(order) else None
        return ahead, behind

    def _record_gaps(self, prev_lap):
        a, b = self.neighbours()
        self.gaps_by_lap[prev_lap] = self.gaps()[0]
        order = sorted((c["position"], i) for i, c in enumerate(self.lap_all) if c and c["result_status"] == 2 and c["position"] > 0)
        for k, (_, i) in enumerate(order):
            if k:
                self.ahead_hist.setdefault(i, {})[prev_lap] = self.lap_all[i]["delta_front_ms"] / 1000
        for k in [k for k in self.gaps_by_lap if k < prev_lap - 8]:
            del self.gaps_by_lap[k]
        self.gap_hist[prev_lap] = {"front": a[1] if a else None, "behind": b[1] if b else None,
                                   "front_car": a[0] if a else None, "behind_car": b[0] if b else None}
        for k in [k for k in self.gap_hist if k < prev_lap - 6]:
            del self.gap_hist[k]

    def dirty_air(self):
        """Pérdida medida por ir pegado a otro coche (ver timing.dirty_air); None sin muestras suficientes."""
        import timing
        times = {i: {n + 1: l["ms"] for n, l in enumerate(h["laps"])} for i, h in self.hist.items()}
        return timing.dirty_air(self.ahead_hist, times)

    def name(self, i):
        return self.names.get(i) or f"coche {i}"

    def used_compounds(self, i):
        h = self.hist.get(i)
        used = set()
        if h:
            used |= {s["visual"] for s in h["stints"] if s["visual"] in S.DRY}
        sa = self.sta_all[i]
        if sa and sa["visual_compound"] in S.DRY:
            used.add(sa["visual_compound"])
        la = self.lap_all[i]
        if la and la["pit_stops"] >= 1 and len(used) < 2:
            used.add(S.OTHER)       # paró pero no conocemos el historial: se asume regla cumplida
        return used

    def damage_loss(self):
        """s/vuelta que te cuesta el daño, MEDIDO: tus vueltas tras el golpe frente a las de antes (sin la vuelta del golpe ni las
        de boxes, descontando lo que suma el desgaste). None hasta tener 2 vueltas de cada lado."""
        h = self.hist.get(self.player)
        if self.dmg_onset is None or not h:
            return None
        pits = {r["lap"] + d for r in self.pit_log for d in (-1, 0)}
        laps = [(n, l["ms"]) for n, l in enumerate(h["laps"], 1) if l["ms"] > 0 and l["valid"] and n >= 2 and n not in pits]
        pre = [x for x in laps if x[0] < self.dmg_onset][-3:]
        post = [x for x in laps if x[0] > self.dmg_onset][:3]
        if len(pre) < 2 or len(post) < 2:
            return None
        drift = self.params.pace_loss_per_wear * (self.stint_rate() or 0.0) * (
            statistics.mean(n for n, _ in post) - statistics.mean(n for n, _ in pre))
        return max(0.0, (statistics.median(m for _, m in post) - statistics.median(m for _, m in pre)) / 1000 - drift)

    def pit_loss(self):
        samples = measure_pit_loss(self.hist)
        if len(samples) >= 2:
            return statistics.median(samples), "medido en esta carrera"
        mem = getattr(self, "memory", None)
        v = mem.pit_loss(self.ses.get("track_id")) if mem is not None else None
        if v:
            return v, "medido en sesiones anteriores en este circuito"
        tr = self.profile.get("tracks", {}).get(str(self.ses.get("track_id")))
        if tr and tr.get("pit_loss_s"):
            return tr["pit_loss_s"], "perfil aprendido"
        return self.params.pit_loss_s, "valor por defecto"

    def low_workload(self):
        """True si el piloto está en una recta a fondo (buen momento para hablarle)."""
        te = self.tel
        return bool(te and te["throttle"] >= 0.95 and te["brake"] <= 0.01 and te["speed"] >= 180 and abs(te["steer"]) < 0.12)

    @staticmethod
    def plain(x):
        return "".join(c for c in unicodedata.normalize("NFD", x.lower()) if unicodedata.category(c) != "Mn")

    def find_driver(self, text):
        """Pilotos mencionados en el texto: [(idx, nombre)]. Tolera errores de transcripción: un nombre exacto gana
        a uno parecido, y entre varios parecidos solo se elige si uno destaca con claridad (si no, es ambiguo)."""
        words = [w for w in re.findall(r"\w+", self.plain(text)) if len(w) >= 3]
        scored = []
        for i in range(NCARS):
            nm = self.names.get(i)
            if not nm or i == self.player:
                continue
            target = self.plain(nm)
            sc = 0.0
            for w in words:
                if w == target:
                    sc = 1.0
                    break
                if len(target) >= 5 and target in w:           # «motoverstappen»: el reconocedor pega palabras al apellido
                    sc = max(sc, 0.95)
                # parecido (error de transcripción): misma inicial, longitud casi igual y no una palabra común
                # ('queda' no es 'Ueda', 'quiero' no es 'Quintero')
                if w[0] == target[0] and abs(len(w) - len(target)) <= 1 and w not in COMMON_WORDS:
                    sc = max(sc, difflib.SequenceMatcher(None, w, target).ratio())
            if sc >= 0.8:
                scored.append((sc, i, nm))
        if not scored:
            return []
        scored.sort(reverse=True)
        top = scored[0][0]
        if top == 1.0:
            return [(i, nm) for sc, i, nm in scored if sc == 1.0]
        near = [(i, nm) for sc, i, nm in scored if top - sc < 0.05]
        return near

    def rel_gap(self, idx):
        """Hueco (s) de ese coche respecto a ti ahora: < 0 va delante. None si no hay datos."""
        g, _ = self.gaps()
        if idx not in g or self.player not in g:
            return None
        return g[idx] - g[self.player]

    def gap_trend(self, idx, laps=2):
        """Cuánto ha cambiado el hueco con ese coche en las últimas vueltas (s; + = se acerca a ti)."""
        now = self.rel_gap(idx)
        past = [l for l in sorted(self.gaps_by_lap) if idx in self.gaps_by_lap[l] and self.player in self.gaps_by_lap[l]]
        if now is None or len(past) < laps:
            return None
        g = self.gaps_by_lap[past[-laps]]
        before = g[idx] - g[self.player]
        return abs(before) - abs(now)

    # ------------------------------------------------------------------ entrada de la estrategia
    def build_inputs(self, include=()):
        """(Situation, Me, [Rival], info) o (None, None, None, motivo)."""
        if not self.is_race():
            return None, None, None, "no es una carrera"
        ses, p = self.ses, self.params
        if self.player is None or self.player >= NCARS:
            return None, None, None, "sin jugador"
        me_l, sa, dm = self.lap_all[self.player], self.sta_all[self.player], self.dmg_all[self.player]
        if not (me_l and sa and dm) or me_l["lap"] < 1 or me_l["result_status"] != 2:
            return None, None, None, "faltan datos del coche"
        if ses["safety_car_status"] == 3:
            return None, None, None, "vuelta de formación"
        if me_l["pit_status"] != 0 or me_l["driver_status"] in (0, 2):
            return None, None, None, "en boxes"
        ll = self.laps_left()
        total = ses["total_laps"]
        if ll is None or not total:
            return None, None, None, "sin vueltas totales"
        k = self.pace_loss_k()
        if k:
            p.pace_loss_per_wear = k
        wear = max(dm["tyre_wear_pct"])
        comp = sa["visual_compound"]
        sets_x = self.sets.get(self.player)
        infos, usable, life_game = [], {}, None
        if sets_x:
            for i, s in enumerate(sets_x["sets"]):
                if s["visual"] in S.NAME and s["usable_life"]:
                    usable.setdefault(s["visual"], float(s["usable_life"]))
                if s["fitted"] and i == sets_x["fitted_idx"]:
                    life_game = float(s["life_span"]) if s["life_span"] else None
                elif s["available"] and s["visual"] in S.DRY + (7, 8):
                    infos.append(S.SetInfo(s["visual"], float(s["wear"]), float(s["life_span"]),
                                           s["lap_delta_ms"] / 1000))
        aero = max(dm["front_left_wing"], dm["front_right_wing"])
        measured = self.damage_loss()                     # con 2 vueltas tras el golpe se usa lo medido, no la estimación
        rate = self.stint_rate()
        if rate is None:                                 # sin medida aún: ritmo aprendido de esta pista, si lo hay
            w = self.profile.get("tracks", {}).get(str(ses.get("track_id")), {}).get("wear", {}).get(str(comp))
            if w and w.get("rate_n", 0) >= 3:
                rate = w["rate"]
        me = S.Me(pos=me_l["position"], compound=comp, age=sa["tyre_age_laps"] + self.lap_frac(), wear=wear,
                  pace=self.pace(self.player), rate=rate, life_game=life_game, sets=infos,
                  usable=usable, used=self.used_compounds(self.player), stops=me_l["pit_stops"],
                  dmg_pace_s=(measured if measured is not None else (aero * p.aero_loss_per_pct if aero >= 5 else 0.0))
                  + max(tyre_extra_damage(dm)) * p.tyre_damage_loss_per_pct * (1 if max(tyre_extra_damage(dm)) >= 10 else 0),
                  dmg_measured=measured is not None,
                  felt_loss_s=(p.felt_grip_loss_s if self.felt_grip else 0.0),
                  pen_pending=("drive-through" if me_l["unserved_dt"] else ("stop-go" if me_l["unserved_sg"] else None)))
        g, order = self.gaps()
        if self.player not in order:
            return None, None, None, "fuera de la clasificación"
        mypos = order.index(self.player)
        # rivales: todos los activos (con paradas obligatorias casi todos cambian de posición; simular solo a los cercanos sesga)
        pick = set(order) | set(include)       # todos los coches: los que van delante también paran y cambian el orden final
        pick.discard(self.player)
        rivals = []
        for i in order:
            if i not in pick:
                continue
            rs, rd, rl = self.sta_all[i], self.dmg_all[i], self.lap_all[i]
            if not (rs and rd and rl) or rs["visual_compound"] not in S.NAME:
                continue
            age = rs["tyre_age_laps"]
            w = max(rd["tyre_wear_pct"])
            rivals.append(S.Rival(i, self.name(i), rl["position"], g[i] - g[self.player], self.pace(i),
                                  rs["visual_compound"], age, w, (w / age if age >= 2 else None),
                                  rl["pit_stops"], self.used_compounds(i)))
        ignored = max(0, mypos - sum(1 for r in rivals if r.pos < me.pos))
        pit_loss, pit_src = self.pit_loss()
        fc = ses.get("forecast") or []
        rain_next = max([f["rain_pct"] for f in fc if f["minutes"] <= 15], default=0)
        wet = ses["weather"] >= 3 or comp in (7, 8)
        sit = S.Situation(total_laps=total, lap=me_l["lap"], laps_left=ll, sc=ses["safety_car_status"] if
                          ses["safety_car_status"] in (1, 2) else 0, sc_setting=ses["safety_car_setting"],
                          wet=wet, pit_loss=pit_loss, ignored_ahead=ignored, year=self.year or 2025,
                          red=self.red_active)
        adj = replace(p, wear_limit=p.wear_limit - self.limit_delta) if self.limit_delta else p
        info = {"pit_loss_src": pit_src, "rain_next_pct": rain_next, "rivals": len(rivals), "ignored_ahead": ignored,
                "params": adj}
        return sit, me, rivals, info

    # ------------------------------------------------------------------ resumen para el LLM / chat
    def summary(self):
        """Estado compacto (JSON-able) con números ya redondeados: es lo único que ve el modelo."""
        if self.player is None or self.player >= NCARS or not self.lap_all[self.player]:
            return {}
        me, sa, dm, te = self.lap_all[self.player], self.sta_all[self.player], self.dmg_all[self.player], self.tel
        ses = self.ses or {}
        a, b = self.neighbours()
        out = {"vuelta": me["lap"], "vueltas_totales": ses.get("total_laps"), "posicion": me["position"],
               "paradas": me["pit_stops"]}
        if ses.get("safety_car_status") in (1, 2):
            out["safety_car"] = "SC" if ses["safety_car_status"] == 1 else "VSC"
        if a:
            out["delante"] = {"piloto": self.name(a[0]), "hueco_s": round(a[1], 1)}
        if b:
            out["detras"] = {"piloto": self.name(b[0]), "hueco_s": round(b[1], 1)}
        if sa:
            out["neumatico"] = {"compuesto": S.NAME.get(sa["visual_compound"], "?"), "vueltas_de_uso": sa["tyre_age_laps"]}
            out["combustible_kg"] = round(sa["fuel_kg"], 1)
            out["ers_pct"] = round(sa["ers_store_j"] / 4_000_000 * 100)
            out["drs_disponible"] = sa["drs_allowed"]
        if dm:
            out["desgaste_pct"] = {WHEELS[k]: round(v) for k, v in enumerate(dm["tyre_wear_pct"])}
            out["dano_aleron_delantero_pct"] = max(dm["front_left_wing"], dm["front_right_wing"])
        fm = self.fuel_margin()
        if fm is not None:
            out["margen_combustible_vueltas"] = round(fm, 1)
        if te:
            out["velocidad_kmh"] = te["speed"]
        if self.last_advice:
            out["ultima_estrategia"] = self.last_advice
        return out
