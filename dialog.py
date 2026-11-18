#!/usr/bin/env python3
"""Diálogo piloto-ingeniero: filtro de silencio, propuestas con confirmación y objetivos.

- Gate: decide si una recomendación merece hablarle al piloto (mejora material, persistencia, cooldown,
  rechazos recientes, urgencia). Registra por qué calla.
- Dialog: propuesta pendiente -> aceptada / rechazada / caducada; recordatorios del plan aceptado.
- Goal: objetivo del piloto ("terminar P5", "pasar a Piastri") con ritmo requerido, estado con histéresis
  (en camino / en riesgo / inalcanzable) y escalera de planes B.
UDP es solo de salida: "aceptar" es un compromiso que el programa vigila, no una orden al coche.
"""
from dataclasses import dataclass, field

import strategy as S

MIN_GAIN = {"conservador": 1.0, "normal": 0.7, "arriesgado": 0.4}


def fmt_lap(s):
    return f"{int(s // 60)}:{s % 60:04.1f}"


def same_stop(a, b, tol=1):
    """Dos primeras paradas equivalentes: mismo compuesto y vuelta a ±tol."""
    return bool(a and b and a[1] == b[1] and abs(a[0] - b[0]) <= tol)


# --------------------------------------------------------------------------------------------- filtro
class Gate:
    def __init__(self, cfg, mode):
        g = cfg.get("dialog", {})
        self.persist = g.get("persistence", 2)
        self.cooldown_laps = g.get("proposal_cooldown_laps", 3)
        self.decline_laps = g.get("decline_laps", 3)
        self.min_gain = g.get("min_gain_pts", MIN_GAIN.get(mode, 0.7))
        self.change_mult = g.get("change_mult", 1.5)
        self.min_lap = g.get("min_lap", 3)                    # no se propone nada de estrategia antes de esta vuelta...
        self.min_frac = g.get("min_lap_frac", 0.08)           # ...ni antes de esta fracción de la carrera (gana la mayor)
        self.horizon_laps = g.get("horizon_laps", 6)          # solo se propone una parada que llegue en las próximas N vueltas
        self.min_clean_laps = g.get("min_clean_laps", 3)      # vueltas limpias tuyas necesarias para fiarse del ritmo
        self.min_rivals = g.get("min_rivals_with_pace", 5)    # rivales con ritmo medido necesarios (o todos, si hay menos)
        self.streak_stop, self.streak = None, 0
        self.last_proposal_lap = -99
        self.declined = []            # (primera parada, vuelta, mejora)

    def observe(self, best):
        first = best.stops[0] if (best and best.stops) else None
        if first and same_stop(first, self.streak_stop):
            self.streak += 1
        else:
            self.streak_stop, self.streak = first, 1 if first else 0
        return self.streak

    def decline(self, stop, lap, gain):
        self.declined.append((stop, lap, gain))

    def validate(self, lap, sit, cand, urgent, closing, clean_laps, rivals_with_pace, n_rivals):
        """Validador previo: (válida?, motivo). Una propuesta de estrategia solo tiene sentido con datos suficientes, no al
        principio de la carrera y con la parada dentro del horizonte. Lo urgente (SC, daño, bandera roja) salta el principio y
        el horizonte, pero nunca los datos mínimos."""
        if clean_laps < min(self.min_clean_laps, max(2, sit.total_laps // 4)):      # carreras cortas: bastan 2
            return False, f"pocos datos: {clean_laps} vueltas limpias tuyas (mínimo {self.min_clean_laps})"
        need = min(self.min_rivals, n_rivals)
        if rivals_with_pace < need:
            return False, f"pocos datos: ritmo medido de {rivals_with_pace} rivales (mínimo {need})"
        if urgent:
            return True, ""
        start = max(self.min_lap, int(-(-self.min_frac * sit.total_laps // 1)))
        if lap < start:
            return False, f"demasiado pronto (hasta la vuelta {start} no propongo estrategia)"
        if cand is not None and cand.kind == "PIT" and cand.stops and not closing:
            ahead = cand.stops[0][0] - lap
            if ahead > self.horizon_laps:
                return False, f"la parada es en la vuelta {cand.stops[0][0]}, fuera del horizonte de {self.horizon_laps} vueltas"
            if sit.laps_left < 2:
                return False, "quedan menos de 2 vueltas"
        return True, ""

    def decide(self, lap, rep, cand, accepted, urgent, closing, pending=None):
        """(hablar?, motivo) para la opción candidata `cand` (la que se propondría). accepted: Proposal aceptada o None."""
        best = cand
        streak = self.observe(best)
        if best.kind != "PIT" or not best.stops:
            return False, "seguir fuera sigue siendo lo mejor"
        first = best.stops[0]
        if accepted and same_stop(first, accepted.option.stops[0]):
            return False, "coincide con el plan ya aceptado"
        if pending and pending.option.stops and same_stop(first, pending.option.stops[0]) and (not urgent or lap - pending.lap < 3):
            return False, "ya hay una propuesta igual pendiente de respuesta"
        base = rep.baseline
        if accepted:
            acc = next((o for o in rep.options if o.stops and same_stop(o.stops[0], accepted.option.stops[0])), None)
            base = acc or base
        gain = 99.0 if base is None else min(best.utility - base.utility, 99.0)   # 99 = obligatorio
        need = self.min_gain * (self.change_mult if accepted else 1.0)
        if gain < need and not urgent:
            return False, f"mejora de {gain:.1f} pts, menos que el mínimo ({need:.1f})"
        if streak < self.persist and not (urgent or closing):
            return False, f"esperando que la tendencia se mantenga ({streak}/{self.persist})"
        if lap - self.last_proposal_lap < self.cooldown_laps and not urgent:
            return False, "propuesta reciente"
        for stop, l, g in self.declined:
            if same_stop(first, stop, 2) and lap - l < self.decline_laps and not urgent and gain < 2 * max(g, 0.1):
                return False, "rechazada hace poco y la situación no ha cambiado lo bastante"
        if base is None:
            why = "obligatorio por la regla de compuestos"
        else:
            why = f"mejora de {gain:.1f} pts"
        return True, "urgente" if urgent else ("ventana cerrándose" if closing else why)

    def proposed(self, lap):
        self.last_proposal_lap = lap


# ------------------------------------------------------------------------------------------- propuestas
@dataclass
class Proposal:
    id: int
    lap: int
    t: float
    source: str                    # "ingeniero" | "piloto"
    option: S.Option
    frase: str
    expires_lap: int
    gain: float = 0.0
    state: str = "pendiente"       # pendiente | aceptada | rechazada | caducada | ejecutada
    reason: str = ""

    def summary(self):
        return {"id": self.id, "lap": self.lap, "source": self.source, "state": self.state, "desc": self.option.desc,
                "stops": [list(s) for s in self.option.stops], "reason": self.reason, "gain": round(self.gain, 2),
                "e_pos": round(self.option.e_pos, 1)}


class Dialog:
    def __init__(self, cfg, mode):
        self.cfg = cfg
        self.gate = Gate(cfg, mode)
        self.pending = None
        self.accepted = None
        self.history = []
        self.alt = None                # alternativa de momento (undercut/overcut) ofrecida junto a la propuesta
        self.muted_until = -1
        self.last_said = ""
        self._id = 0

    # -- creación
    def propose(self, option, frase, lap, t, source="ingeniero", reason="", gain=0.0):
        first = option.stops[0][0] if option.stops else lap
        if self.pending and self.pending.option.stops and option.stops and same_stop(option.stops[0], self.pending.option.stops[0]):
            return self.pending                       # la misma parada ya está propuesta: no se crea otra con otro número
        self._id += 1
        p = Proposal(self._id, lap, t, source, option, frase, max(first, lap) + 1, gain if gain != float("inf") else 99.0,
                     reason=reason)
        if self.pending:
            self.pending.state = "caducada"
            self.history.append(self.pending.summary())
        self.pending = p
        self.gate.proposed(lap)
        return p

    def text(self, p, n_evidence=2):
        if p.reason.startswith("riesgo"):             # alternativa arriesgada: la frase ya trae sus cifras
            return p.frase + " ¿Lo hacemos?"
        ev = " ".join(p.option.evidence[1:1 + n_evidence])
        who = "Propuesta" if p.source == "ingeniero" else "Si quieres"
        return f"{who}: {p.frase} {ev} Esperado P{p.option.e_pos:.1f}. ¿Lo hacemos?".replace("  ", " ")

    # -- respuestas
    def accept(self, lap):
        p = self.pending
        if not p:
            return "No hay ninguna propuesta pendiente."
        p.state = "aceptada"
        self.accepted, self.pending = p, None
        self.history.append(p.summary())
        return f"De acuerdo, plan confirmado: {p.option.desc.lower()}. Te aviso a tiempo."

    def decline(self, lap):
        p = self.pending
        if not p:
            return "No hay ninguna propuesta pendiente."
        p.state = "rechazada"
        self.gate.decline(p.option.stops[0] if p.option.stops else None, lap, p.gain)
        self.history.append(p.summary())
        self.pending = None
        return "Entendido, seguimos con el plan actual."

    def explain(self):
        p = self.pending or self.accepted
        if not p:
            return "No tengo ninguna propuesta que explicar."
        return " ".join(p.option.evidence[:5])

    # -- tiempo
    def on_lap(self, lap, in_pit=False):
        """Recordatorios del plan aceptado y caducidad de propuestas. Devuelve [(texto, prioridad, clave)]."""
        out = []
        if self.pending and lap > self.pending.expires_lap:
            self.pending.state = "caducada"
            self.history.append(self.pending.summary())
            out.append(("La propuesta anterior ya caducó; sigo evaluando.", 3, "prop_exp"))
            self.pending = None
        a = self.accepted
        if a and a.option.stops:
            stop_lap, comp = a.option.stops[0]
            name = S.NAME.get(comp, "?")
            if lap == stop_lap - 1:
                out.append((f"Recordatorio: entras a boxes la próxima vuelta, {name}.", 2, "plan_rem"))
            elif lap == stop_lap:
                out.append((f"Entra a boxes esta vuelta, {name}.", 1, "plan_now"))
            elif lap > stop_lap and (not in_pit or lap > stop_lap + 1):   # cruzar la meta por el pit lane es la parada, no un fallo
                a.state = "caducada"
                self.history.append(a.summary())
                self.accepted = None
                out.append(("Pasó la ventana del plan aceptado; recalculo.", 2, "plan_miss"))
        return out

    def on_pit_out(self):
        if self.accepted:
            self.accepted.state = "ejecutada"
            self.history.append(self.accepted.summary())
            self.accepted = None
            return "Plan ejecutado. Buen trabajo."
        return None


# ---------------------------------------------------------------------------------------------- objetivos
@dataclass
class Goal:
    kind: str                      # "pos" | "pass" | "hold"
    target: int | None = None      # posición objetivo (pos / hold)
    rival: int | None = None       # coche a pasar (pass)
    name: str = ""
    status: str = "en camino"
    p: float | None = None
    since_lap: int = 0
    pending_status: str | None = None
    pending_count: int = 0
    last_msg_lap: int = -99
    level: int = 0                 # cuántas veces se ha bajado en la escalera

    def desc(self):
        if self.kind == "pass":
            return f"pasar a {self.name}"
        if self.kind == "hold":
            return f"asegurar P{self.target}"
        return f"terminar en P{self.target}"


class Goals:
    def __init__(self, cfg):
        g = cfg.get("dialog", {})
        self.ok, self.risk = g.get("goal_ok_p", 0.55), g.get("goal_risk_p", 0.2)
        self.persist = g.get("goal_persistence", 2)
        self.margin_s = g.get("pass_margin_s", 0.5)
        self.goal = None

    def set_pos(self, n, lap):
        self.goal = Goal("pos", target=n, since_lap=lap, last_msg_lap=lap)
        return self.goal

    def set_pass(self, idx, name, lap):
        self.goal = Goal("pass", rival=idx, name=name, since_lap=lap, last_msg_lap=lap)
        return self.goal

    def clear(self):
        self.goal = None

    def include(self):
        return (self.goal.rival,) if self.goal and self.goal.rival is not None else ()

    def _status(self, p):
        return "en camino" if p >= self.ok else ("en riesgo" if p >= self.risk else "inalcanzable")

    def required(self, me, sit, rivals):
        """Texto con el ritmo necesario para cumplir el objetivo (None si no aplica o faltan datos)."""
        g = self.goal
        rv = None
        if g.kind == "pass":
            rv = next((r for r in rivals if r.idx == g.rival), None)
        elif g.kind in ("pos", "hold") and me.pos > g.target:
            rv = next((r for r in rivals if r.pos == g.target and r.gap < 0), None)
        if not rv or rv.gap >= 0 or not rv.pace or not me.pace or sit.laps_left < 1:
            return None
        per_lap = (abs(rv.gap) + self.margin_s) / sit.laps_left
        need = rv.pace - per_lap
        return (f"{rv.name} está a {abs(rv.gap):.1f} s: para pasarlo necesitas vueltas de {fmt_lap(need)} "
                f"({per_lap:.2f} s mejor que él en cada una) y ahora haces {fmt_lap(me.pace)}.")

    def update(self, opt, sit, me, rivals, lap):
        """Evalúa el objetivo con la opción elegida. Devuelve lista de (texto, prioridad)."""
        g = self.goal
        if g is None or opt is None:
            return []
        out = []
        if g.kind == "pass":
            rv = next((r for r in rivals if r.idx == g.rival), None)
            if rv is None:
                return [(f"No puedo evaluar el objetivo de {g.desc()}: {g.name} no está entre los coches simulados.", 3)]
            if rv.gap >= 0:               # ya está por detrás
                out.append((f"Objetivo cumplido: pasaste a {g.name}. Vamos bien encaminados; ahora a asegurar P{me.pos}.", 2))
                self.goal = Goal("hold", target=me.pos, since_lap=lap, last_msg_lap=lap)
                return out
            p = opt.p_beat.get(g.name)
        else:
            k = max(1, min(22, g.target))
            p = opt.cdf[k - 1] if opt.cdf else None
        if p is None:
            return []
        g.p = p
        new = self._status(p)
        if new != g.status:
            g.pending_count = g.pending_count + 1 if g.pending_status == new else 1
            g.pending_status = new
            if g.pending_count >= self.persist:
                g.status, g.pending_status, g.pending_count = new, None, 0
                msg = f"Objetivo {g.desc()}: {new} ({S.pct(p)}%)."
                req = self.required(me, sit, rivals)
                if req and new != "en camino":
                    msg += " " + req
                out.append((msg, 2))
                g.last_msg_lap = lap
                if new == "inalcanzable":
                    out += self._fallback(me, lap)
        else:
            g.pending_status, g.pending_count = None, 0
            if lap - g.last_msg_lap >= 3:
                g.last_msg_lap = lap
                out.append((f"Objetivo {g.desc()}: {g.status} ({S.pct(p)}%).", 3))
        return out

    def _fallback(self, me, lap):
        g = self.goal
        nxt = me.pos if g.kind != "hold" else min(22, g.target + 1)
        self.goal = Goal("hold", target=nxt, since_lap=lap, level=g.level + 1, last_msg_lap=lap)
        return [(f"El objetivo anterior ya no es realista. Paso a {self.goal.desc()}.", 2)]

    def status_text(self, opt, sit, me, rivals):
        g = self.goal
        if g is None:
            return "No tenemos un objetivo fijado. Dime, por ejemplo, 'quiero acabar quinto' o 'necesito pasar a un piloto'."
        txt = f"Objetivo {g.desc()}: {g.status}" + (f" ({S.pct(g.p)}%)." if g.p is not None else ".")
        req = self.required(me, sit, rivals)
        return txt + (" " + req if req else "")
