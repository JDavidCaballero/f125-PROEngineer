#!/usr/bin/env python3
"""Alertas por reglas (sin IA): frases fijas, instantáneas y siempre coherentes con los datos.

Tactics.poll(tracker, señales) devuelve una lista de Alert. Las frases que ya habías probado
("Entra a boxes: desgaste del ...", "Combustible justo: te faltan ...") se conservan tal cual.
Cada alerta tiene prioridad (1 crítica, 2 importante, 3 informativa), clave y enfriamiento en segundos
de sesión; el Engineer decide cuándo hablar.
"""
from dataclasses import dataclass

import strategy as S
from f1_parse import WEATHER, NCARS, tyre_extra_damage

WHEELS = ["trasero izquierdo", "trasero derecho", "delantero izquierdo", "delantero derecho"]
# con su artículo: «Daño en la caja de cambios», «Desgaste de la batería»
PARTS = {"front_left_wing": "el alerón delantero izquierdo", "front_right_wing": "el alerón delantero derecho",
         "rear_wing": "el alerón trasero", "floor": "el fondo plano", "diffuser": "el difusor", "sidepod": "el pontón",
         "gearbox": "la caja de cambios", "engine": "el motor"}
COMPONENTS = {"mguh_wear": "el MGU-H", "es_wear": "la batería (ES)", "ce_wear": "la centralita (CE)",
              "ice_wear": "el motor de combustión (ICE)", "mguk_wear": "el MGU-K", "tc_wear": "el control de tracción (TC)"}
PENALTY = {0: "Sanción: drive-through. Cúmplelo en boxes en las próximas 3 vueltas.",
           1: "Sanción: stop-and-go. Cúmplelo en boxes en las próximas 3 vueltas.",
           6: "Descalificado.", 16: "Retirado."}   # tiempo (4) y aviso (5) salen de LapData, sin duplicar
INVALID_LAP = {10, 11, 12, 13, 14}


@dataclass
class Alert:
    text: str
    prio: int = 2
    key: str = ""
    cooldown: float = 0.0


# Temas que el piloto puede subir o bajar ("menos avisos de combustible"): clave de la alerta -> tema
TOPIC_OF = {"fuel": "combustible", "fuelzone": "combustible", "mix": "combustible", "erslow": "ers", "drs": "drs",
            "sec1": "sectores", "sec2": "sectores", "sec3": "sectores",
            "attack": "adelantamiento", "defend": "adelantamiento", "defend_drs": "adelantamiento",
            "rivalpit": "rivales", "rivalwear": "rivales", "fuelok": "combustible"}
TOPICS = ("combustible", "ers", "drs", "sectores", "adelantamiento", "rivales", "alternativas", "vueltas")


def mmss(s):
    return f"{int(s // 60)}:{s % 60:06.3f}"


class Tactics:
    def __init__(self, cfg):
        self.th = cfg.get("thresholds", {})
        self.year = cfg.get("engineer", {}).get("game_year", 2025)
        self.lap_summary = cfg.get("engineer", {}).get("lap_summary", True)
        self.levels = {}
        self.topics = {}          # tema -> -1 (menos avisos), 0 normal, +1 (más avisos); lo cambia el piloto por voz
        self.prev = {}
        self.last_sector = 0
        self.warn_seen = 0
        self.pen_seen = 0
        self.fuel_lap = None
        self.fuel_said = None     # déficit (vueltas) del último «combustible justo»: se repite solo si empeora
        self.erslow_said = False  # «batería baja» una vez por episodio: se rearma al recargar
        self.erslow_lap = -99
        self.wet_said = False     # «pista mojada» una vez por episodio: se rearma al montar mojado o secarse
        self.lapsum_prev = -99    # última vuelta con resumen hablado
        self.tyre_hot = {}        # (tipo, rueda) -> {peak, lap, age}: avisos de temperatura dinámicos por juego de neumáticos
        self.defend_ep, self.attack_ep = {}, {}   # coche -> episodio de duelo ya avisado
        self._ovtk_last = {}                      # coche -> (sentido, tiempo) del último adelantamiento que se dijo
        self.drs_seen = {}        # coche al que se atacó con DRS -> vuelta del último aviso (None = DRS sin rival cerca)
        self.rain_alerted = None  # (probabilidad, minutos) del último aviso de lluvia
        self.last_weather = None

    # ----------------------------------------------------------------- utilidades
    def _level(self, key, value, levels, make):
        """levels: [(umbral, nombre)] de mayor a menor. Avisa solo al subir de nivel; se rearma al bajar."""
        for rank_from_top, (thr, name) in enumerate(levels):
            if value >= thr:
                rank = len(levels) - rank_from_top
                if self.levels.get(key, 0) < rank:
                    self.levels[key] = rank
                    return make(name, value)
                return None
        self.levels[key] = 0
        return None

    def _rising(self, key, cond):
        was = self.prev.get(key, False)
        self.prev[key] = cond
        return cond and not was

    # ----------------------------------------------------------------- señales
    def on_signals(self, tr, signals):
        out = []
        for s in signals:
            kind = s[0]
            if kind == "sc":
                new, old = s[1], s[2]
                if new == 1:
                    out.append(Alert("¡Safety Car desplegado! Ventana de boxes abierta.", 1, "sc", 5))
                elif new == 2:
                    out.append(Alert("Virtual Safety Car (VSC) activo. Mantén tu delta.", 1, "sc", 5))
                elif new == 0 and old in (1, 2):
                    out.append(Alert("Pista despejada. Bandera verde.", 1, "sc", 5))
            elif kind == "red_flag":
                out.append(Alert("Bandera roja: puedes cambiar neumáticos sin perder tiempo en boxes.", 1, "red", 30))
            elif kind == "tyres":
                for k in [k for k in self.levels if k.startswith("wear") or k.startswith("tyredmg")]:
                    self.levels[k] = 0
                comp = S.NAME.get(s[1])
                out.append(Alert(f"Neumáticos nuevos puestos ({comp}), buen trabajo." if comp
                                 else "Neumáticos nuevos puestos, buen trabajo.", 2, "tyres", 10))
            elif kind == "pit_out":
                r = s[1]
                if r["stop_s"] > 0:
                    out.append(Alert(f"Parada completada: {r['stop_s']:.1f} s parado, {r['lane_s']:.0f} s en el pit lane.",
                                     2, "pitout", 10))
            elif kind == "weather":
                prev_w, self.last_weather = self.last_weather, s[1]
                if s[1] >= 3 or (prev_w is not None and prev_w >= 3):          # solo cambios con lluvia: «poco nublado» no cambia nada
                    out.append(Alert(f"Cambia el tiempo: {WEATHER.get(s[1], '?')}.", 2, "wchange", 20))
            elif kind == "event":
                out += self._event(tr, s[1])
            elif kind == "lap":
                out += self._on_lap(tr, s[1], s[2])
            elif kind == "final":
                out += self._final(tr, s[1])
            elif kind == "finish":
                out += self._finish(tr, s[1])
        return out

    def _event(self, tr, e):
        out, code, me = [], e["code"], tr.player
        if code == "OVTK":
            if e["car"] == me:
                if self._ovtk_new(tr, e["overtaken"], "to"):
                    out.append(Alert(f"Adelantaste a {tr.name(e['overtaken'])}.", 3, f"ovtk_to{e['overtaken']}", 3))
            elif e["overtaken"] == me:
                if self._ovtk_new(tr, e["car"], "by"):
                    out.append(Alert(f"{tr.name(e['car'])} te adelantó.", 3, f"ovtk_by{e['car']}", 3))
        elif code == "PENA" and e.get("car") == me:
            t = PENALTY.get(e["penalty_type"])
            if t:
                out.append(Alert(t, 1, "pena", 5))
            elif e["penalty_type"] in INVALID_LAP:
                out.append(Alert("Vuelta invalidada.", 2, "invalid", 10))
        elif code == "COLL" and me in (e.get("car"), e.get("other_car")):
            other = e["other_car"] if e["car"] == me else e["car"]
            out.append(Alert(f"Contacto con {tr.name(other)}: revisa los daños.", 2, "coll", 45))
        elif code == "RTMT" and e.get("car") != me:
            la = tr.lap_all[e["car"]] if e["car"] < NCARS else None
            mine = tr.lap_all[me] if me < NCARS else None
            if la and mine and la["position"] and la["position"] < mine["position"]:
                out.append(Alert(f"Se retira {tr.name(e['car'])}.", 3, "rtmt", 3))
        elif code == "FTLP":
            if not (0 < e["time_s"] < 1200):                          # tiempo basura (visto: 307445749164714:40.000 en menús)
                return out
            if e["car"] == me:
                out.append(Alert(f"¡Tu vuelta rápida! {mmss(e['time_s'])}.", 3, "ftlp", 5))
            elif not tr.is_race():                                    # en carrera la vuelta rápida de otro no cambia nada; en clasificación sí es la referencia
                out.append(Alert(f"Vuelta rápida de {tr.name(e['car'])}: {mmss(e['time_s'])}.", 3, "ftlp", 5))
        elif code == "CHQF":
            out.append(Alert("Bandera a cuadros.", 2, "chqf", 30))
        return out

    def _ovtk_new(self, tr, car, way):
        """¿Hay algo nuevo que decir de este adelantamiento? El mismo piloto en el MISMO sentido no se repite en 20 s (vuelta 1 del sprint del 11 nov:
        «Verstappen te adelantó» tres veces en 12 s), pero un CAMBIO de sentido siempre se dice: si te pasa y lo recuperas, el piloto debe saberlo."""
        now = getattr(tr, "t", None)
        last = self._ovtk_last.get(car)
        if last and last[0] == way and now is not None and last[1] is not None and now - last[1] < 20:
            return False
        self._ovtk_last[car] = (way, now)
        return True

    def _finish(self, tr, pos):
        """Cruzas la meta: el resultado se dice aunque el juego no mande la clasificación final (en el sprint del 11 nov nunca llegó)."""
        if not tr.is_race() or not pos:
            return []
        h = tr.hist.get(tr.player)
        laps = [l["ms"] for l in (h["laps"] if h else []) if l["ms"] > 0]
        best = f" Tu mejor vuelta: {mmss(min(laps) / 1000)}." if laps else ""
        return [Alert(f"Terminas P{pos}.{best}", 2, "finish", 60)]

    def _final(self, tr, fin):
        me = next((c for c in fin["cars"] if c["idx"] == tr.player), None)
        if not me:
            return []
        # la clasificación final llega a veces cuando ya empezó la sesión siguiente (sin datos): se usa el tipo de la anterior
        kind = tr.session_kind() if tr.ses else getattr(tr, "prev_kind", None)
        if kind and kind != "race":
            return [Alert("Fin de la sesión.", 2, "final", 60)]          # entrenos y clasificación: sin puntos ni «fin de carrera»
        return [Alert(f"Fin de carrera: terminas P{me['position']}, {me['points']} puntos.", 1, "final", 60)]

    def _on_lap(self, tr, lap, prev):
        out = []
        if not tr.is_race() or prev < 1:
            return out
        h = tr.hist.get(tr.player)
        me = tr.lap_all[tr.player]
        last = me["last_ms"] if me and me["last_ms"] > 0 else None
        gh = tr.gap_hist.get(prev, {})
        # tendencia de los huecos respecto a la vuelta anterior
        pg = tr.gap_hist.get(prev - 1, {})
        if gh.get("behind") is not None and pg.get("behind") is not None and gh["behind_car"] == pg["behind_car"]:
            d = pg["behind"] - gh["behind"]
            if 0.3 <= d <= 3.0 and 0.05 < gh["behind"] <= 4.0 and pg["behind"] <= 10.0:
                out.append(Alert(f"{tr.name(gh['behind_car'])} recorta {d:.1f} s por vuelta, está a {gh['behind']:.1f} s.",
                                 2, "closing_b", 20))
        if gh.get("front") is not None and pg.get("front") is not None and gh["front_car"] == pg["front_car"]:
            d = pg["front"] - gh["front"]
            if 0.3 <= d <= 3.0 and 0.05 < gh["front"] <= 4.0 and pg["front"] <= 10.0:
                out.append(Alert(f"Recortas {d:.1f} s por vuelta a {tr.name(gh['front_car'])}, está a {gh['front']:.1f} s.",
                                 2, "closing_f", 20))
        if self.lap_summary and last:
            hist = [l["ms"] for l in (h["laps"] if h else []) if l["ms"] > 0]
            best = min(hist + [last])
            txt = f"Vuelta {prev}: {mmss(last / 1000)}"
            is_best = last <= best and len(hist) >= 1 and prev >= 3
            if is_best:
                txt += " (tu mejor)"
            txt += f", P{me['position']}."
            if gh.get("front") is not None and gh["front"] >= 0.05:
                txt += f" Delante {tr.name(gh['front_car'])} a {gh['front']:.1f} s."
            if gh.get("behind") is not None and gh["behind"] >= 0.05:
                txt += f" Detrás {tr.name(gh['behind_car'])} a {gh['behind']:.1f} s."
            if is_best or prev - self.lapsum_prev >= 3:          # resumen cada 3 vueltas o cuando es tu mejor (antes: todas las vueltas)
                self.lapsum_prev = prev
                out.append(Alert(txt, 3, "lapsum", 0))
        return out

    # ----------------------------------------------------------------- estado continuo
    def poll(self, tr, signals):
        out = self._poll(tr, signals)
        return [a for a in out if self.topics.get(TOPIC_OF.get(a.key), 0) >= 0]      # tema silenciado por el piloto

    def _poll(self, tr, signals):
        out = self.on_signals(tr, signals)
        if tr.player is None or tr.player >= NCARS:
            return out
        me, sa, dm, te = tr.lap_all[tr.player], tr.sta_all[tr.player], tr.dmg_all[tr.player], tr.tel
        if not (me and sa and dm):
            return out
        if me["result_status"] != 2 or me["driver_status"] == 0:
            return out
        race = tr.is_race()
        th = self.th
        add = out.append

        # --- neumáticos
        worst = max(range(4), key=lambda k: dm["tyre_wear_pct"][k])
        wv = dm["tyre_wear_pct"][worst]
        proj_ok = hold = False
        if race:
            ll, rate = tr.laps_left(), tr.stint_rate()
            proj_ok = bool(ll and rate and wv + rate * ll <= th.get("wear_critical", 70))
            # a pocas vueltas del final una parada cuesta más que acabar con gomas gastadas (límite 85% estimado, no medido)
            hold = bool(ll is not None and ll <= 3 and wv + (rate or 0) * ll <= th.get("wear_finish_max", 85))
        levels = [(th.get("wear_critical", 70), "critico"), (th.get("wear_plan", 55), "aviso")]
        a = self._level("wear", wv, levels, lambda n, v: Alert(
            f"Desgaste del {WHEELS[worst]} al {v:.0f}%: quedan {max(1, round(tr.laps_left()))} vueltas, no merece la pena parar. Cuida las gomas."
            if n == "critico" and hold else
            f"Entra a boxes: desgaste del {WHEELS[worst]} al {v:.0f}%." if n == "critico"
            else f"Desgaste del {WHEELS[worst]} al {v:.0f}%, planea la parada.", 2 if hold or n != "critico" else 1,
            "wear", 20))
        if a and not (proj_ok and a.prio == 2):
            add(a)
        extra = tyre_extra_damage(dm)           # el daño que el juego repite del desgaste no se avisa dos veces
        for k in range(4):
            a = self._level(f"tyredmg{k}", extra[k], [(th.get("damage_severe", 50), "g"), (th.get("damage_light", 20), "l")],
                            lambda n, v, k=k: Alert(f"Daño en el neumático {WHEELS[k]}: {dm['tyre_damage_pct'][k]:.0f}%.", 2, f"tyredmg{k}", 30))
            if a:
                add(a)
        # --- daños
        lv = [(th.get("damage_severe", 50), "grave"), (th.get("damage_light", 20), "leve")]
        for key, name in PARTS.items():
            a = self._level("dmg_" + key, dm[key], lv, lambda n, v, name=name, key=key: Alert(
                f"Daño grave en {name}: {v:.0f}%." if n == "grave" else f"Daño en {name}: {v:.0f}%.",
                1 if n == "grave" else 2, "dmg_" + key, 30))
            if a:
                add(a)
        lv = [(th.get("component_severe", 80), "critico"), (th.get("component_warn", 50), "aviso")]
        for key, name in COMPONENTS.items():
            a = self._level("cmp_" + key, dm[key], lv, lambda n, v, name=name, key=key: Alert(
                f"Desgaste crítico de {name}: {v:.0f}%." if n == "critico" else f"Desgaste de {name} al {v:.0f}%.",
                1 if n == "critico" else 3, "cmp_" + key, 60))
            if a:
                add(a)
        if self._rising("drsfault", dm["drs_fault"]):
            add(Alert("Fallo de DRS.", 2, "drsfault", 60))
        if self._rising("ersfault", dm["ers_fault"]):
            add(Alert("Fallo de ERS.", 2, "ersfault", 60))
        if self._rising("blown", dm["engine_blown"]):
            add(Alert("¡Motor roto!", 1, "blown", 60))
        if self._rising("seized", dm["engine_seized"]):
            add(Alert("¡Motor gripado!", 1, "seized", 60))

        # --- temperaturas (valores imposibles = paquete corrupto: no se avisan)
        if te and te["engine_temp"] < 300 and max(te["brake_temp"]) < 2000 and te["speed"] < 420:
            if te["engine_temp"] >= th.get("engine_temp_hot", 135):
                add(Alert(f"Motor caliente: {te['engine_temp']}°C, levanta un poco.", 2, "enginet", 60))
            hot = max(range(4), key=lambda k: te["brake_temp"][k])
            if te["brake_temp"][hot] >= th.get("brake_temp_hot", 1150):
                add(Alert(f"Frenos {WHEELS[hot]} al límite: {te['brake_temp'][hot]}°C.", 2, "braket", 45))
            for key, thr, nm in (("tyre_surface_temp", "tyre_surface_hot", "superficie"), ("tyre_inner_temp", "tyre_inner_hot", "interior")):
                k = max(range(4), key=lambda j: te[key][j])
                a = self._tyre_hot(tr, me, sa, key, k, te[key][k], th.get(thr, 999), nm)
                if a:
                    add(a)

        # --- banderas
        if sa["fia_flag"] == 2:
            add(Alert("Bandera azul: deja pasar.", 2, "blue", 25))
        elif sa["fia_flag"] == 3:
            add(Alert("Bandera amarilla en tu sector: no adelantes.", 2, "yellow", 45))
        ses = tr.ses or {}
        zones = ses.get("marshal_zones") or []
        if zones and ses.get("track_length_m"):
            f = tr.lap_frac()
            for k, (start, flag) in enumerate(zones):
                nxt = zones[k + 1][0] if k + 1 < len(zones) else 1.0
                if flag == 3 and 0 < (start - f) % 1.0 <= 0.08 and start >= f:
                    add(Alert("Amarilla adelante: prepárate para levantar.", 2, "yellowahead", 15))
                    break

        # --- sanciones y límites de pista
        if me["penalties_s"] > self.pen_seen:
            d = me["penalties_s"] - self.pen_seen
            add(Alert(f"Sanción de {d} s, llevas {me['penalties_s']} s acumulados.", 1, "pen_s", 0))
        self.pen_seen = me["penalties_s"]
        if me["unserved_dt"] > 0:
            add(Alert("Tienes un drive-through pendiente: cúmplelo en boxes.", 1, "dt", 90))
        if me["unserved_sg"] > 0:
            add(Alert("Tienes un stop-and-go pendiente: cúmplelo en boxes.", 1, "sg", 90))
        if me["warnings"] > self.warn_seen:
            add(Alert(f"Aviso por límites de pista: llevas {me['warnings']}.", 2, "warn", 5))
        self.warn_seen = me["warnings"]
        if self._rising("invalid", me["lap_invalid"]):
            add(Alert("Vuelta invalidada.", 3, "invalidlap", 10))

        # --- limitador en boxes
        if te and me["pit_status"] in (1, 2) and te["speed"] < 420:
            lim = ses.get("pit_speed_limit") or 0
            if lim and te["speed"] > lim + th.get("pit_limiter_margin_kmh", 3):
                add(Alert(f"¡Limitador! Vas a {te['speed']} km/h, el límite es {lim}.", 1, "limiter", 4))

        # --- clima
        add_w = self._weather(tr, sa)
        if add_w:
            add(add_w)
        # --- tramos
        out += self._sectors(tr, me)

        if race:
            out += self._race(tr, me, sa, th)
        return out

    def _corner_at(self, tr, me):
        """«la curva 3» donde está ahora el coche, con la última vuelta medida; None si no se sabe."""
        rec = tr.traces.last_timed(tr.player) or tr.traces.best.get(tr.player)
        tl = (tr.ses or {}).get("track_length_m")
        if rec is None or not tl:
            return None
        import quali as Q
        from traces import NB
        b = min(int(me["lap_distance"] / tl * NB), NB - 1)
        for s in Q.segments(rec):
            if s["kind"] == "corner" and s["b0"] <= b < s["b1"]:
                return s["name"]
        return None

    def _tyre_hot(self, tr, me, sa, key, k, v, thr, nm):
        """Temperatura de neumático: se avisa UNA vez por juego de neumáticos (con la curva donde se calienta) y de nuevo solo si
        sube 8 °C más; con neumáticos nuevos se rearma. Antes se repetía cada vuelta con el mismo valor."""
        if v < thr:
            return None
        age = sa["tyre_age_laps"]
        st = self.tyre_hot.get((key, k))
        if st and age < st["age"]:
            st = None                                          # neumáticos nuevos
        if st is not None and not (v >= st["peak"] + 8 and tr.cur_lap > st["lap"]):
            self.tyre_hot[(key, k)] = {**st, "age": age}
            return None
        first = st is None
        where = self._corner_at(tr, me) if first else None
        self.tyre_hot[(key, k)] = {"peak": max(v, st["peak"] if st else 0), "lap": tr.cur_lap, "age": age}
        txt = f"Neumático {WHEELS[k]} sobrecalentado ({nm}): {v}°C" + (f", sobre todo en {where}" if where else "")
        return Alert(txt + ("." if first else "; sigue subiendo."), 2, "tyret_" + key, 60)

    def _weather(self, tr, sa):
        ses = tr.ses or {}
        w, comp = ses.get("weather", 0), sa["visual_compound"]
        fc = ses.get("forecast") or []
        if not (w >= 3 and comp in S.DRY):
            self.wet_said = False                              # ya no aplica (se secó o montó mojado): si vuelve, se avisa de nuevo
        elif not self.wet_said:
            self.wet_said = True
            return Alert("Pista mojada: entra a cambiar a intermedios.", 1, "wet", 90)
        if w >= 4 and comp == 7:
            return Alert("Lluvia fuerte: considera neumáticos de lluvia.", 2, "heavy", 60)
        if w <= 1 and comp in (7, 8) and not any(f["rain_pct"] >= 50 for f in fc if f["minutes"] <= 15):
            return Alert("La pista se seca: considera neumáticos de seco.", 2, "dry", 60)
        if w < 3 and comp in S.DRY:
            soon = [f for f in fc if f["minutes"] <= 20 and f["rain_pct"] >= 60]
            if soon:
                f = min(soon, key=lambda x: x["minutes"])
                last = self.rain_alerted
                if last is None or f["rain_pct"] >= last[0] + 15 or (f["minutes"] <= 5 < last[1]):     # solo si empeora o se acerca
                    self.rain_alerted = (f["rain_pct"], f["minutes"])
                    when = "inminente" if f["minutes"] <= 2 else f"en unos {f['minutes']} min"
                    return Alert(f"Lluvia {when} ({f['rain_pct']}%): prepárate para intermedios.", 2, "rainsoon", 60)
            elif self.rain_alerted is not None:
                self.rain_alerted = None                       # la previsión se despejó: si vuelve, se avisa de nuevo
        return None

    def _sectors(self, tr, me):
        out = []
        sec = me["sector"]
        if sec != self.last_sector:
            h = tr.hist.get(tr.player)
            done = [l for l in (h["laps"] if h else []) if l["ms"] > 0 and l["valid"]]
            clean = (me["pit_status"] == 0 and me["driver_status"] in (1, 4) and tr.sc == 0
                     and not (tr.sta_all[tr.player] and tr.sta_all[tr.player]["fia_flag"] in (2, 3)))
            race = tr.is_race()
            if race and self.topics.get("sectores", 0) <= 0:
                clean = False                              # en carrera con tráfico y combustible cambiante no dicen nada útil: solo si los pides
            if not race:                                   # fuera de carrera, solo vueltas lanzadas y pérdidas razonables
                clean = clean and me["driver_status"] == 1
            top = 2.5 if race else 1.0
            if done and not me["lap_invalid"] and clean:
                th = self.th.get("sector_loss_s", 0.3)
                if self.last_sector == 0 and sec == 1 and me["sector1_ms"] > 0:
                    best = min(l["s1"] for l in done if l["s1"] > 0)
                    d = (me["sector1_ms"] - best) / 1000
                    if th <= d <= top:
                        out.append(Alert(f"Pierdes {d:.2f} s en el sector 1 respecto a tu mejor.", 3, "sec1", 20))
                elif self.last_sector == 1 and sec == 2 and me["sector2_ms"] > 0:
                    best = min(l["s2"] for l in done if l["s2"] > 0)
                    d = (me["sector2_ms"] - best) / 1000
                    if th <= d <= top:
                        out.append(Alert(f"Pierdes {d:.2f} s en el sector 2 respecto a tu mejor.", 3, "sec2", 20))
            self.last_sector = sec
        return out

    def _race(self, tr, me, sa, th):
        out = []
        add = out.append
        ses = tr.ses or {}
        # combustible (con el margen medido en vueltas reales; el campo del juego no es fiable al inicio)
        # Por defecto solo avisa si es crítico: al final de la carrera y sin llegar por un margen claro.
        # "más avisos de combustible" = avisa en cuanto falte algo, en cualquier vuelta (antes era siempre así).
        fm, ll_f = tr.fuel_margin(), tr.laps_left()
        if fm is not None:
            lvl = self.topics.get("combustible", 0)
            if lvl > 0:
                critical = fm < -0.05
            else:
                last_laps = th.get("fuel_alert_laps", 4) if lvl == 0 else 2
                deficit = th.get("fuel_alert_deficit", 0.2) if lvl == 0 else 1.0
                critical = bool(ll_f is not None and ll_f <= last_laps and fm < -deficit)
            if (not critical and lvl >= 0 and fm >= 1.0 and ll_f is not None and ll_f <= th.get("fuel_alert_laps", 4)):
                # al final de la carrera y sobrándote combustible: se dice UNA vez ("más avisos" la repite cada 90 s)
                add(Alert(f"Llegas con margen de combustible (+{fm:.1f} vueltas): puedes atacar sin ahorrar.", 3, "fuelok",
                          90 if lvl > 0 else 99999))
            if critical:
                deficit = -fm
                worse = self.fuel_said is None or deficit >= self.fuel_said + 0.5 or (ll_f is not None and ll_f <= 1 and self.fuel_lap != me["lap"])
                if worse and self.fuel_lap != me["lap"]:      # una vez al entrar en crítico; otra solo si empeora 0.5 vueltas o en la última vuelta
                    self.fuel_lap, self.fuel_said = me["lap"], deficit
                    add(Alert(f"Combustible justo: te faltan {-fm:.1f} vueltas, ahorra.", 1, "fuel", 0))
                    zs = tr.fz.suggest(1)
                    if zs:
                        add(Alert(zs[0], 2, "fuelzone", 150))
                if sa["fuel_mix"] != 0:          # F1 25 solo ofrece normal y pobre
                    add(Alert("Pon la mezcla pobre.", 2, "mix", 240))
        # ERS
        ers = sa["ers_store_j"] / 4_000_000 * 100
        ahead, behind = tr.neighbours()
        attack = bool(ahead and 0.05 <= ahead[1] <= th.get("attack_range_s", 1.5))
        ers_low = 20 if self.topics.get("ers", 0) > 0 else th.get("ers_low_pct", 10)
        if ers >= ers_low + 8:
            self.erslow_said = False                                   # se recargó: el siguiente episodio vuelve a avisarse
        elif ers < ers_low and not attack and not self.erslow_said and me["lap"] - self.erslow_lap >= 3:
            self.erslow_said, self.erslow_lap = True, me["lap"]            # a lo sumo una vez cada 3 vueltas
            add(Alert(f"Batería ERS baja ({ers:.0f}%), ahorra despliegue.", 3, "erslow", 60))
        lap = me["lap"]
        for car in [c for c, ep in self.attack_ep.items() if not (ahead and ahead[0] == c and ahead[1] <= 1.8) and lap - ep["seen"] >= 1]:
            del self.attack_ep[car]                            # el duelo terminó: el siguiente vuelve a avisarse
        if ahead and ahead[1] <= 1.8 and ahead[0] in self.attack_ep:
            self.attack_ep[ahead[0]]["seen"] = lap
        if attack and ers >= 25 and sa["ers_mode"] != 3:
            ep = self.attack_ep.get(ahead[0])
            if ep is None:                                              # una vez por duelo (se rearma cuando el duelo termina)
                self.attack_ep[ahead[0]] = {"lap": lap, "seen": lap}
                add(Alert(f"Usa el modo adelantamiento: {tr.name(ahead[0])} está a {ahead[1]:.1f} s.", 2, "attack", 40))
        # DRS (2025). En 2026 el formato UDP no está verificado: sin avisos de DRS.
        if self.year < 2026 and self._rising("drs", sa["drs_allowed"]):
            who = ahead[0] if ahead and 0.05 <= ahead[1] <= th.get("attack_range_s", 1.5) else None
            if lap - self.drs_seen.get(who, -99) >= 3:         # el DRS se abre cada vuelta mientras persigues al mismo: se dice cada 3 vueltas
                self.drs_seen[who] = lap
                if who is not None:
                    add(Alert(f"DRS disponible, ataca a {tr.name(who)}.", 2, "drs", 8))
                else:
                    add(Alert("DRS disponible.", 3, "drs", 8))
        # rivales cercanos (±2 posiciones): paradas y neumáticos al límite
        if tr.is_race():
            mypos = tr.lap_all[tr.player]["position"]
            for i, la in enumerate(tr.lap_all):
                if i == tr.player or not la or la["result_status"] != 2 or abs(la["position"] - mypos) > 2:
                    continue
                g0 = tr.rel_gap(i) if la["pit_status"] == 0 else None
                if g0 is not None and abs(g0) > 0.05:
                    self.__dict__.setdefault("_rgap", {})[i] = g0                 # el hueco útil es el último real antes de entrar al pit lane
                if self._rising(f"rpit{i}", la["pit_status"] != 0):
                    g = self.__dict__.get("_rgap", {}).get(i)
                    where = "" if g is None else f" Está a {abs(g):.1f} s {'delante' if g < 0 else 'detrás'}."
                    add(Alert(f"{tr.name(i)} entra a boxes.{where}", 3, "rivalpit", 20))
                rd = tr.dmg_all[i]
                if rd:
                    w = max(rd["tyre_wear_pct"])
                    a = self._level(f"rwear{i}", w, [(th.get("wear_critical", 70) - 5, "alto")],
                                    lambda n, v, i=i: Alert(f"{tr.name(i)} lleva el neumático al {v:.0f}%: debería parar pronto.",
                                                            3, "rivalwear", 30))
                    if a:
                        add(a)
        # defensa
        for car in [c for c, ep in self.defend_ep.items() if not (behind and behind[0] == c and behind[1] <= 1.5) and lap - ep["seen"] >= 1]:
            del self.defend_ep[car]
        if behind and behind[1] <= 1.5 and behind[0] in self.defend_ep:
            self.defend_ep[behind[0]]["seen"] = lap
        if behind and 0.05 <= behind[1] <= th.get("drs_range_s", 1.0):
            rs = tr.sta_all[behind[0]]
            drs = " con DRS" if (rs and rs["drs_allowed"] and self.year < 2026) else ""
            ep = self.defend_ep.get(behind[0])
            # un duelo se avisa una vez; de nuevo a las 3 vueltas si sigue, o cuando por fin le abren el DRS (salvo que se acabe de avisar:
            # sprint del 11 nov, «defiende» y «defiende con DRS» con 1.5 s de diferencia)
            now = getattr(tr, "t", None)
            recent = ep is not None and now is not None and ep.get("t") is not None and now - ep["t"] < 8.0
            if drs and recent and not ep["drs"]:
                ep["drs"] = True
            elif ep is None or lap - ep["lap"] >= 3 or (drs and not ep["drs"]):
                self.defend_ep[behind[0]] = {"lap": lap, "seen": lap, "t": now, "drs": bool(drs) or bool(ep and ep["drs"])}
                add(Alert(f"Tienes a {tr.name(behind[0])} a {behind[1]:.1f} s detrás{drs}: defiende.", 2,
                          "defend_drs" if drs else "defend", 30 if drs else 60))
        return out
