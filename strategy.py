#!/usr/bin/env python3
"""Motor de estrategia de carrera (Python puro, sin E/S, solo librería estándar).

Recibe una foto de la carrera (Situation, Me, Rivals) y devuelve opciones ordenadas con probabilidades:
  - genera planes: seguir fuera, 1 parada (vuelta x compuesto), 2 paradas, y variantes que reaccionan al Safety Car
  - los puntúa con una simulación Monte Carlo (Safety Car/VSC, ritmo de cada coche, paradas de la IA, tráfico)
  - devuelve P(victoria), P(podio), posición esperada y puntos esperados de cada plan
El modo (conservador / normal / arriesgado) solo cambia la utilidad con que se ordenan los planes:
la media de potencia de los puntos (exponente < 1 evita riesgo, > 1 lo busca).

Todos los números de entrada salen del juego (telemetría) o de config.toml/profile.json; los parámetros
marcados "estimado" son suposiciones: están en config.toml para que los calibres.
La profundidad depende de la longitud de la carrera (ver TIERS): en 5 vueltas casi no hay estrategia.

  python3 strategy.py --demo       # imprime un ejemplo
"""
from __future__ import annotations
import math, random, time
from dataclasses import dataclass, field, asdict

POINTS = (25, 18, 15, 12, 10, 8, 6, 4, 2, 1)
NAME = {16: "Blando", 17: "Medio", 18: "Duro", 7: "Intermedio", 8: "Lluvia"}
DRY = (16, 17, 18)
OTHER = -1   # marcador: "ya usó otro compuesto" cuando no se conoce el historial de stints
DRYX = DRY + (OTHER,)
DEFAULT_USABLE = {16: 22.0, 17: 38.0, 18: 42.0, 7: 63.0, 8: 44.0}   # vueltas; las reales llegan en TyreSets

# gamma: aversión (<1) o búsqueda (>1) de riesgo. margin: puntos de diferencia dentro de los cuales la IA puede elegir.
MODES = {
    "conservador": {"gamma": 0.7, "margin": 0.3},
    "normal": {"gamma": 1.0, "margin": 0.6},
    "arriesgado": {"gamma": 1.6, "margin": 1.8},
}
# Profundidad según vueltas totales de la carrera: (nombre, máx. vueltas, simulaciones, candidatos de parada, dos paradas)
TIERS = (("sprint", 8, 80, 3, False), ("corta", 20, 100, 6, False),
         ("media", 35, 90, 8, True), ("larga", 10_000, 70, 9, True))


def pct(p):
    """Probabilidad para mostrar: nunca 0% ni 100% (el modelo tiene incertidumbre que no ve)."""
    return max(1, min(99, round(p * 100)))


def tier_of(total_laps):
    for name, mx, nsim, cands, two in TIERS:
        if total_laps <= mx:
            return name, nsim, cands, two
    return TIERS[-1][0], TIERS[-1][2], TIERS[-1][3], TIERS[-1][4]


@dataclass
class Params:
    wear_limit: float = 70.0
    cliff_wear: float = 55.0
    cliff_k: float = 0.006
    pace_loss_per_wear: float = 0.025
    pit_loss_s: float = 22.0
    pit_loss_sc: float = 0.5
    pit_loss_vsc: float = 0.6
    lap_noise_s: float = 0.25
    form_noise_s: float = 0.12
    pass_adv_s: float = 0.6
    clamp_gap_s: float = 0.5
    sc_gap_s: float = 1.0
    sc_prob_lap: tuple = (0.0, 0.01, 0.02, 0.035)
    ai_pit_wear: float = 60.0
    ai_sc_pit_prob: float = 0.8
    min_stop_laps: int = 3
    two_compound_rule: str = "auto"
    use_game_life: bool = True
    aero_loss_per_pct: float = 0.015
    tyre_damage_loss_per_pct: float = 0.01   # s/vuelta por 1% de daño en un neumático (estimado)
    felt_grip_loss_s: float = 0.3            # s/vuelta que se asumen perdidos si el piloto dice que no tiene agarre (estimado)
    seed: int = 7
    off_prior: dict = field(default_factory=lambda: {16: 0.0, 17: 0.84, 18: 1.68, 7: 6.1, 8: 8.5})
    wear_rate_soft: float = 2.6
    ai_next: dict = field(default_factory=dict)    # compuesto actual -> {siguiente: probabilidad}, aprendido de tus capturas

    @classmethod
    def from_cfg(cls, cfg):
        s, pr = dict(cfg.get("strategy", {})), cfg.get("priors", {})
        p = cls()
        for k, v in s.items():
            if hasattr(p, k):
                setattr(p, k, tuple(v) if isinstance(v, list) else v)
        p.off_prior = {16: 0.0, 17: pr.get("off_medium", 0.84), 18: pr.get("off_hard", 1.68),
                       7: pr.get("off_inter", 6.1), 8: pr.get("off_wet", 8.5)}
        p.wear_rate_soft = pr.get("wear_rate_soft", p.wear_rate_soft)
        return p

    def rule_applies(self, total_laps):
        r = str(self.two_compound_rule).lower()
        return r == "true" or (r == "auto" and total_laps >= 12)


@dataclass
class SetInfo:
    visual: int
    wear: float = 0.0          # % de desgaste del juego de neumáticos
    life: float = 0.0          # vueltas de vida que quedan según el juego
    delta_s: float = 0.0       # ritmo de este juego nuevo respecto al montado ahora (s/vuelta; + = más lento)
    count: int = 1


@dataclass
class Me:
    pos: int
    compound: int
    age: float                 # vueltas de uso del neumático montado
    wear: float                # % de la peor rueda
    pace: float                # s, ritmo reciente (vueltas limpias) con el neumático montado
    rate: float | None = None  # %/vuelta de la peor rueda, medido en este stint
    life_game: float | None = None
    sets: list = field(default_factory=list)   # SetInfo disponibles (sin el montado)
    usable: dict = field(default_factory=dict)  # compuesto -> vida útil total del juego (vueltas)
    used: set = field(default_factory=set)     # compuestos secos usados en la carrera (incluye el montado)
    stops: int = 0
    dmg_pace_s: float = 0.0    # s/vuelta perdidos por daño aerodinámico o de neumático (se recupera en boxes)
    dmg_measured: bool = False # True si dmg_pace_s sale de tus vueltas tras el golpe (no de la estimación por %)
    felt_loss_s: float = 0.0   # pérdida que declara el piloto (solo vueltas futuras; se borra al parar)
    pen_pending: str | None = None   # 'drive-through' | 'stop-go' sin cumplir


@dataclass
class Rival:
    idx: int
    name: str
    pos: int
    gap: float                 # s respecto a ti: < 0 va delante, > 0 va detrás
    pace: float | None
    compound: int
    age: float
    wear: float
    rate: float | None = None
    stops: int = 0
    used: set = field(default_factory=set)


@dataclass
class Situation:
    total_laps: int
    lap: int                   # vuelta actual (1 = primera)
    laps_left: float           # incluye lo que falta de la vuelta actual
    sc: int = 0                # 0 verde, 1 Safety Car, 2 VSC
    sc_setting: int = 2
    wet: bool = False
    pit_loss: float = 22.0
    ignored_ahead: int = 0     # coches por delante de ti que no se simulan
    year: int = 2025
    red: bool = False          # bandera roja: cambiar neumáticos no cuesta tiempo


@dataclass
class Option:
    id: str
    kind: str                  # STAY | PIT
    desc: str
    phrase: str
    stops: tuple               # ((vuelta_absoluta, compuesto), ...)
    sc_react: bool
    e_pos: float = 0.0
    p_win: float = 0.0
    p_podium: float = 0.0
    p_points: float = 0.0
    e_pts: float = 0.0
    std_pts: float = 0.0
    utility: float = 0.0
    cdf: list = field(default_factory=list)       # cdf[k-1] = P(terminar en posición <= k)
    p_beat: dict = field(default_factory=dict)    # rival -> P(terminar por delante de él)
    evidence: list = field(default_factory=list)  # razones calculadas por el código (con números)

    def brief(self):
        return {"id": self.id, "descripcion": self.desc, "pos_esperada": round(self.e_pos, 1),
                "prob_victoria_pct": pct(self.p_win), "prob_podio_pct": pct(self.p_podium),
                "puntos_esperados": round(self.e_pts, 1), "motivos": self.evidence[:2]}


@dataclass
class Report:
    tier: str = ""
    mode: str = "normal"
    options: list = field(default_factory=list)
    eligible: list = field(default_factory=list)   # ids que la IA puede elegir (dentro del margen)
    best: Option | None = None
    nsim: int = 0
    elapsed_s: float = 0.0
    notes: list = field(default_factory=list)
    win_view: str = ""
    window: dict = field(default_factory=dict)
    skipped: str = ""
    baseline: Option | None = None   # 'seguir fuera' (referencia para medir cuánto mejora cada plan)
    rivals: list = field(default_factory=list)   # [(nombre, hueco s)] de los coches simulados

    def to_dict(self):
        return {"tier": self.tier, "mode": self.mode, "nsim": self.nsim, "win_view": self.win_view,
                "window": self.window, "notes": self.notes, "skipped": self.skipped,
                "best": self.best.id if self.best else None, "eligible": self.eligible,
                "options": [asdict(o) | {"stops": [list(s) for s in o.stops]} for o in self.options]}


# --------------------------------------------------------------------------------------------------
# Modelo de ritmo: lap = base_fresca + off[compuesto] + k*desgaste + acantilado(desgaste)
# --------------------------------------------------------------------------------------------------
def cliff(p, w):
    return p.cliff_k * (w - p.cliff_wear) ** 2 if w > p.cliff_wear else 0.0


def lap_pace(p, base, off, w):
    return base + off + p.pace_loss_per_wear * w + cliff(p, w)


def make_steps(laps_left):
    n = max(1, math.ceil(laps_left - 1e-9))
    return [laps_left - (n - 1)] + [1.0] * (n - 1)


def utility(positions, gamma):
    """Media de potencia de los puntos (+ un pequeño término de posición para ordenar fuera de los puntos)."""
    tot = 0.0
    for pos in positions:
        eff = (POINTS[pos - 1] if pos <= 10 else 0.0) + max(0, 22 - pos) * 0.05
        tot += eff ** gamma
    return (tot / len(positions)) ** (1.0 / gamma)


@dataclass(frozen=True)
class Plan:
    stops: tuple               # ((paso, compuesto), ...) paso 0 = al final de ESTA vuelta
    react: bool

    @property
    def kind(self):
        return "PIT" if self.stops else "STAY"


class Ctx:
    """Datos precalculados y compartidos por todos los planes y escenarios."""

    def __init__(self, sit, me, rivals, p):
        self.sit, self.me, self.rivals, self.p = sit, me, rivals, p
        self.cars = [me] + rivals
        self.C = len(self.cars)
        self.steps = make_steps(sit.laps_left)
        self.n = len(self.steps)
        self.rule = p.rule_applies(sit.total_laps)
        self.usable = dict(DEFAULT_USABLE) | {k: v for k, v in me.usable.items() if v}
        k, w0 = p.pace_loss_per_wear, me.wear
        off = dict(p.off_prior)
        for s in me.sets:                       # el juego da el ritmo de cada juego nuevo vs el montado
            if s.visual != me.compound:
                derived = off[me.compound] + s.delta_s + k * w0 + cliff(p, w0)
                # guarda: si el valor del juego se aleja mucho del prior, se descarta (dato incoherente)
                if abs(derived - p.off_prior.get(s.visual, derived)) <= 2.5:
                    off[s.visual] = derived
        self.off = off
        self.avail = {}                         # mejor juego disponible por compuesto seco
        for s in me.sets:
            if s.visual in DRY and (s.visual not in self.avail or s.wear < self.avail[s.visual].wear):
                self.avail[s.visual] = s
        # estado inicial por coche (índice 0 = tú)
        self.t0, self.w0, self.comp0, self.base0, self.rate0, self.used0, self.pace0 = [], [], [], [], [], [], []
        for i, c in enumerate(self.cars):
            if i == 0:
                t, pace, comp, w, age, rate, used = 0.0, me.pace, me.compound, me.wear, me.age, me.rate, set(me.used)
            else:
                pace = c.pace if c.pace else me.pace
                t, comp, w, age, rate, used = c.gap, c.compound, c.wear, c.age, c.rate, set(c.used) | {c.compound}
            rate = rate if (rate and rate > 0.05 and (i == 0 or age >= 2)) else self.default_rate(comp, me.rate)
            self.t0.append(t); self.w0.append(w); self.comp0.append(comp); self.rate0.append(rate)
            self.pace0.append(pace); self.used0.append({u for u in used if u in DRYX})
            self.base0.append(pace - k * w - cliff(p, w) - off.get(comp, 0.0) - (me.dmg_pace_s if i == 0 else 0.0))
        self.order0 = sorted(range(self.C), key=self.t0.__getitem__)
        self.tyre_cap = self.laps_to_limit()
        # plan de cada rival: la parada que haría un equipo con el MISMO modelo de ritmo que el tuyo. Antes paraban todos al 60%
        # de desgaste con un neumático al azar y tu plan optimizado les ganaba siempre (sesgo «sales P1»).
        self.ai_plan = [None] + [self._ai_plan(i) for i in range(1, self.C)]

    def _ai_plan(self, i):
        """(paso de parada, compuesto) de menor tiempo total con una parada más, o None si lo mejor es no parar."""
        p, steps, n, k = self.p, self.steps, self.n, self.p.pace_loss_per_wear
        comp, w, r, base = self.comp0[i], self.w0[i], self.rate0[i], self.base0[i]
        if comp not in DRY or n < 2:
            return None
        need = self.rule and len(self.used0[i]) == 1
        cum1, t, wear = [], 0.0, w
        for f in steps:                                 # tiempo acumulado sin parar, paso a paso
            wn = wear + r * f
            wm = (wear + wn) * 0.5
            t += f * (base + self.off[comp] + k * wm + cliff(p, wm))
            cum1.append((t, wn))
            wear = wn
        best = None if (need or cum1[-1][1] > p.wear_limit + 5) else (cum1[-1][0], None)
        for c in DRY:
            if need and c == comp:
                continue
            rc = self.rate_for(i, c)
            cum2, t2 = [0.0], 0.0
            for j in range(n):
                wm = rc * (j + 0.5)
                t2 += base + self.off[c] + k * wm + cliff(p, wm)
                cum2.append(t2)
            for s_ in range(n - 1):
                left = n - 1 - s_
                if cum1[s_][1] > p.wear_limit + 5 or rc * left > p.wear_limit + 5 or left < p.min_stop_laps:
                    continue
                tot = cum1[s_][0] + self.sit.pit_loss + cum2[left]
                if best is None or tot < best[0]:
                    best = (tot, (s_, c))
        return best[1] if best else None

    def default_rate(self, comp, my_rate):
        """Ritmo de desgaste cuando no hay medida: el tuyo escalado por la vida útil del juego, o el prior."""
        u = self.usable
        if my_rate and my_rate > 0.05:
            return my_rate * u.get(self.me.compound, 22.0) / u.get(comp, 22.0)
        return self.p.wear_rate_soft * u[16] / u.get(comp, u[16])

    def rate_for(self, i, comp):
        cur = self.comp0[i]
        return self.rate0[i] * self.usable.get(cur, 22.0) / self.usable.get(comp, 22.0)

    def laps_to_limit(self):
        m = self.me
        by_wear = (self.p.wear_limit - m.wear) / max(self.rate0[0], 0.05)
        if self.p.use_game_life and m.life_game and m.life_game > 0:
            return max(0.0, min(by_wear, m.life_game))
        return max(0.0, by_wear)

    def feasible(self, i, laps_after, used):
        out = []
        for c in DRY:
            if i == 0 and c not in self.avail:
                continue
            if self.rule and len(used) == 1 and c in used:
                continue
            if self.rate_for(i, c) * laps_after <= self.p.wear_limit - 5:
                out.append(c)
        return sorted(out, key=lambda c: self.off[c])

    def pick_compound(self, i, laps_after, used, roll=None, cur=None):
        """Compuesto para el resto de la carrera. Tú: el más rápido que aguanta. Un rival (roll dado): no se sabe qué
        elegirá la IA, así que con prob. 0.5 el más rápido viable y si no uno cualquiera de los viables."""
        ok = self.feasible(i, laps_after, used)
        table = self.p.ai_next.get(cur) if (i != 0 and cur is not None) else None
        if ok and table and roll is not None:           # lo que hace la IA según tus carreras (con un mínimo de azar)
            w = [table.get(c, 0.0) + 0.02 for c in ok]
            x, acc = roll * sum(w), 0.0
            for c, wi in zip(ok, w):
                acc += wi
                if x <= acc:
                    return c
            return ok[-1]
        if ok:
            if roll is None or roll < 0.8:                  # un equipo suele montar el más rápido que aguanta
                return ok[0]
            return ok[min(int((roll - 0.5) * 2 * len(ok)), len(ok) - 1)]
        cands = [c for c in DRY if (i != 0 or c in self.avail) and not (self.rule and len(used) == 1 and c in used)]
        cands = cands or [c for c in DRY if i != 0 or c in self.avail]
        return max(cands, key=lambda c: self.usable.get(c, 0)) if cands else self.comp0[i]


@dataclass
class Scen:
    kind: int = 0
    start: int = 0
    length: int = 0
    form: list = field(default_factory=list)
    noise: list = field(default_factory=list)
    jit: list = field(default_factory=list)
    roll: list = field(default_factory=list)
    rate_mult: list = field(default_factory=list)   # incertidumbre del ritmo de desgaste de cada coche
    pit_mult: float = 1.0                            # incertidumbre del coste de parada
    pick: list = field(default_factory=list)        # dado de cada rival para elegir compuesto en su parada


def gen_scenario(ctx, rng, deterministic=False):
    C, n, p, sit = ctx.C, ctx.n, ctx.p, ctx.sit
    if deterministic:
        return Scen(0, 0, 0, [0.0] * C, [[0.0] * n for _ in range(C)], [0.0] * C, [1.0] * C, [1.0] * C, 1.0, [0.0] * C)
    kind = start = length = 0
    if sit.sc:
        kind, start = sit.sc, 0
        length = rng.randint(1, 4) if kind == 1 else rng.randint(1, 2)
    else:
        pl = p.sc_prob_lap[min(max(sit.sc_setting, 0), 3)]
        if pl > 0 and n >= 3 and rng.random() < 1 - (1 - pl) ** n:
            kind = 1 if rng.random() < 0.7 else 2
            start = rng.randint(0, n - 2)
            length = rng.randint(2, 5) if kind == 1 else rng.randint(1, 2)
    form = [rng.gauss(0, p.form_noise_s) for _ in range(C)]
    noise = [[rng.gauss(0, p.lap_noise_s) for _ in range(n)] for _ in range(C)]
    jit = [rng.uniform(-6, 6) for _ in range(C)]
    roll = [rng.random() for _ in range(C)]
    rate_mult = [max(0.6, rng.gauss(1.0, 0.15)) for _ in range(C)]
    pick = [rng.random() for _ in range(C)]
    return Scen(kind, start, length, form, noise, jit, roll, rate_mult, max(0.6, rng.gauss(1.0, 0.12)), pick)


def run_one(ctx, plan, sc):
    """Simula el resto de la carrera. Devuelve (posición final tuya, tiempo tuyo - mejor rival)."""
    p, n, steps, C = ctx.p, ctx.n, ctx.steps, ctx.C
    t, w, comp = ctx.t0[:], ctx.w0[:], ctx.comp0[:]
    rate = [r * m for r, m in zip(ctx.rate0, sc.rate_mult)]
    used = [set(u) for u in ctx.used0]
    order = ctx.order0[:]
    base, off, k = ctx.base0, ctx.off, p.pace_loss_per_wear
    dmg = ctx.me.dmg_pace_s
    felt = ctx.me.felt_loss_s
    me_stops = list(plan.stops)
    react_done = False
    stopped = [False] * C
    lt = ctx.pace0[:]
    for s in range(n):
        f = steps[s]
        st = sc.kind if (sc.kind and sc.start <= s < sc.start + sc.length) else 0
        if st == 0:
            for i in range(C):
                wn = w[i] + rate[i] * f
                wm = (w[i] + wn) * 0.5
                lt[i] = (base[i] + off[comp[i]] + k * wm + cliff(p, wm) + sc.form[i] + sc.noise[i][s]
                         + (dmg + felt if i == 0 else 0.0))
                t[i] += f * lt[i]
                w[i] = wn
            for j in range(1, C):   # tráfico: sin ventaja de ritmo suficiente no hay adelantamiento
                a, b = order[j - 1], order[j]
                if t[b] < t[a] + p.clamp_gap_s and (lt[a] - lt[b]) < p.pass_adv_s:
                    t[b] = t[a] + p.clamp_gap_s
        else:
            fac = 0.4 if st == 1 else 0.5
            for i in range(C):
                w[i] += rate[i] * f * fac
        # ---- paradas al final de este paso
        left_after = n - 1 - s
        pit_cost = ctx.sit.pit_loss * sc.pit_mult * (1.0 if st == 0 else (p.pit_loss_sc if st == 1 else p.pit_loss_vsc))
        if ctx.sit.red and s == 0 and st == 0:        # bandera roja: parar es gratis para todos
            pit_cost = 0.0
        # tú
        go = None
        if me_stops and me_stops[0][0] == s:
            go = me_stops.pop(0)[1]
        elif plan.react and st and not react_done and left_after >= 4:
            if me_stops and 0 < me_stops[0][0] - s <= 8:
                go = me_stops.pop(0)[1]
                react_done = True
            elif not plan.stops and not stopped[0]:
                go = ctx.pick_compound(0, left_after, used[0])
                react_done = True
        if go is not None and left_after >= 1:
            t[0] += pit_cost
            comp[0], w[0] = go, (ctx.avail[go].wear if go in ctx.avail else 0.0)
            rate[0] = ctx.rate_for(0, go) * sc.rate_mult[0]
            used[0].add(go) if go in DRY else None
            dmg = felt = 0.0
            stopped[0] = True
        # IA: para en su vuelta óptima (± 2 vueltas de variación), antes si hay Safety Car cerca de su ventana, y siempre si las
        # gomas llegan al límite o le falta el segundo compuesto al final
        for i in range(1, C):
            if left_after < 2 or comp[i] not in DRY:
                continue
            plan_i = None if stopped[i] else ctx.ai_plan[i]
            due = plan_i is not None and s >= plan_i[0] + round(sc.jit[i] / 3)
            must = ctx.rule and len(used[i]) == 1 and left_after <= 3
            worn = w[i] >= p.wear_limit + 5
            sc_stop = (bool(st) and plan_i is not None and plan_i[0] - s <= 10 and sc.roll[i] < p.ai_sc_pit_prob
                       and left_after >= 6)
            if due or must or worn or sc_stop:
                c2 = plan_i[1] if (due and plan_i) else ctx.pick_compound(i, left_after, used[i], sc.pick[i], comp[i])
                stopped[i] = True
                t[i] += pit_cost
                comp[i], w[i] = c2, 0.0
                rate[i] = ctx.rate_for(i, c2) * sc.rate_mult[i]
                used[i].add(c2)
        if st == 1:                              # el pelotón se compacta tras el Safety Car
            order = sorted(range(C), key=t.__getitem__)
            for j in range(1, C):
                a, b = order[j - 1], order[j]
                if t[b] > t[a] + p.sc_gap_s:
                    t[b] = t[a] + p.sc_gap_s
        order = sorted(range(C), key=t.__getitem__)
    ahead = sum(1 for i in range(1, C) if t[i] < t[0])
    best_r = min(t[1:]) if C > 1 else t[0] + 99
    return 1 + ahead + ctx.sit.ignored_ahead, t[0] - best_r, [t[i] > t[0] for i in range(1, C)], t


# --------------------------------------------------------------------------------------------------
# Generación de planes
# --------------------------------------------------------------------------------------------------
def build_plans(ctx, cands_n, two_stop, keep=None):
    n, sit, me = ctx.n, ctx.sit, ctx.me
    used = {u for u in me.used if u in DRYX}
    plans = []
    react = sit.sc == 0     # con el SC ya activo la decisión es explícita (parar ahora o no); reaccionar solo aplica a un SC futuro

    def rule_ok(comps):
        return (not ctx.rule) or len(used | set(comps)) >= 2

    if rule_ok(()):
        plans += [Plan((), False)] + ([Plan((), True)] if react else [])
    last_i = n - 2
    urgent = me.dmg_pace_s >= 0.6 or sit.red        # daño grande o bandera roja: vale la pena parar aunque queden pocas vueltas
    if last_i < 0 or (sit.laps_left < ctx.p.min_stop_laps and not urgent) or not ctx.avail or sit.wet:
        return plans
    cap = int(ctx.tyre_cap)
    cands = {0, 1, 2}
    if last_i > 0:
        cands |= {round(x * last_i / max(cands_n - 1, 1)) for x in range(cands_n)}
    for c in (cap - 1, cap):
        if 0 <= c <= last_i:
            cands.add(c)
    if keep and 0 <= keep[0] - sit.lap <= last_i:      # el plan anterior siempre se re-evalúa (estabilidad)
        cands.add(keep[0] - sit.lap)
    cands = sorted(c for c in cands if 0 <= c <= last_i)
    comps = sorted(ctx.avail)
    for i in cands:
        for c in comps:
            if rule_ok((c,)):
                plans.append(Plan(((i, c),), react))
    if two_stop and n >= 14:
        s1 = max(1, min(cap, round((n - 1) / 3)))
        for i1 in {s1, max(1, s1 - 2)}:
            i2 = min(last_i, i1 + round((n - 1 - i1) / 2))
            if i2 - i1 < 4:
                continue
            for c1 in comps:
                for c2 in comps:
                    if rule_ok((c1, c2)):
                        plans.append(Plan(((i1, c1), (i2, c2)), react))
    return plans


def describe(plan, sit, ctx):
    lap = sit.lap
    if not plan.stops:
        if plan.react:
            return ("Seguir fuera; si sale el Safety Car, parar", "Sigue fuera; si sale el Safety Car, entra a boxes.")
        return ("Seguir fuera hasta el final", "Sigue fuera hasta el final.")
    parts, ph = [], []
    for k, (i, c) in enumerate(plan.stops):
        if i == 0:
            parts.append(f"parar esta vuelta a {NAME[c]}")
            ph.append(f"entra a boxes esta vuelta, {NAME[c]}")
        else:
            parts.append(f"parar en la vuelta {lap + i} a {NAME[c]}")
            ph.append(f"entra a boxes en la vuelta {lap + i}, {NAME[c]}")
    txt = ", luego ".join(parts)
    pht = "; luego ".join(ph)
    if len(plan.stops) == 2:
        txt = "Dos paradas: " + txt
        pht = "Dos paradas: " + pht
    else:
        txt, pht = txt[0].upper() + txt[1:], pht[0].upper() + pht[1:]
    return (txt, pht + ".")


def window(ctx):
    """Cuándo dejan de dar los neumáticos montados (vueltas absolutas)."""
    lap, cap = ctx.sit.lap, ctx.tyre_cap
    return {"limit_lap": lap + int(cap), "tyre_laps_left": round(cap, 1),
            "reaches_end": cap >= ctx.sit.laps_left - 0.5}


def tactical_notes(ctx):
    """Observaciones de undercut/overcut. Son lecturas del modelo, no órdenes."""
    p, sit, me = ctx.p, ctx.sit, ctx.me
    notes = []
    if not ctx.avail or sit.laps_left < 4 or sit.wet:
        return notes
    best_c = min(ctx.avail, key=lambda c: ctx.off[c])
    base_me = ctx.base0[0]

    def fresh_lap(j):               # tu vuelta j con juego nuevo del compuesto más rápido
        w = ctx.rate_for(0, best_c) * (j - 0.5)
        return base_me + ctx.off[best_c] + p.pace_loss_per_wear * w + cliff(p, w)

    def rival_lap(i, j):
        w = ctx.w0[i] + ctx.rate0[i] * (j - 0.5)
        return ctx.base0[i] + ctx.off.get(ctx.comp0[i], 0) + p.pace_loss_per_wear * w + cliff(p, w)

    group = []
    for r in sorted((r for r in ctx.rivals if r.gap < 0), key=lambda x: -x.gap):
        if abs(r.gap) > 6.0:
            break
        i = ctx.cars.index(r)
        gain = sum(rival_lap(i, j) - fresh_lap(j) for j in (1, 2))
        if gain - abs(r.gap) > 0.3:
            group.append((r, gain))
    if group:
        names = ", ".join(f"{r.name} (a {abs(r.gap):.1f} s)" for r, _ in group[:3])
        notes.append(f"Undercut posible sobre {names}: con neumático nuevo ganarías unos {group[0][1]:.1f} s en 2 vueltas.")
    if me.pen_pending and sit.sc:
        notes.append(f"Con el {'Safety Car' if sit.sc == 1 else 'VSC'} cumplir el {me.pen_pending} pendiente cuesta menos: "
                     "aprovecha esta vuelta.")
    behind = [r for r in ctx.rivals if r.gap > 0]
    if behind:
        r = min(behind, key=lambda x: x.gap)
        i = ctx.cars.index(r)
        if r.gap <= 4.0 and (r.wear >= 25 or r.age >= 8):
            my_next = lambda j: lap_pace(p, base_me, ctx.off[me.compound], me.wear + ctx.rate0[0] * (j - 0.5))
            their_fresh = lambda j: ctx.base0[i] + ctx.off[best_c] + p.pace_loss_per_wear * ctx.rate_for(i, best_c) * (j - 0.5)
            gain = sum(my_next(j) - their_fresh(j) for j in (1, 2))
            if gain - r.gap > 0.3:
                notes.append(f"Amenaza de undercut de {r.name} (a {r.gap:.1f} s): si para ahora te pasaría en pista.")
    return notes


def p_range(cdf, lo=0.1, hi=0.9):
    a = next((k + 1 for k, v in enumerate(cdf) if v >= lo), 22)
    b = next((k + 1 for k, v in enumerate(cdf) if v >= hi), 22)
    return a, b


def build_evidence(ctx, o, base, rivals):
    """Razones de una opción, calculadas por el código con los mismos números que usó la simulación."""
    p, me, sit = ctx.p, ctx.me, ctx.sit
    ev = []
    cap = ctx.tyre_cap
    ev.append(f"Tus {NAME.get(me.compound, '?')} llevan {me.age:.0f} vueltas y {me.wear:.0f}% de desgaste; " +
              (f"llegan al límite ({p.wear_limit:.0f}%) en unas {cap:.0f} vueltas y quedan {sit.laps_left:.0f}."
               if cap < sit.laps_left else f"aguantan hasta el final (límite {p.wear_limit:.0f}%)."))
    if base is None and ctx.rule and o.stops:
        ev.append("Es obligatorio usar otro compuesto (regla de dos compuestos).")
    ahead = sorted(((o.p_beat.get(r.name, 0), r.name) for r in rivals if r.gap < 0 and o.p_beat.get(r.name, 0) >= 0.5),
                   reverse=True)[:3]
    if ahead:
        ev.append("Acabarías por delante de " + ", ".join(f"{n} ({pct(v)}%)" for v, n in ahead) + ".")
    lose = sorted(((1 - o.p_beat.get(r.name, 1), r.name) for r in rivals if r.gap > 0 and o.p_beat.get(r.name, 1) < 0.5),
                  reverse=True)[:3]
    if lose:
        ev.append("Acabarían por delante de ti: " + ", ".join(f"{n} ({pct(v)}%)" for v, n in lose) + ".")
    if base is not None and o is not base:
        ev.append(f"Frente a seguir fuera: {o.e_pts - base.e_pts:+.1f} puntos esperados "
                  f"(posición {o.e_pos:.1f} frente a {base.e_pos:.1f}).")
    if o.stops and me.dmg_pace_s >= 0.2:
        ev.append(f"El daño te cuesta ~{me.dmg_pace_s:.1f} s por vuelta ({'medido en tus vueltas' if me.dmg_measured else 'estimado'})"
                  " y solo se recupera en boxes.")
    if o.stops and me.felt_loss_s > 0:
        ev.append(f"Incluyo los {me.felt_loss_s:.1f} s por vuelta que dices perder con las gomas actuales.")
    if o.kind == "STAY" and o.sc_react:
        ev.append("Si sale el Safety Car, parar cuesta la mitad.")
    if o.stops:
        lap0, c0 = o.stops[0]
        i0 = lap0 - sit.lap
        stt = sit.sc if i0 == 0 else 0
        cost = sit.pit_loss * (1.0 if not stt else (p.pit_loss_sc if stt == 1 else p.pit_loss_vsc))
        if sit.red and i0 == 0:
            cost = 0.0
        ev.append(f"Parar {'esta vuelta' if i0 == 0 else f'en la vuelta {lap0}'} cuesta unos {cost:.0f} s" +
                  (" (con Safety Car/VSC cuesta menos)." if stt else (" (bandera roja: gratis)." if cost == 0 else ".")))
        if c0 in ctx.off:
            fresh = ctx.base0[0] + ctx.off[c0] + p.pace_loss_per_wear * ctx.rate_for(0, c0) * 0.5
            gain = me.pace - fresh
            if abs(gain) > 0.1:
                ev.append(f"Con {NAME[c0]} nuevos {'ganas' if gain > 0 else 'pierdes'} ~{abs(gain):.1f} s por vuelta "
                          "frente a tus gomas actuales.")
    a, b = p_range(o.cdf)
    ev.append(f"Rango probable de llegada: P{a} a P{b}.")
    return ev


def win_view(best, me):
    if best is None:
        return ""
    pw = best.p_win
    if me.pos == 1 and pw >= 0.5:
        return f"Lideras con {pct(pw)}% de ganar: defiende la posición."
    if pw >= 0.25:
        return f"Lucha por la victoria: {pct(pw)}% de probabilidad."
    if pw >= 0.05:
        return f"Victoria posible solo con ayuda (Safety Car o errores): {pct(pw)}%."
    if best.p_podium >= 0.3:
        return f"Victoria improbable ({pct(pw)}%), el podio sí está al alcance: {pct(best.p_podium)}%."
    return f"Victoria improbable ({pct(pw)}%): asegura la mejor posición."


def evaluate(sit, me, rivals, params=None, mode="normal", nsim=None, seed=None, budget_s=4.0, keep=None):
    """Punto de entrada. Devuelve un Report; si faltan datos, Report.skipped explica por qué."""
    p = params or Params()
    t0 = time.time()
    tier, nsim_t, cands_n, two = tier_of(sit.total_laps)
    rep = Report(tier=tier, mode=mode)
    if sit.wet:
        rep.skipped = "pista mojada: sin simulación (el modelo de ritmo es para seco); decide por reglas"
        return rep
    if me.pace is None or me.pace <= 0:
        rep.skipped = "faltan vueltas limpias para medir tu ritmo (completa 1-2 vueltas)"
        return rep
    gamma = MODES.get(mode, MODES["normal"])["gamma"]
    margin = MODES.get(mode, MODES["normal"])["margin"]
    ctx = Ctx(sit, me, rivals, p)
    plans = build_plans(ctx, cands_n, two, keep)
    if not plans:
        rep.skipped = "ningún plan cumple las reglas (compuestos obligatorios)"
        return rep
    rng = random.Random((p.seed if seed is None else seed))
    # Etapa 1: ranking determinista barato para quedarse con los planes más prometedores
    det = gen_scenario(ctx, rng, deterministic=True)
    scored = sorted(((run_one(ctx, pl, det), pl) for pl in plans), key=lambda x: (x[0][0], x[0][1]))
    max_stops = 12 if two else 8
    kept = [pl for pl in plans if not pl.stops]                       # "seguir fuera" siempre se evalúa
    kept += [pl for _, pl in scored if pl.stops][:max_stops]
    for c in DRY:                                                      # «parar ya» siempre se evalúa (el piloto puede pedirlo)
        now = next((pl for _, pl in scored if pl.stops and pl.stops[0] == (0, c)), None)
        if now is not None and now not in kept:
            kept.append(now)
    if keep is not None:                                               # el plan anterior también (estabilidad)
        for pl in plans:
            if pl.stops and (sit.lap + pl.stops[0][0], pl.stops[0][1]) == tuple(keep) and pl not in kept:
                kept.append(pl)
    # Etapa 2: Monte Carlo con los mismos escenarios para todos (comparación justa)
    N = nsim or nsim_t
    results = {pl: [] for pl in kept}
    beats = {pl: [0] * len(rivals) for pl in kept}
    done = 0
    for _ in range(N):
        sc = gen_scenario(ctx, rng)
        for pl in kept:
            pos, _, bm, _ = run_one(ctx, pl, sc)
            results[pl].append(pos)
            b = beats[pl]
            for j, x in enumerate(bm):
                if x:
                    b[j] += 1
        done += 1
        if time.time() - t0 > budget_s and done >= 20:
            break
    opts = []
    for pl, poss in results.items():
        d, ph = describe(pl, sit, ctx)
        pts = [(POINTS[x - 1] if x <= 10 else 0) for x in poss]
        mean = sum(pts) / len(pts)
        o = Option("", pl.kind, d, ph, tuple((sit.lap + i, c) for i, c in pl.stops), pl.react,
                   e_pos=sum(poss) / len(poss), p_win=sum(1 for x in poss if x == 1) / len(poss),
                   p_podium=sum(1 for x in poss if x <= 3) / len(poss),
                   p_points=sum(1 for x in poss if x <= 10) / len(poss), e_pts=mean,
                   std_pts=math.sqrt(sum((x - mean) ** 2 for x in pts) / len(pts)),
                   utility=utility(poss, gamma),
                   cdf=[sum(1 for x in poss if x <= k) / len(poss) for k in range(1, 23)],
                   p_beat={r.name: beats[pl][j] / len(poss) for j, r in enumerate(rivals)})
        opts.append(o)
    opts.sort(key=lambda o: (-o.utility, o.e_pos))
    for k, o in enumerate(opts):
        o.id = chr(65 + k)
    rep.options, rep.best, rep.nsim = opts, opts[0], done
    rep.eligible = [o.id for o in opts if opts[0].utility - o.utility <= margin]
    rep.baseline = next((o for o in opts if o.kind == "STAY" and not o.sc_react), None) or \
        next((o for o in opts if o.kind == "STAY"), None)
    rep.rivals = [(r.name, round(r.gap, 1)) for r in rivals]
    for o in opts:
        o.evidence = build_evidence(ctx, o, rep.baseline, rivals)
    rep.window = window(ctx)
    rep.notes = tactical_notes(ctx)
    rep.win_view = win_view(opts[0], me)
    rep.elapsed_s = time.time() - t0
    return rep


# --------------------------------------------------------------------------------------------------
def _demo():
    me = Me(pos=5, compound=16, age=9, wear=34.0, pace=92.6, rate=3.6, life_game=12,
            sets=[SetInfo(17, 0, 38, 0.2), SetInfo(18, 0, 42, 0.9)], used={16}, stops=0)
    rivals = [Rival(1, "Norris", 1, -14.0, 92.1, 17, 14, 38, 2.7, 0, {17}),
              Rival(2, "Verstappen", 2, -9.0, 92.3, 17, 14, 36, 2.6, 0, {17}),
              Rival(3, "Leclerc", 3, -5.5, 92.4, 16, 9, 30, 3.3, 0, {16}),
              Rival(4, "Alonso", 4, -2.1, 92.8, 16, 9, 31, 3.4, 0, {16}),
              Rival(5, "Piastri", 6, 1.4, 92.9, 16, 9, 33, 3.6, 0, {16}),
              Rival(6, "Gasly", 7, 4.0, 93.3, 18, 9, 18, 1.6, 0, {18})]
    sit = Situation(total_laps=30, lap=10, laps_left=21.0, sc=0, sc_setting=2)
    for mode in MODES:
        rep = evaluate(sit, me, rivals, Params(), mode=mode)
        print(f"\n== modo {mode} | tramo {rep.tier} | {rep.nsim} simulaciones en {rep.elapsed_s:.1f}s")
        print(rep.win_view)
        for o in rep.options[:5]:
            print(f"  {o.id}) {o.desc:55s} pos {o.e_pos:4.1f} | gana {pct(o.p_win):3d}% podio {pct(o.p_podium):3d}% "
                  f"| pts {o.e_pts:4.1f} u={o.utility:4.1f}{'  <-- elegible' if o.id in rep.eligible else ''}")
        for n in rep.notes:
            print("  nota:", n)


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        _demo()
    else:
        print(__doc__)
