#!/usr/bin/env python3
"""Memoria entre sesiones y plan de carrera previo, con lo MEDIDO en entrenos y clasificación.

La memoria guarda, por circuito, lo que midieron tus tandas largas (ritmo y desgaste por compuesto), la parada medida y la
mejor vuelta. Con `path` se guarda en disco (data/memory.json, fuera de git) y sobrevive a cerrar el programa; sin borrado por
antigüedad (decisión de diseño, 11 nov 2026: medir primero cuánto ocupa). Si los datos son de otro día, el plan lo dice.

El plan previo NO es el Monte Carlo de la carrera (no conoce a los rivales): es una comparación determinista de paradas con tu
ritmo medido, con las mismas fórmulas de desgaste que usa la estrategia. Dice de qué datos sale y qué le falta.
"""
import json
import statistics
import time
from pathlib import Path

import practice as PR
import strategy as S

OLD_S = 8 * 3600           # datos más viejos que esto se usan, pero el plan avisa de que son de otro día
MIN_RACE_LAPS, MAX_RACE_LAPS = 3, 80
WINDOW = (0.30, 0.75)        # fracción de la carrera donde se busca la parada de un plan de una parada
PRIOR_WEAR = {16: 1.0, 17: 0.65, 18: 0.45}   # desgaste relativo al blando cuando no hay tanda cerrada de ese compuesto (estimado)


class SessionMemory:
    def __init__(self, clock=time.time, path=None):
        self.clock = clock
        self.path = Path(path) if path else None
        self.tracks = {}
        if self.path and self.path.exists():
            try:
                self.tracks = _decode(json.loads(self.path.read_text(encoding="utf-8")))
            except (ValueError, OSError, KeyError, TypeError):
                self.tracks = {}                        # archivo roto: se empieza de cero en vez de cerrar el programa

    def save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(_encode(self.tracks), ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)                      # escritura atómica: un cierre a medias no deja el archivo roto
        except OSError:
            pass

    def age_s(self, tid):
        m = self.tracks.get(tid)
        return self.clock() - m["t"] if m else None

    def _t(self, tid):
        return self.tracks.setdefault(tid, {"tables": {}, "pit_loss": [], "best": None, "race_laps": None, "t": 0.0, "from": []})

    def record(self, old):
        """Guarda lo medido en una sesión que acaba de terminar (old: el RaceTracker con su estado)."""
        tid = (old.ses or {}).get("track_id")
        if tid is None:
            return None
        m = self._t(tid)
        kind = old.session_kind()
        rows = PR.compound_table(old) if old.hist else []
        for r in rows:                                  # por compuesto, la tanda más larga medida
            cur = m["tables"].get(r["comp"])
            if cur is None or r["laps"] >= cur["laps"]:
                m["tables"][r["comp"]] = {**r, "kind": kind}
        import state
        pl = state.measure_pit_loss(old.hist) if old.hist else []
        if pl:
            m["pit_loss"] = list(pl)
        q = PR.quali_estimate(old) if old.hist else None
        if q and (m["best"] is None or q["best_ms"] < m["best"]["best_ms"]):
            m["best"] = q
        m["t"] = self.clock()
        m["from"].append(kind)
        self.save()
        return m

    def get(self, tid):
        m = self.tracks.get(tid)
        if not m or not m["tables"]:
            return None
        return m

    def view(self, tr):
        """Lo guardado de sesiones anteriores más lo que lleva medido la sesión en curso (sin guardarlo todavía)."""
        tid = (tr.ses or {}).get("track_id")
        if tid is None:
            return None
        old = self.get(tid)
        m = {"tables": dict(old["tables"]) if old else {}, "pit_loss": list(old["pit_loss"]) if old else [],
             "race_laps": old["race_laps"] if old else self.tracks.get(tid, {}).get("race_laps")}
        if tr.hist and not tr.is_race():
            for r in PR.compound_table(tr):
                cur = m["tables"].get(r["comp"])
                if cur is None or r["laps"] >= cur["laps"]:
                    m["tables"][r["comp"]] = {**r, "kind": tr.session_kind()}
        return m if m["tables"] else None

    def pit_loss(self, tid):
        """Pérdida de una parada medida en sesiones anteriores de este circuito (mediana, mínimo 3 muestras) o None."""
        m = self.get(tid)
        pl = m["pit_loss"] if m else []
        return statistics.median(pl) if len(pl) >= 3 else None

    def set_race_laps(self, tid, n):
        self._t(tid)["race_laps"] = int(n)
        self.save()


def _encode(tracks):
    """JSON no admite claves numéricas: circuito y compuesto se guardan como texto."""
    return {str(tid): {**m, "tables": {str(c): r for c, r in m["tables"].items()}} for tid, m in tracks.items()}


def _decode(raw):
    return {int(tid): {**m, "tables": {int(c): {**r, "comp": int(r["comp"])} for c, r in m["tables"].items()}}
            for tid, m in raw.items()}


def prior_tables(p):
    """Tabla de compuestos con los valores por defecto (diferencias de ritmo y desgaste de config.toml [priors]): nada medido."""
    return {c: {"comp": c, "pace": p.off_prior[c], "laps": 0, "trend": None, "wear_rate": None} for c in (16, 17, 18)}


def wear_rates(tables, p):
    """% de desgaste por vuelta de cada compuesto: el medido en tandas cerradas; si no, el del blando medido escalado (estimado)."""
    out, est = {}, set()
    soft = tables.get(16, {}).get("wear_rate")
    for comp, r in tables.items():
        if r.get("wear_rate"):
            out[comp] = r["wear_rate"]
    for comp in tables:
        if comp in out:
            continue
        base = soft or p.wear_rate_soft
        out[comp] = base * PRIOR_WEAR.get(comp, 0.65) / PRIOR_WEAR[16]
        est.add(comp)
    return out, est


def _stint_cost(p, base, off, rate, laps, start_age=0):
    return sum(S.lap_pace(p, base, off, rate * (start_age + k + 0.5)) for k in range(laps))


def options(total_laps, tables, p, pit_loss, rule=None, limit=None, top=3, start=None):
    """Mejores planes de 0, 1 o 2 paradas con los compuestos medidos: [{stints:[(comp,laps)], time, stops, notes}] por tiempo total."""
    comps = [c for c in tables if c in S.NAME]
    if not comps or total_laps < MIN_RACE_LAPS:
        return [], {}
    limit = limit or p.wear_limit
    rate, est = wear_rates(tables, p)
    # el ritmo medido es la media de una tanda con gomas ya gastadas; _stint_cost vuelve a sumar el desgaste desde 0, así que
    # se lleva cada ritmo a «goma nueva» (media de la tanda ≈ la mitad de sus vueltas). Antes el desgaste contaba dos veces.
    fresh = {c: tables[c]["pace"] - p.pace_loss_per_wear * rate[c] * tables[c].get("laps", 0) / 2 for c in comps}
    base = min(fresh.values())
    off = {c: fresh[c] - base for c in comps}
    life = {c: max(2, int(limit / rate[c])) for c in comps}                 # vueltas hasta el límite de desgaste
    need2 = p.rule_applies(total_laps) if rule is None else rule
    res = []

    def add(stints):
        if need2 and len({c for c, _ in stints}) < 2:
            return
        if start is not None and stints[0][0] != start:            # empezar con el neumático que ya llevas montado
            return
        if any(n > life[c] for c, n in stints) or any(n < p.min_stop_laps for _, n in stints[:-1]):
            return
        t = sum(_stint_cost(p, base, off[c], rate[c], n) for c, n in stints) + pit_loss * (len(stints) - 1)
        res.append({"stints": stints, "time": t, "stops": len(stints) - 1})

    for a in comps:
        add([(a, total_laps)])
    for a in comps:
        for b in comps:
            if need2 and a == b:
                continue
            lo, hi = max(p.min_stop_laps, round(WINDOW[0] * total_laps)), min(total_laps - p.min_stop_laps, round(WINDOW[1] * total_laps))
            for l in range(lo, hi + 1):                               # la parada cae en la zona habitual de la carrera, no en la vuelta 3
                add([(a, l), (b, total_laps - l)])
    if total_laps >= 30:                                            # dos paradas solo en carreras largas
        for a in comps:
            for b in comps:
                for c in comps:
                    for l1 in range(8, total_laps - 14, 2):
                        for l2 in range(8, total_laps - l1 - 5, 2):
                            add([(a, l1), (b, l2), (c, total_laps - l1 - l2)])
    res.sort(key=lambda r: r["time"])
    picked = []
    for r in res:                                                   # opciones distintas: otra secuencia de compuestos o parada a 3+ vueltas
        r["delta"] = r["time"] - res[0]["time"]
        if picked and r["delta"] < 1.0 and sorted(c for c, _ in r["stints"]) == sorted(c for c, _ in picked[0]["stints"]):
            continue                                                # el mismo plan con los tramos al revés (empate) no es una alternativa
        if picked and r["stops"] > picked[0]["stops"] and len({c for c, _ in r["stints"]}) == 1 and not need2:
            continue                                                # parar para montar el mismo compuesto no es una alternativa útil
        if all(r["stops"] != q["stops"] or [c for c, _ in r["stints"]] != [c for c, _ in q["stints"]] or
               abs(r["stints"][0][1] - q["stints"][0][1]) >= 3 for q in picked):
            picked.append(r)
        if len(picked) >= top:
            break
    return picked, {"rate": rate, "est": est, "life": life, "off": off, "base": base}


def short(plan, full, n):
    """Lo que se dice por radio al empezar la carrera: UNA frase con el plan. El texto completo (alternativa, avisos de lo que no incluye) sigue en el
    registro y se oye al preguntar por la estrategia. Informe del 11 nov (sprint): el completo eran 51 palabras ≈ 19 s de voz en la vuelta 1 y
    retrasaba 14 s las alertas de contacto y de adelantamiento."""
    try:
        lead = ""
        if full.startswith("Ojo:"):                         # datos viejos: esa advertencia sí se conserva
            lead = full.split(". ")[0] + ". "
        # de dónde sale el plan SÍ se dice (medido en entrenos o estimado): cambia cuánto fiarse de él
        origen = ", sin datos de entrenos" if plan.get("estimated") else " con lo medido en entrenos"
        kind = "estimado" if plan.get("estimated") else "previo"
        return f"{lead}Plan {kind} para {int(n)} vueltas{origen}: {stints_txt(plan)}."
    except (KeyError, TypeError, IndexError, AttributeError, ValueError):
        return full


def stints_txt(o):
    """«Sal con Blando, para en la vuelta 20 y monta Medio» (como lo diría un ingeniero por radio)."""
    st = o["stints"]
    if len(st) == 1:
        return f"sal con {S.NAME.get(st[0][0], '?')} y no pares"
    lap, stops = 0, []
    for c, n in st[:-1]:
        lap += n
        stops.append(lap)
    nxt = [S.NAME.get(c, "?") for c, _ in st[1:]]
    if len(st) == 2:
        return f"sal con {S.NAME.get(st[0][0], '?')}, para en la vuelta {stops[0]} y monta {nxt[0]}"
    return (f"sal con {S.NAME.get(st[0][0], '?')}, para en la vuelta {stops[0]} a {nxt[0]} y en la {stops[1]} a {nxt[1]}")


def describe(opts, info, total_laps, pit_loss, pit_src, tables, estimated=False):
    """Frases del plan previo con su origen y lo que no se sabe."""
    if not opts:
        return None
    nm = lambda c: S.NAME.get(c, "?")
    best = opts[0]
    txt = [f"Plan estimado para {total_laps} vueltas, sin datos de entrenos (ritmo y desgaste por defecto): {stints_txt(best)}."
           if estimated else f"Plan previo para {total_laps} vueltas con lo medido en entrenos: {stints_txt(best)}."]
    if len(opts) > 1:
        a = opts[1]
        txt.append(f"Alternativa: {stints_txt(a)} (+{a['delta']:.1f} s en total).")
    meas = [nm(c) for c in info["rate"] if c not in info["est"]]
    est = [nm(c) for c in info["est"]]
    why = f"Parada de {pit_loss:.0f} s ({pit_src})." if any(o["stops"] for o in opts) else "Sin paradas de neumáticos en el plan principal."
    if est and not estimated:
        why += f" El desgaste de {', '.join(est)} es estimado: no lo rodaste hasta el final."
    txt.append(why + " No incluye rivales, Safety Car ni tráfico: durante la carrera lo recalculo.")
    return " ".join(txt)


def build(eng, total_laps=None, start_comp=None):
    """Texto del plan previo para la carrera, o (None, motivo)."""
    tr = eng.tr
    tid = (tr.ses or {}).get("track_id")
    m = eng.memory.view(tr)
    estimated = m is None
    if estimated:                                       # sin entrenos: plan con valores por defecto, y se dice
        m = {"tables": prior_tables(eng.tr.params), "pit_loss": [], "race_laps": None}
    n = total_laps or ((tr.ses or {}).get("total_laps") if tr.is_race() else None) or m.get("race_laps")
    if not n or not (MIN_RACE_LAPS <= n <= MAX_RACE_LAPS):
        return None, "no sé de cuántas vueltas es la carrera: dímelo (por ejemplo «carrera de 18 vueltas»)"
    p = eng.tr.params
    pl = m["pit_loss"]
    if len(pl) >= 2:
        pit_loss, src = statistics.median(pl), "medida"
    else:
        pit_loss, src = tr.pit_loss() if tr.ses else (p.pit_loss_s, "valor por defecto")
    opts, info = options(int(n), m["tables"], p, pit_loss)
    if not opts:
        return None, "con los compuestos que rodaste no hay un plan que cumpla las reglas; rueda otro compuesto"
    eng._last_plan = opts[0]
    txt = describe(opts, info, int(n), pit_loss, src, m["tables"], estimated)
    if estimated:
        eng._last_plan = dict(opts[0], estimated=True)
    if start_comp in m["tables"] and opts[0]["stints"][0][0] != start_comp:    # ya llevas otro neumático montado
        mine, _ = options(int(n), m["tables"], p, pit_loss, start=start_comp, top=1)
        if mine:                                     # el plan que se sigue es el del neumático montado; el ideal, como nota
            o, ideal = mine[0], opts[0]
            eng._last_plan = dict(o, estimated=estimated)
            rest = stints_txt(o).split(", ", 1)[1] if len(o["stints"]) > 1 else "no pares"
            head = "Plan estimado (sin datos de entrenos)" if estimated else "Plan previo con lo medido en entrenos"
            lost = o["time"] - ideal["time"]
            txt = (f"{head} para {int(n)} vueltas: con el {S.NAME[start_comp]} que llevas, {rest}. "
                   f"Si aún puedes elegir, {stints_txt(ideal)}" + (f" ({lost:.1f} s mejor). " if lost >= 0.1 else " (lo mismo). ")
                   + txt[min(i for i in (txt.find("Parada de"), txt.find("Sin paradas"), txt.find("No incluye")) if i >= 0):])
    age = eng.memory.age_s(tid)
    if age is not None and age > OLD_S:                 # datos de otro día: puede haber cambiado el reglaje o el clima
        days = age / 86400
        txt = (f"Ojo: uso datos de hace {days:.0f} días." if days >= 1.5 else f"Ojo: uso datos de hace {age / 3600:.0f} horas.") + " " + txt
    return txt, None
