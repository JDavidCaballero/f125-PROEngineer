#!/usr/bin/env python3
"""Ingeniero de pista de F1 25: alertas por reglas + estrategia simulada + IA local opcional.

  python3 engineer.py --file captures/demo_corta.f1raw --speed 0          # sobre una captura
  python3 engineer.py --live --port 20777                                # con el juego (o con replay)
  python3 engineer.py --file ... --mode arriesgado --llm --verbose --chat

Flujo: paquete UDP -> RaceTracker (estado de los 22 coches) -> Tactics (alertas con frases fijas)
       y Strategy (opciones con probabilidades) -> el LLM elige entre opciones ya calculadas (opcional).
Todo se registra en runs/*.jsonl (decisiones, opciones, resultado) para analizar o calibrar después.
"""
import argparse, json, re, socket, sys, threading, time, unicodedata
from datetime import datetime
from pathlib import Path

import config as C
import dialog as D
import llm as L
import strategy as S
from assistant import Assistant
from f1_parse import read_capture, NCARS, TRACKS, tyre_extra_damage
from state import RaceTracker, WHEELS
from tactics import Tactics, mmss


# avisos de «ambiente» (duelos, sectores, rivales, temperaturas...) que entran en el presupuesto por vuelta
BUDGET_KEYS = ("defend", "attack", "sec", "rivalpit", "rivalwear", "closing", "tyret", "erslow", "fuelzone", "ftlp", "lapsum",
               "invalidlap", "note_", "coach", "recover", "lapreport", "wchange")


class Engineer:
    def __init__(self, cfg, mode=None, client=None, out=print, log_path=None, sync=True, profile=None, verbose=False, memory_path=None):
        self.cfg = cfg
        self.mode = mode or cfg.get("engineer", {}).get("mode", "normal")
        if self.mode not in S.MODES:
            raise ValueError(f"modo desconocido: {self.mode} (usa {', '.join(S.MODES)})")
        self.tr = RaceTracker(cfg, profile)
        self.tac = Tactics(cfg)
        self.client = client
        self.out = out
        self.sync = sync
        self.verbose = verbose
        self.log_path = log_path
        self._log_f = None
        self.cool, self.pending = {}, []
        self.bad_packets, self.last_bad = 0, None      # paquetes ilegibles (truncados, otro formato): se cuentan y se siguen leyendo
        self.last_say = -99.0
        self.log = []                    # frases emitidas
        self.evals = 0
        self.last_eval_t = -99.0
        self.last_key = None
        self.intro_done = False
        self.coach_lap, self.recover = {}, None
        import quali
        quali.DEPLOY_LIMIT_J = cfg.get("ers", {}).get("deploy_limit_mj", 4.0) * 1e6
        eb = cfg.get("engineer", {})
        self.budget = {2: eb.get("budget_prio2", 4), 3: eb.get("budget_prio3", 2)}      # avisos por vuelta (0 = sin límite)
        self.budget = {k: v for k, v in self.budget.items() if v}
        self.budget_lap, self.budget_n, self.budget_logged = -1, {2: 0, 3: 0}, set()
        import raceplan
        self.memory = raceplan.SessionMemory(path=memory_path)   # lo medido en entrenos/clasificación (en disco si hay ruta)
        self.tr.memory = self.memory               # el tracker la consulta (p. ej. pérdida de parada medida antes de la primera parada de la carrera)
        self.preplan, self.preplan_said = None, False
        self.fmt_checked = False
        self._pit_wait, self._pit_tyres = None, False      # salida de boxes pendiente de confirmar cambio de neumáticos
        d = cfg.get("dialog", {})
        self.change_min_pos = d.get("plan_change_min_pos", 1.0)    # posiciones esperadas que debe ganar un cambio del plan que se sigue
        self.change_hold_laps = d.get("plan_change_hold_laps", 5)  # vueltas sin otro cambio de plan (salvo algo grave)
        self.plan_change_lap = self.proposed_lap = -99
        self.dmg_told = self.dmg_checked = None              # golpe ya comentado / ya medido (vuelta del golpe)
        self.summary_uids = set()                            # sesiones ya resumidas (no repetir el resumen al cambiar de sesión)
        self.plan = None             # primera parada del plan recomendado: (vuelta, compuesto)
        self.lap_seen = 0
        self.last_spoke_lap = 0
        self.aero_bucket = 0
        self._busy = threading.Lock()
        self.min_gap = cfg.get("engineer", {}).get("min_gap_between_strategy_s", 8)
        self.dialog = D.Dialog(cfg, self.mode)
        self.goals = D.Goals(cfg)
        self.assistant = Assistant(self)
        self._epoch = self.tr.epoch
        self.plan_hint = None        # primera parada propuesta a petición del piloto
        self.pred_log = []           # (vuelta, posición esperada de la mejor opción): calibración al final
        self.debrief_done = False
        self.my_timed = []           # tus vueltas cronometradas con traza (clasificación y entrenos)
        self.timing_asked = set()    # paradas para las que ya se ofreció una alternativa de momento
        self.last_best_epos = None
        self.held = None             # propuesta retenida hasta que el piloto esté en una recta
        self.on_eval = []            # funciones(reason, lap, sit, me, rivals, rep, chosen): para backtest y herramientas
        self.on_say = []             # funciones(texto, prioridad): cada aviso que el ingeniero dice (la voz se engancha aquí)
        self.lock = threading.RLock()  # la telemetría y la voz corren en hilos distintos

    # ------------------------------------------------------------------ salida
    def _log(self, rec):
        if not self.log_path:
            return
        if self._log_f is None:
            Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
            self._log_f = open(self.log_path, "a", encoding="utf-8")
        self._log_f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._log_f.flush()

    def say(self, text, prio=2, key="", cooldown=0.0, force=False):
        with self.lock:
            return self._say(text, prio, key, cooldown, force)

    def _say(self, text, prio, key, cooldown, force):
        t = self.tr.t
        if not force and prio >= 2 and self.dialog.muted_until > (self.tr.cur_lap or 0):
            self._log({"type": "silence", "t": round(t, 1), "reason": "silencio pedido por el piloto", "text": text})
            return False
        if key and not force and t - self.cool.get(key, -1e9) < cooldown:
            return False
        if not force and prio >= 2 and not self._within_budget(prio, text, key, t):
            return False
        if not force and prio >= 3 and t - self.last_say < 3.0:
            return False
        if not force and prio == 2 and t - self.last_say < 1.5:
            if len(self.pending) < 5:
                self.pending.append((t, text, key, cooldown))
            return False
        self._speak(text, key, prio)
        return True

    def _within_budget(self, prio, text, key, t):
        """Presupuesto de avisos por vuelta: no más de N de prioridad 2 y M de prioridad 3 (las de prioridad 1 y las respuestas
        al piloto no cuentan). Informe real: 125 avisos en 18 vueltas, uno cada 10 s."""
        if not (key == "drs" or key.startswith(BUDGET_KEYS)):
            return True                                   # seguridad, daños, banderas, estrategia y avisos del plan nunca se recortan
        lap = self.tr.cur_lap or 0
        if lap != self.budget_lap:
            self.budget_lap, self.budget_n, self.budget_logged = lap, {2: 0, 3: 0}, set()
        cap = self.budget.get(prio)
        if cap is None:
            return True
        if self.budget_n.get(prio, 0) >= cap:
            self.cool[key] = t                            # cuenta como dicho: no se reintenta en cada paquete ni se dice tarde, ya caducado
            if key not in self.budget_logged:
                self.budget_logged.add(key)
                self._log({"type": "budget", "t": round(t, 1), "lap": lap, "prio": prio, "key": key, "text": text[:80]})
            return False
        self.budget_n[prio] = self.budget_n.get(prio, 0) + 1
        return True

    def _speak(self, text, key, prio=2):
        t = self.tr.t
        if key:
            self.cool[key] = t
        self.last_say = t
        lap = self.tr.cur_lap or self.lap_seen
        self.lap_seen = lap
        self.out(f"[vuelta {lap} | t={t:6.1f}s] {text}")
        self.log.append(text)
        for cb in self.on_say:
            cb(text, prio)
        self._log({"type": "say", "t": round(t, 1), "lap": lap, "prio": prio, "text": text})

    def _flush_pending(self):
        t = self.tr.t
        self.pending = [p for p in self.pending if t - p[0] < 8.0]
        if self.pending and t - self.last_say >= 1.5:
            _, text, key, cd = self.pending.pop(0)
            if not key or t - self.cool.get(key, -1e9) >= cd:
                self._speak(text, key)

    # ------------------------------------------------------------------ paquetes
    def on_packet(self, t, d):
        with self.lock:
            try:
                self._on_packet(t, d)
            except Exception as e:                                    # noqa: BLE001 un paquete malo no puede matar la telemetría
                self.bad_packets += 1
                self.last_bad = f"{type(e).__name__}: {e}"[:160]
                if self.bad_packets in (1, 10, 100) or self.bad_packets % 1000 == 0:
                    self._log({"type": "bad_packet", "t": round(t, 1), "n": self.bad_packets, "len": len(d), "error": self.last_bad})

    def _new_session(self):
        """Sesión nueva (otro UID): el reloj del juego vuelve a 0, así que todo lo que dependa de él se reinicia.
        Sin esto, los tiempos de la sesión anterior (p. ej. 1700 s de entreno) dejaban mudo al ingeniero en la clasificación y la carrera."""
        topics = dict(self.tac.topics)
        self._pit_wait, self._pit_tyres = None, False
        carried = {k: v for k, v in self.tac.levels.items() if k.startswith(("dmg_", "cmp_"))}
        self.tac = Tactics(self.cfg)
        self.tac.topics.update(topics)                         # lo que pediste («menos avisos de X») se mantiene
        self.tac.levels.update(carried)                        # el daño del motor/alerones sigue siendo el mismo en la sesión siguiente: no se repite
        self.cool, self.pending, self.held = {}, [], None
        self.last_say, self.last_eval_t, self.last_key = -99.0, -99.0, None
        self.intro_done, self.debrief_done = False, False
        self.plan, self.plan_hint, self.last_best_epos = None, None, None
        self.lap_seen = self.last_spoke_lap = self.aero_bucket = 0
        self.dialog, self.goals = D.Dialog(self.cfg, self.mode), D.Goals(self.cfg)
        self.pred_log, self.my_timed, self.timing_asked = [], [], set()
        self.coach_lap, self.recover = {}, None
        self.budget_lap, self.budget_n, self.budget_logged = -1, {2: 0, 3: 0}, set()
        self.assistant.awaiting = None
        self._epoch = self.tr.epoch
        self._log({"type": "new_session"})

    def _session_summary(self, old):
        """Al empezar la sesión siguiente se resume la que terminó (entreno o clasificación) con el estado que tenía."""
        kind = old.session_kind() if old.ses else None
        try:
            m = self.memory.record(old)
            if m:
                self._log({"type": "memory", "kind": kind, "track": (old.ses or {}).get("track_id"),
                           "compounds": {str(c): {"pace": round(r["pace"], 2), "laps": r["laps"], "wear_rate": r["wear_rate"]}
                                         for c, r in m["tables"].items()}, "pit_loss": m["pit_loss"], "race_laps": m["race_laps"]})
        except Exception as e:                                       # noqa: BLE001
            self._log({"type": "memory_error", "error": repr(e)})
        if kind not in ("practice", "quali") or not old.hist or old.uid in self.summary_uids:
            return                                    # ya se resumió al final de esa sesión (informe 11 nov: salía dos veces)
        import practice
        try:
            lines = practice.summary(old)
        except Exception as e:                                        # noqa: BLE001
            self._log({"type": "summary_error", "error": repr(e)})
            return
        self._log({"type": "practice_summary", "kind": kind, "lines": lines})
        self.say(("Resumen del entrenamiento: " if kind == "practice" else "Resumen de la clasificación: ") + " ".join(lines[:3]),
                 3, "practice", 0, force=True)

    def _on_packet(self, t, d):
        sig = self.tr.on_packet(t, d)
        if self.tr.fmt is not None and self.tr.fmt != 2025:
            if not self.fmt_checked:                 # otro formato = bytes leídos en el sitio equivocado (2816 km/h, 13107 °C): no se avisa nada
                self.fmt_checked = True
                self._speak(f"El juego manda la telemetría en formato UDP {self.tr.fmt} y solo sé leer el formato 2025. "
                            "Cámbialo a 2025 en los ajustes de telemetría del juego; mientras tanto no doy avisos.", "fmt", 1)
            return
        if self.tr.epoch != self._epoch:
            self._new_session()
        for s in sig:
            if s[0] == "session_end":
                self._session_summary(s[1])
            if s[0] == "tyres" or s[0] == "sc":
                self.last_key = None
        for a in self.tac.poll(self.tr, sig):
            self.say(a.text, a.prio, a.key, a.cooldown)
        for s in sig:
            if s[0] == "final" and not self.debrief_done and self.tr.session_kind() == "practice":
                self.debrief_done = True
                import practice
                lines = practice.summary(self.tr)
                self._log({"type": "practice_summary", "lines": lines})
                self.summary_uids.add(self.tr.uid)
                self.say("Resumen del entrenamiento: " + " ".join(lines), 3, "practice", 0, force=True)
            if s[0] == "final" and not self.debrief_done and self.tr.is_race():
                self.debrief_done = True
                import debrief
                lines = debrief.build(self)
                self._log({"type": "debrief", "lines": lines, "pred_log": self.pred_log})
                if self.tac.topics.get("resumen", 0) >= 0:
                    self.say(" ".join(lines), 3, "debrief", 0, force=True)
            if s[0] == "lap":
                for text, prio, key in self.dialog.on_lap(s[1], in_pit=bool(self.tr.prev_pit)):
                    self.say(text, prio, key, 0, force=prio == 1)
            elif s[0] == "pit_in":
                self._pit_tyres = False
            elif s[0] == "tyres" and (self.tr.prev_pit or self._pit_wait):
                self._pit_tyres = True
                if self._pit_wait:                       # los neumáticos llegaron justo después de salir del pit lane
                    self._stop_done(self._pit_wait[0])
            elif s[0] == "pit_out":
                if self._pit_tyres:
                    self._stop_done(s[1])
                else:                                    # sin cambio de neumáticos todavía: puede ser un drive-through
                    self._pit_wait = (s[1], self.tr.t)
        if self._pit_wait and self.tr.t - self._pit_wait[1] > 15:
            self._log({"type": "pit_no_tyres", **self._pit_wait[0]})   # paso por boxes sin neumáticos (sanción): no ejecuta el plan
            self._pit_wait = None
        self._poll_laps()
        self._flush_pending()
        if self.held and (self.tr.low_workload() or self.tr.t - self.held[1] > 15):
            self.say(self.held[0], 2, "proposal", 0, force=True)
            self.held = None
        self._intro(sig)
        self._coach_tick(sig)
        self._maybe_strategy(sig)

    def _stop_done(self, rec):
        """Parada con cambio de neumáticos: cumple el plan aceptado o se compara con el plan previo."""
        self._pit_wait, self._pit_tyres = None, False
        had_plan = self.dialog.accepted is not None
        m = self.dialog.on_pit_out()
        if m:
            self.say(m, 3, "plan_done", 0)
        if had_plan:                                  # cumplió el plan aceptado: no se compara con el previo (informe del 11 nov:
            self.preplan_said = True                  # «el plan previo era parar en la 15» justo tras cumplir el aceptado)
            return
        self._off_plan_stop(rec)

    def _off_plan_stop(self, rec):
        """Una parada que no estaba en el plan previo ni en un plan aceptado: se dice, con lo que se había previsto."""
        tr = self.tr
        if self.preplan_said or not tr.is_race() or self.preplan is None or self.dialog.accepted is not None:
            return
        self.preplan_said = True
        planned = self.preplan["stints"]
        lap = rec.get("lap", tr.cur_lap)
        if len(planned) == 1:
            txt = f"Parada en la vuelta {lap}: el plan previo era acabar sin parar. Recalculo con los neumáticos nuevos."
        else:
            want = planned[0][1] + 1
            if abs(lap - want) <= 1:
                return
            txt = (f"Parada en la vuelta {lap}: el plan previo era parar al final de la vuelta {planned[0][1]}. "
                   f"{'Antes' if lap < want else 'Después'} de lo previsto; recalculo.")
        self._log({"type": "off_plan_stop", "lap": lap, "planned": planned})
        self.say(txt, 2, "offplan", 9999)

    def _coach_tick(self, sig):
        """Consejos de carrera con las trazas de los dos coches: al perder una posición (cómo recuperarla) y durante un duelo largo
        (dónde atacar). Una vez por rival cada 4 vueltas; se pueden apagar con «menos avisos de adelantamiento»."""
        import coach
        tr = self.tr
        if not tr.is_race() or tr.cur_lap < 3 or self.tac.topics.get("adelantamiento", 0) < 0:
            return
        for s in sig:
            if s[0] == "event" and s[1]["code"] == "OVTK" and s[1]["overtaken"] == tr.player:
                self.recover = (s[1]["car"], tr.t)
        if self.recover and tr.t - self.recover[1] >= 6 and tr.low_workload():
            car, _ = self.recover
            self.recover = None
            g, _o = tr.gaps()
            gap = g.get(car, 0) - g.get(tr.player, 0)
            if gap < 0 and abs(gap) <= 2.5 and tr.cur_lap - self.coach_lap.get(car, -99) >= 2:
                txt = coach.how_to_pass(self, car, "recover")
                if txt:
                    self.coach_lap[car] = tr.cur_lap
                    self._log({"type": "coach", "kind": "recover", "car": car, "lap": tr.cur_lap, "text": txt})
                    self.say(f"{tr.name(car)} te pasó. {txt}", 2, f"recover{car}", 0)
            return
        lap = tr.cur_lap
        h = [tr.gap_hist.get(l, {}) for l in (lap - 1, lap - 2)]
        if all(x.get("front") is not None and x["front"] <= 1.0 for x in h) and h[0].get("front_car") == h[1].get("front_car") \
                and h[0].get("front_car") is not None:
            car = h[0]["front_car"]
            if lap - self.coach_lap.get(car, -99) >= 4 and tr.low_workload():
                txt = coach.how_to_pass(self, car)
                if txt:
                    self.coach_lap[car] = lap
                    self._log({"type": "coach", "kind": "attack", "car": car, "lap": lap, "text": txt})
                    self.say(txt, 2, f"coach{car}", 0)

    def _poll_laps(self):
        """Vuelta tuya recién cerrada (clasificación/entrenos): análisis una vez por vuelta cronometrada. En carrera no habla."""
        tr = self.tr
        if tr.player is None:
            return
        while tr.traces.rejects:                                  # diagnóstico: por qué no se midió una vuelta
            r = tr.traces.rejects.popleft()
            if r["car"] == tr.player:
                self._log({"type": "lap_rejected", **r})
        rec = tr.traces.pop_done(tr.player)
        if rec is not None:
            self._log({"type": "lap_trace", "lap": rec.lap, "time_ms": rec.time_ms, "timed": rec.timed, "valid": rec.valid,
                       "cover": round(rec.cover, 2),
                       "status": getattr(rec, "status", None)})
        if rec is None or not rec.timed:
            return
        self.my_timed.append(rec)
        del self.my_timed[:-12]
        if tr.session_kind() not in ("quali", "practice") or self.tac.topics.get("vueltas", 0) < 0:
            return
        import quali as Q
        prev = [r for r in self.my_timed if r is not rec]
        slow = 1.015 if tr.session_kind() == "practice" else 1.07      # en entrenos, tandas largas: solo vueltas de ataque; en quali, todas las lanzadas
        if prev and rec.time_ms > slow * min(r.time_ms for r in prev):
            self._log({"type": "lap_report_skipped", "lap": rec.lap, "reason": "no es una vuelta de ataque (>1.5% de tu mejor)"})
            return                                            # tanda larga, vuelta de enfriamiento...: sin análisis de curvas (se pide por voz)
        if not prev:
            lines = [f"Vuelta lanzada: {Q.fmt(rec.time_ms)}."] + Q.ers_report(rec)
        else:
            best = min(prev, key=lambda r: r.time_ms)
            lines = Q.lap_report(rec, best, "tu mejor vuelta anterior", n=2)
            lines += Q.ers_report(rec, best, "tu mejor vuelta anterior")[:1]
        main = list(lines)
        mine = tr.traces.best.get(tr.player)
        lead = self._reachable_rival(mine)
        riv = Q.rival_report(mine, lead[1], lead[0]) if lead else []
        text = " ".join(main + riv)
        self._log({"type": "lap_report", "lap": rec.lap, "time_ms": rec.time_ms, "text": text})
        pb = not prev or rec.time_ms < min(r.time_ms for r in prev)
        self.say(" ".join(main[:3 if pb else 2] + riv), 2, "lapreport", 0)       # una vuelta normal: lo esencial; la mejor, un poco más

    def _reachable_rival(self, mine, max_gap_s=1.0):
        """(nombre, vuelta) del coche de delante en la tabla (por mejor vuelta) si está a <= max_gap_s y hay traza suya."""
        tr = self.tr
        if mine is None:
            return None
        cand = [(r.time_ms, car, r) for car, r in tr.traces.best.items() if car != tr.player and r.time_ms < mine.time_ms
                and (mine.time_ms - r.time_ms) / 1000.0 <= max_gap_s]
        if not cand:
            return None
        _, car, r = max(cand)                      # el inmediatamente más rápido que tú
        return tr.name(car), r

    def _intro(self, sig):
        """Al empezar la carrera: qué nivel de estrategia aplica según su longitud."""
        if self.intro_done or self.tr.cur_lap < 1 or not self.tr.is_race():
            return
        sa = self.tr.sta_all[self.tr.player] if self.tr.player is not None and self.tr.player < NCARS else None
        if sa is None and self.tr.t < 5:
            return                                   # aún no llegó el neumático montado: el plan previo parte de él
        ses = self.tr.ses
        n = ses.get("total_laps") or 0
        if not n:
            return
        tier = S.tier_of(n)[0]
        brief = {"sprint": "sin paradas de neumáticos; céntrate en huecos, DRS y ERS",
                 "corta": "como mucho una parada; evaluaré undercut y Safety Car",
                 "media": "una o dos paradas; vigilaré la ventana de neumáticos, rivales y Safety Car",
                 "larga": "estrategia completa: 1 a 3 paradas, combustible, clima y Safety Car"}[tier]
        track = TRACKS.get(ses.get("track_id"), "")
        self.say(f"Carrera de {n} vueltas{' en ' + track if track else ''} ({tier}): {brief}.", 2, "intro", 9999)
        self.intro_done = True                       # si no se pudo decir al momento, queda en cola; no se repite
        import raceplan
        text, why = raceplan.build(self, n, sa["visual_compound"] if sa else None)
        self._log({"type": "preplan", "text": text, "why": why})
        self.preplan = getattr(self, "_last_plan", None) if text else None
        if text:
            self.say(text, 2, "preplan", 9999)

    def _maybe_strategy(self, sig):
        tr = self.tr
        if not tr.is_race():
            return
        reason = None
        for s in sig:
            if s[0] == "lap" and s[2] >= 1:
                reason = reason or "lap"
            elif s[0] in ("sc", "red_flag", "weather", "tyres", "pit_out"):
                reason = s[0]
            elif s[0] == "event" and s[1]["code"] == "PENA" and s[1].get("car") == tr.player:
                reason = "penalty"
        loss = tr.damage_loss() if (reason == "lap" and tr.dmg_onset is not None and tr.dmg_onset != self.dmg_checked) else None
        if loss is not None:                                 # ya hay 2 vueltas tras el golpe: se decide con lo medido
            self.dmg_checked = tr.dmg_onset
            self._log({"type": "damage_measured", "lap": tr.cur_lap, "loss_s": round(loss, 2)})
            if loss >= self.cfg.get("thresholds", {}).get("damage_loss_s", 0.5):
                reason = "damage"
        dm = tr.dmg_all[tr.player] if tr.player is not None and tr.player < NCARS else None
        if dm:
            bucket = int(max(dm["front_left_wing"], dm["front_right_wing"], max(tyre_extra_damage(dm))) // 20)
            if bucket > self.aero_bucket:
                reason = reason or "damage"
            self.aero_bucket = bucket
        if reason is None:
            return
        if reason == "lap" and tr.t - self.last_eval_t < self.min_gap * 0.5:
            return
        if reason != "lap" and tr.t - self.last_eval_t < 2.0:
            return
        self.strategy_tick(reason)

    # ------------------------------------------------------------------ estrategia
    def strategy_tick(self, reason):
        tr = self.tr
        sit, me, rivals, info = tr.build_inputs(self.goals.include())
        if sit is None:
            self._log({"type": "skip", "t": round(tr.t, 1), "reason": info})
            return
        self.last_eval_t = tr.t
        summary = tr.summary()
        lap = tr.cur_lap
        job = (reason, lap, sit, me, rivals, info, summary, tr.t)
        if self.sync:
            self._job(*job)
        elif self._busy.acquire(blocking=False):
            threading.Thread(target=self._guarded_job, args=job, daemon=True).start()

    def _guarded_job(self, *job):
        try:
            self._job(*job)
        finally:
            self._busy.release()

    def plan_keep(self):
        """Primera parada que se re-evalúa siempre (estabilidad): la aceptada, la propuesta o la última recomendada."""
        for p in (self.dialog.accepted, self.dialog.pending):
            if p and p.option.stops:
                return p.option.stops[0]
        return self.plan_hint or self.plan

    def evaluate_now(self, include=()):
        """Evaluación inmediata (para las preguntas del piloto). (rep, sit, me, rivals, info) o (None, motivo)."""
        sit, me, rivals, info = self.tr.build_inputs(include)
        if sit is None:
            return None, info
        p = info.get("params", self.tr.params)
        rep = S.evaluate(sit, me, rivals, p, self.mode, seed=p.seed * 1000 + self.tr.cur_lap + 7, keep=self.plan_keep())
        if rep.skipped:
            return None, rep.skipped
        return rep, sit, me, rivals, info

    def _job(self, reason, lap, sit, me, rivals, info, summary, t):
        p = info.get("params", self.tr.params)
        rep = S.evaluate(sit, me, rivals, p, self.mode, seed=p.seed * 1000 + lap, keep=self.plan_keep())
        self.evals += 1
        if rep.skipped:
            self._log({"type": "skip", "t": round(t, 1), "lap": lap, "reason": rep.skipped})
            if reason == "sc" and sit.sc and me.stops >= 1 and me.age < 3:     # recién parado: aún sin ritmo medible
                self.say(f"Con el {'Safety Car' if sit.sc == 1 else 'VSC'}: acabas de cambiar neumáticos, sigue fuera.",
                         2, "strategy", 0, force=True)
            return
        # estabilidad: el óptimo suele ser plano; si el plan anterior sigue casi igual de bueno, se mantiene
        prefer, keep = None, self.plan_keep()
        if keep and rep.best.kind == "PIT":
            tol = 0.35 * S.MODES[self.mode]["margin"]
            for o in rep.options:
                if o.stops and D.same_stop(o.stops[0], keep, 0) and rep.best.utility - o.utility <= tol:
                    prefer = o.id
                    break
        dec = L.choose(self.client, summary, rep, self.mode, prefer)
        by_id = {o.id: o for o in rep.options}
        chosen = by_id[dec["opcion"]]
        self.plan = chosen.stops[0] if (chosen.kind == "PIT" and chosen.stops and chosen.stops[0][0] >= lap) else None
        if self.plan_hint and self.plan_hint[0] < lap:
            self.plan_hint = None
        self.last_best_epos = rep.best.e_pos
        self.pred_log.append((lap, round(rep.best.e_pos, 2)))
        self.tr.last_advice = {"opcion": chosen.desc, "p_victoria_pct": S.pct(chosen.p_win),
                               "posicion_esperada": round(chosen.e_pos, 1)}
        self._log({"type": "strategy", "t": round(t, 1), "lap": lap, "reason": reason, "mode": self.mode,
                   "tier": rep.tier, "nsim": rep.nsim, "elapsed_s": round(rep.elapsed_s, 2), "info": {
                       k: v for k, v in info.items() if k != "params"}, "summary": summary, "decision": dec,
                   "report": rep.to_dict()})
        for cb in self.on_eval:
            cb(reason, lap, sit, me, rivals, rep, chosen)
        if self.verbose:
            self._print_table(rep, dec, info)
        self._announce(reason, lap, sit, me, rivals, rep, chosen, dec)

    def _timing_alt(self, rep, chosen, lap):
        """Alternativa de momento (undercut/overcut) para la parada propuesta; una sola vez por ventana de parada."""
        import timing
        if chosen.kind != "PIT" or not chosen.stops or self.mode == "conservador" or self.tac.topics.get("alternativas", 0) < 0:
            return None
        key = chosen.stops[0][0]
        if key in self.timing_asked:
            return None
        alts = timing.alternatives(rep, chosen, lap)
        if not alts:
            return None
        self.timing_asked.add(key)
        return alts[0]

    def _print_table(self, rep, dec, info):
        self.out(f"   -- estrategia ({rep.tier}, {rep.nsim} sim, {rep.elapsed_s:.1f}s, parada={info['pit_loss_src']}) | "
                 f"{rep.win_view}")
        for o in rep.options[:6]:
            mark = "*" if o.id == dec["opcion"] else (" " if o.id in rep.eligible else "-")
            self.out(f"   {mark}{o.id}) {o.desc:62s} pos {o.e_pos:4.1f} | gana {S.pct(o.p_win):3d}% podio {S.pct(o.p_podium):3d}% "
                     f"| pts {o.e_pts:4.1f} u={o.utility:4.1f}")

    def _announce(self, reason, lap, sit, me, rivals, rep, chosen, dec):
        """Decide si hablar. Regla: silencio salvo mejora material, urgencia o ventana que se cierra."""
        for text, prio in self.goals.update(chosen, sit, me, rivals, lap):
            self.say(text, prio, "goal", 0)
        urgent = reason in ("sc", "red_flag", "weather", "damage", "penalty")
        if reason == "damage" and not self._damage_serious(me):
            urgent = False                                   # daño leve: solo se para si la simulación lo justifica (filtro normal)
            if self.dmg_told != self.tr.dmg_onset and self.dialog.muted_until <= lap:
                self.dmg_told = self.tr.dmg_onset
                self.say("Con el daño: de momento seguimos. Mido en las próximas 2 vueltas cuánto pierdes y te digo si hay que parar.",
                         2, "dmg_wait", 0)
        win = rep.window or {}
        closing = (not win.get("reaches_end", True)) and win.get("tyre_laps_left", 99) <= 2
        import compare as C
        rows = C.laps_table(self.tr, self.tr.player)
        valid, why = self.dialog.gate.validate(lap, sit, chosen, urgent, closing, sum(1 for r in rows if r["clean"]),
                                               sum(1 for r in rivals if r.pace), len(rivals))
        if valid:
            speak, why = self.dialog.gate.decide(lap, rep, chosen, self.dialog.accepted, urgent, closing, self.dialog.pending)
        else:
            speak = False
        if not valid:
            self._log({"type": "proposal_blocked", "t": round(self.tr.t, 1), "lap": lap, "why": why})
        if speak and chosen.kind != "PIT":
            speak, why = False, "el modelo prefirió seguir fuera"
        if speak and self.dialog.muted_until > lap:
            speak, why = False, "silencio pedido por el piloto"
        plan_b = False
        if speak:
            speak, why, plan_b = self._change_ok(rep, chosen, sit, me, lap, urgent, why)
        tag = {"sc": ({1: "Con el Safety Car: ", 2: "Con el VSC: "}.get(sit.sc, "Con bandera verde: ")), "damage": "Con el daño: ",
               "weather": "Con el clima: ", "penalty": "Con la sanción: ", "red_flag": "Con la bandera roja: "}.get(reason, "")
        if speak:
            gain = (chosen.utility - rep.baseline.utility) if rep.baseline else 99.0
            if plan_b and not tag:
                tag = "Plan B: "
            prop = self.dialog.propose(chosen, tag + dec["frase"][0].lower() + dec["frase"][1:] if tag else dec["frase"],
                                       lap, self.tr.t, "ingeniero", why, gain)
            self.held = None                                 # una propuesta nueva sustituye a la que esperaba una recta
            self.proposed_lap = lap
            if self._plan_ref(rep) is not None:
                self.plan_change_lap = lap
            self._log({"type": "proposal", "t": round(self.tr.t, 1), "lap": lap, **prop.summary()})
            text = self.dialog.text(prop)
            alt = self._timing_alt(rep, chosen, lap)
            self.dialog.alt = alt
            if alt:
                import timing
                text += " " + timing.describe(alt, chosen, lap, self.tr.dirty_air()) + " Di «parar antes» o «alargar» si la prefieres."
                self._log({"type": "alt_offered", "t": round(self.tr.t, 1), "lap": lap, "kind": alt["kind"],
                           "rival": alt["rival"], "p_alt": round(alt["p_alt"], 2), "p_plan": round(alt["p_plan"], 2)})
            self.dialog.last_said = text
            if urgent or self.tr.low_workload():
                self.held = None
                self.say(text, 1 if urgent else 2, "proposal", 0, force=True)
            else:
                self.held = (text, self.tr.t)
        else:
            self._log({"type": "silence", "t": round(self.tr.t, 1), "lap": lap, "reason": why, "best": rep.best.desc})
            if urgent and why.startswith("ya hay una propuesta igual") and self.dialog.pending and self.dialog.muted_until <= lap:
                self.say(f"{tag}el plan no cambia: {self.dialog.pending.option.desc.lower()}.", 2, "strategy", 0, force=True)
            if urgent and chosen.kind == "STAY" and self.dialog.muted_until <= lap:
                self.say(f"{tag}{chosen.phrase} Esperado P{chosen.e_pos:.1f}.", 2, "strategy", 0, force=True)
        if self.dialog.muted_until <= lap:
            soon = chosen.kind == "PIT" and chosen.stops and chosen.stops[0][0] - lap <= 3
            for n in rep.notes:
                if n.startswith("Undercut posible") and not soon:
                    self._log({"type": "note_skipped", "lap": lap, "note": n, "why": "el plan no incluye parar pronto"})
                    continue                              # ofrecer un undercut mientras el plan dice «sigue fuera» es contradictorio
                if n.startswith("Amenaza de undercut") and self._cover(n, rep, sit, lap):
                    continue
                self._note(n)

    def _damage_serious(self, me):
        """Daño que justifica hablar de parar ya: muy grave (≥ damage_critical, estimado) o que te cuesta mucho tiempo, medido."""
        dm = self.tr.dmg_all[self.tr.player] if self.tr.player is not None and self.tr.player < NCARS else None
        th = self.cfg.get("thresholds", {})
        worst = max(dm["front_left_wing"], dm["front_right_wing"], dm["rear_wing"]) if dm else 0
        tyre = max(tyre_extra_damage(dm)) if dm else 0                     # pinchazo: siempre grave
        return (worst >= th.get("damage_critical", 60) or tyre >= th.get("damage_severe", 50)
                or (me.dmg_measured and me.dmg_pace_s >= th.get("damage_loss_s", 0.5)))

    def _plan_ref(self, rep):
        """Opción del informe que corresponde al plan que se sigue (aceptado o plan previo), o None si no hay plan."""
        acc = self.dialog.accepted
        if acc and acc.option.stops:
            want = acc.option.stops[0]
        elif self.preplan and len(self.preplan["stints"]) > 1:
            st = self.preplan["stints"]
            want = (st[0][1], st[1][0])                      # parar al final de la vuelta L con el compuesto del 2.º tramo
        else:
            return None
        cands = [o for o in rep.options if o.stops and o.stops[0][1] == want[1] and abs(o.stops[0][0] - want[0]) <= 2]
        return min(cands, key=lambda o: abs(o.stops[0][0] - want[0])) if cands else None

    def _change_ok(self, rep, chosen, sit, me, lap, urgent, why):
        """¿Merece la pena cambiar el plan que se sigue? (hablar?, motivo, es_plan_b). Regla de diseño (11 nov): si la carrera va
        según el plan, solo una alternativa MUY clara (≥ 1 posición esperada); fuera de los puntos o perdiendo mucho, plan B solo
        con algo grave (SC, daño, clima, sanción) o una posibilidad muy clara. Tras un cambio, unas vueltas sin otro."""
        ref = self._plan_ref(rep)
        if ref is None or not self.tr.is_race():
            return True, why, False
        if chosen.stops and D.same_stop(chosen.stops[0], ref.stops[0], 2):
            return True, why, False                           # es el mismo plan (p. ej. confirmar la parada)
        grid = self.tr.lap_all[self.tr.player].get("grid") if self.tr.lap_all[self.tr.player] else None
        bad = me.pos > 10 or bool(grid and me.pos - grid >= 3)
        if urgent:
            return True, why, bad
        if lap - self.plan_change_lap < self.change_hold_laps:
            return False, f"cambio de plan hace {lap - self.plan_change_lap} vueltas: no se cambia otra vez tan pronto", False
        gain_pos = ref.e_pos - chosen.e_pos
        if gain_pos < self.change_min_pos:
            return False, f"el plan sigue valiendo: la alternativa gana {gain_pos:.1f} posiciones (mínimo {self.change_min_pos:.1f})", False
        return True, f"alternativa clara: {gain_pos:.1f} posiciones mejor que el plan", bad

    def _cover(self, note, rep, sit, lap):
        """Ante la amenaza de undercut de un rival: comparar «parar ahora» contra «seguir fuera» y decirlo, en vez de solo avisar."""
        if self.dialog.accepted:
            return False
        opt = self.assistant._pit_now_option(rep, sit)
        # se compara con lo que harías si no: el plan que sigues, o la mejor opción que no sea parar ya (antes solo con «seguir
        # fuera», que no existe mientras falte la parada obligatoria: justo cuando hay undercuts)
        stay = self._plan_ref(rep) or next((o for o in rep.options if not (o.stops and o.stops[0][0] == sit.lap)), None)
        if opt is None or stay is None or self.proposed_lap == lap:
            return False                                   # ya se propuso algo esta vuelta: una sola pregunta cada vez
        later = "si sigues fuera" if stay.kind == "STAY" else f"si paras en la vuelta {stay.stops[0][0]}"
        if opt.e_pos <= stay.e_pos - 0.15:
            if not (self.dialog.pending and self.dialog.pending.reason == "amenaza de undercut"):
                self.dialog.propose(opt, opt.phrase, lap, self.tr.t, "ingeniero", "amenaza de undercut", stay.e_pos - opt.e_pos)
                self.held, self.proposed_lap = None, lap
            text = (f"{note} Cubrirte parando ahora a {S.NAME[opt.stops[0][1]]} te deja P{opt.e_pos:.1f} esperado, "
                    f"frente a P{stay.e_pos:.1f} {later}. ¿Lo hacemos?")
        else:
            text = (f"{note} Aun así, parar ahora te dejaría P{opt.e_pos:.1f} frente a P{stay.e_pos:.1f} {later}: "
                    "defiende en pista y no le dejes ir a rueda en la recta.")
        if "cover" in self.cool and self.tr.t - self.cool["cover"] < 120:
            return True                                    # dicho hace menos de 2 min: no se repite (antes se forzaba)
        self._log({"type": "cover", "lap": lap, "pit_now": round(opt.e_pos, 2), "ref": round(stay.e_pos, 2)})
        self.dialog.last_said = text
        self.say(text, 2, "cover", 120)
        return True

    def _note(self, n):
        self.say(n, 2, "note_" + n[:26], 120)

    # ------------------------------------------------------------------ preguntas del piloto
    def ask(self, q):
        """Texto del piloto (la voz entrará por aquí): intención -> consulta en código -> respuesta."""
        a = self.assistant.handle(q)                 # el LLM piensa fuera del candado; solo la consulta lo toma
        self.out(f"[ingeniero] {a}")
        d = self.dialog
        self._log({"type": "ask", "t": round(self.tr.t, 1), "q": q, "a": a,
                   "pending": d.pending.summary() if d.pending else None,
                   "accepted": d.accepted.summary() if d.accepted else None})
        return a

    def close(self):
        if self._log_f:
            self._log_f.close()
            self._log_f = None


# ---------------------------------------------------------------------------------------------- ejecución
def _chat_thread(eng):
    def run():
        for line in sys.stdin:
            q = line.strip()
            if q:
                eng.ask(q)
    threading.Thread(target=run, daemon=True).start()


def run_file(eng, path, speed):
    start = time.time()
    n = 0
    for t, d in read_capture(path):
        if speed > 0:
            wait = start + t / speed - time.time()
            if wait > 0:
                time.sleep(wait)
        eng.on_packet(t, d)
        n += 1
    eng.close()
    print(f"\nFin de la captura ({n} paquetes). Avisos emitidos: {len(eng.log)} | evaluaciones de estrategia: {eng.evals}")


def local_ip():
    """IP de este equipo en la red local (la que hay que poner en el juego). No envía nada: connect() en UDP solo elige la ruta."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))                       # dirección de documentación (no enrutable): no sale ningún paquete
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


class LinkWatch:
    """Vigila que lleguen datos del juego y lo dice en pantalla. Antes, sin paquetes (cortafuegos, IP o puerto mal puestos en el
    juego) el programa se quedaba callado para siempre y parecía que «no hacía nada» (informe de un usuario de Windows, 11 nov 2026)."""

    def __init__(self, port, eng=None, clock=time.time, no_data_s=10, every_s=60):
        self.port, self.eng, self.clock = port, eng, clock
        self.no_data_s, self.every_s = no_data_s, every_s
        self.t0 = self.last = self.clock()
        self.n, self.first, self.said_stop, self.next_status = 0, None, False, None

    def packet(self):
        now = self.clock()
        if self.first is None:
            self.first = now
            self.next_status = now + self.every_s
        self.n += 1
        self.last, self.said_stop = now, False

    def tick(self):
        """Mensaje para la pantalla, o None."""
        now = self.clock()
        if self.first is None:
            if now - self.last >= self.no_data_s:
                self.last = now - self.no_data_s + 30            # se repite cada 30 s mientras no llegue nada
                ip = local_ip()
                return (f"** No me llega ningún dato del juego en el puerto UDP {self.port}. Revisa en el juego (Ajustes > Telemetría): "
                        f"telemetría UDP activada, IP = {ip or 'la de este equipo'}, puerto = {self.port}, formato UDP 2025. "
                        "En Windows, permite a Python en el cortafuegos (red privada). **")
            return None
        if now - self.last >= self.no_data_s and not self.said_stop:
            self.said_stop = True
            return "** Han dejado de llegar datos del juego (¿pausa, menús o el juego se cerró?). Sigo escuchando. **"
        if self.next_status is not None and now >= self.next_status:
            self.next_status = now + self.every_s
            tr = getattr(self.eng, "tr", None)
            fmt = getattr(tr, "fmt", None)
            kind = tr.session_kind() if tr is not None and tr.ses else "sin sesión aún"
            warn = f" ** formato {fmt}: cámbialo a 2025 en el juego, así no doy avisos **" if fmt not in (None, 2025) else ""
            return f"[datos del juego: {self.n} paquetes, formato {fmt}, sesión {kind}]{warn}"
        return None


def run_live(eng, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(1.0)
    print(f"Ingeniero escuchando en UDP {port} | modo {eng.mode} | IA {'sí' if eng.client else 'no'} (Ctrl+C para parar)")
    t0 = time.time()
    watch = LinkWatch(port, eng)
    try:
        while True:
            msg = watch.tick()
            if msg:
                print(msg)
            try:
                d, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            watch.packet()
            if len(d) >= 29:
                eng.on_packet(time.time() - t0, d)
    except KeyboardInterrupt:
        pass
    eng.close()
    print(f"\nAvisos emitidos: {len(eng.log)} | evaluaciones de estrategia: {eng.evals}")


def main():
    C.utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--file")
    g.add_argument("--live", action="store_true")
    ap.add_argument("--speed", type=float, default=10.0, help="solo con --file; 0 = sin esperas")
    ap.add_argument("--port", type=int)
    ap.add_argument("--config", default=None)
    ap.add_argument("--mode", choices=list(S.MODES), help="conservador | normal | arriesgado")
    ap.add_argument("--llm", action="store_true", help="usar el servidor de LM Studio configurado en config.toml")
    ap.add_argument("--verbose", action="store_true", help="imprime la tabla de opciones en cada evaluación")
    ap.add_argument("--chat", action="store_true", help="escribe preguntas por teclado mientras corre")
    ap.add_argument("--no-log", action="store_true")
    a = ap.parse_args()
    cfg = C.load(a.config)
    if a.llm:
        cfg.setdefault("llm", {})["enabled"] = True
    client = L.LLMClient.from_cfg(cfg)
    llm_down = bool(client) and not client.ping()
    if llm_down:
        print("Aviso: el servidor del modelo no responde ahora; sigo con reglas y lo reintento cada 30 s.")
    log = None
    if not a.no_log:
        runs = Path(cfg.get("paths", {}).get("runs", "runs"))
        runs = runs if runs.is_absolute() else C.ROOT / runs
        log = runs / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".jsonl")
    eng = Engineer(cfg, a.mode, client, print, log, sync=bool(a.file), profile=C.load_profile(cfg), verbose=a.verbose,
                   memory_path=C.memory_path(cfg) if a.live else None)
    if llm_down and eng.assistant.brain is not None:
        eng.assistant.brain.down_until = time.time() + eng.assistant.brain.cooldown_s
    if a.chat:
        _chat_thread(eng)
    if a.file:
        run_file(eng, a.file, a.speed)
    else:
        run_live(eng, a.port or cfg.get("network", {}).get("port", 20777))


if __name__ == "__main__":
    main()
