#!/usr/bin/env python3
"""Parar antes (undercut) o después (overcut): alternativas de momento para la MISMA parada, con números de la simulación.

Una alternativa solo se ofrece si cuesta poco en puntos esperados y mejora claramente la probabilidad de acabar por
delante de un rival concreto (p_beat de la simulación). Se pregunta una vez por ventana de parada; después, solo si el
piloto lo pide. El aire sucio medido (si hay datos) se usa para explicar el overcut/undercut, nunca como aviso suelto.
"""
import statistics

import strategy as S

MAX_COST = 0.4            # puntos esperados que puede costar la alternativa respecto al plan
MIN_DP = 0.15             # mejora mínima de P(acabar por delante) del rival objetivo
SHIFT = (1, 2, 3)         # vueltas de adelanto o retraso que se estudian


def pmf(cdf):
    return [b - a for a, b in zip([0.0] + list(cdf[:-1]), cdf)]


def risk_utility(o, gamma=S.MODES["arriesgado"]["gamma"]):
    """Utilidad arriesgada (la del modo 'arriesgado') calculada desde la distribución de llegada de la opción."""
    tot = 0.0
    for k, w in enumerate(pmf(o.cdf), 1):
        eff = (S.POINTS[k - 1] if k <= 10 else 0.0) + max(0, 22 - k) * 0.05
        tot += w * eff ** gamma
    return tot ** (1.0 / gamma) if tot > 0 else 0.0


def alternatives(rep, chosen, lap, direction=None, max_cost=MAX_COST, min_dp=MIN_DP):
    """Alternativas de momento para la parada de `chosen`, de mejor a peor.
    direction: "antes" | "despues" | None (ambas). Cada una: {option, kind, rival, p_alt, p_plan, cost, risk_gain, shift}."""
    if chosen is None or chosen.kind != "PIT" or not chosen.stops:
        return []
    lap0, comp = chosen.stops[0]
    out = []
    for o in rep.options:
        if not o.stops or o.id == chosen.id or o.stops[0][1] != comp or len(o.stops) != len(chosen.stops):
            continue
        d = o.stops[0][0] - lap0
        if abs(d) not in SHIFT or o.stops[0][0] < lap or (direction == "antes" and d > 0) or (direction == "despues" and d < 0):
            continue
        if chosen.utility - o.utility > max_cost:
            continue
        # rival cuya probabilidad de ser superado más mejora con el cambio (y que sigue siendo plausible)
        best = None
        for r, p in o.p_beat.items():
            gain = p - chosen.p_beat.get(r, 0.0)
            if gain >= min_dp and 0.3 <= p <= 0.97 and (best is None or gain > best[1]):
                best = (r, gain, p, chosen.p_beat.get(r, 0.0))
        if best is None:
            continue
        out.append({"option": o, "kind": "undercut" if d < 0 else "overcut", "rival": best[0], "dp": best[1],
                    "p_alt": best[2], "p_plan": best[3], "cost": chosen.utility - o.utility,
                    "risk_gain": risk_utility(o) - risk_utility(chosen), "shift": d})
    out.sort(key=lambda a: (-(a["risk_gain"] > 0), -a["dp"], a["cost"]))
    return out


def describe(alt, chosen, lap, dirty=None):
    """Frase para el piloto con las cifras de la simulación (sin inventar nada)."""
    o, lap_alt, lap_plan = alt["option"], alt["option"].stops[0][0], chosen.stops[0][0]
    comp = S.NAME.get(o.stops[0][1], "?")
    when = "ahora" if lap_alt <= lap else f"en la vuelta {lap_alt}"
    if alt["kind"] == "undercut":
        head = f"Alternativa arriesgada: parar {when} a {comp} (undercut a {alt['rival']})"
    else:
        head = f"Alternativa arriesgada: alargar y parar {when} a {comp} (overcut a {alt['rival']})"
    txt = (f"{head}: la probabilidad de acabar por delante de {alt['rival']} pasa de {S.pct(alt['p_plan'])}% a "
           f"{S.pct(alt['p_alt'])}%, y cuesta {max(alt['cost'], 0):.1f} puntos esperados (P{o.e_pos:.1f} frente a "
           f"P{chosen.e_pos:.1f}).")
    if dirty:
        txt += f" Ojo: detrás de otro coche pierdes ~{dirty['loss_s']:.1f} s por vuelta ({dirty['n']} vueltas medidas)."
    return txt


def dirty_air(ahead_hist, lap_times, close=1.0, free=2.0, min_n=3):
    """Pérdida por ir pegado a otro coche, medida: mediana de vueltas con hueco < close menos las de hueco > free,
    comparando cada coche consigo mismo. ahead_hist: {coche: {vuelta: hueco_al_de_delante_s}};
    lap_times: {coche: {vuelta: ms}}. Devuelve {'loss_s', 'n'} o None si no hay muestras suficientes."""
    diffs, n = [], 0
    for car, gaps in ahead_hist.items():
        t = lap_times.get(car, {})
        near = [t[l] for l, g in gaps.items() if g is not None and 0 < g < close and l in t and t[l] > 0]
        far = [t[l] for l, g in gaps.items() if g is not None and g > free and l in t and t[l] > 0]
        if len(near) >= min_n and len(far) >= min_n:
            diffs.append((statistics.median(near) - statistics.median(far)) / 1000)
            n += len(near)
    if len(diffs) < 2:
        return None
    loss = statistics.median(diffs)
    return {"loss_s": loss, "n": n} if 0.05 <= loss <= 2.0 else None
