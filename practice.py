#!/usr/bin/env python3
"""Entrenamientos: estimación de clasificación y de ritmo de carrera, y qué se midió de cada compuesto.

Todo sale del historial de vueltas de los coches y de tus propias tandas. Es una ESTIMACIÓN: en entrenos no se conoce el
combustible ni el modo motor de los demás, así que el orden de verdad puede cambiar mucho.
"""
import statistics

import compare as C
import quali as Q
import strategy as S

MIN_RUN = 4                # vueltas limpias seguidas en el mismo compuesto para contar como ritmo de carrera



def _lap(sec):
    """Ritmo en segundos -> «1:22.34» (así se entiende como tiempo de vuelta, no como «82 segundos»)."""
    return Q.fmt(sec * 1000)[:-1]

def _slope(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return None if den == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def quali_estimate(tr):
    """Posición estimada por la mejor vuelta válida de cada coche: {rank, n, best_ms, gap_s, leader}."""
    best = {}
    for i, h in tr.hist.items():
        ms = [l["ms"] for l in h["laps"] if l["ms"] > 0 and l["valid"]]
        if ms:
            best[i] = min(ms)
    me = best.get(tr.player)
    if me is None or len(best) < 2:
        return None
    order = sorted(best, key=best.get)
    lead = order[0]
    return {"rank": order.index(tr.player) + 1, "n": len(order), "best_ms": me, "gap_s": (me - best[lead]) / 1000.0,
            "leader": tr.name(lead), "leader_is_me": lead == tr.player}


def long_runs(tr, i):
    """[(compuesto, ritmo mediano s, vueltas limpias, tendencia s/vuelta)] de las tandas con >= MIN_RUN vueltas limpias."""
    rows = C.laps_table(tr, i)
    out = []
    for k in sorted({r["stint"] for r in rows}):
        clean = [r for r in rows if r["stint"] == k and r["clean"]]
        if len(clean) >= MIN_RUN:
            ms = [r["ms"] / 1000 for r in clean]
            out.append((clean[0]["comp"], statistics.median(ms), len(clean), _slope([r["age"] for r in clean], ms)))
    return out


def race_estimate(tr):
    """Posición estimada por el ritmo en tandas largas: {rank, n, pace, gap_s}. None si tú no tienes una tanda así."""
    pace = {}
    for i in tr.hist:
        runs = long_runs(tr, i)
        if runs:
            pace[i] = min(r[1] for r in runs)
    me = pace.get(tr.player)
    if me is None or len(pace) < 2:
        return None
    order = sorted(pace, key=pace.get)
    return {"rank": order.index(tr.player) + 1, "n": len(order), "pace": me, "gap_s": me - pace[order[0]]}


def compound_table(tr):
    """Tus tandas largas por compuesto: [{comp, pace, laps, trend, wear_rate}] (wear_rate de tandas ya cerradas)."""
    closed = {}
    for s in tr.stint_log:
        if s["age"] >= 2:
            closed.setdefault(s["comp"], []).append(s["wear"] / s["age"])
    out = []
    for comp, pace, n, trend in long_runs(tr, tr.player):
        wr = closed.get(comp)
        out.append({"comp": comp, "pace": pace, "laps": n, "trend": trend,
                    "wear_rate": statistics.mean(wr) if wr else None})
    return out


def summary(tr):
    """Frases del resumen de entrenamiento."""
    out = []
    q, r = quali_estimate(tr), race_estimate(tr)
    if q:
        out.append(f"Tu mejor vuelta es {q['best_ms'] // 60000}:{q['best_ms'] % 60000 / 1000:06.3f}: "
                   f"P{q['rank']} de {q['n']} con tiempo" + ("." if q["leader_is_me"] else f", a {q['gap_s']:.3f} s de {q['leader']}."))
    else:
        out.append("Aún no tengo suficientes vueltas válidas tuyas y de rivales para estimar la clasificación.")
    if r:
        out.append(f"En ritmo de carrera (tandas de {MIN_RUN}+ vueltas limpias) estimo P{r['rank']} de {r['n']}, "
                   f"a {r['gap_s']:.2f} s por vuelta del mejor.")
    else:
        out.append(f"Aún no tienes una tanda de {MIN_RUN} vueltas limpias seguidas para estimar el ritmo de carrera.")
    tab = compound_table(tr)
    if tab:
        parts = []
        for t in tab:
            s = f"{S.NAME.get(t['comp'], '?')} {_lap(t['pace'])} ({t['laps']} v."
            if t["wear_rate"]:
                s += f", {t['wear_rate']:.1f}% de desgaste por vuelta"
            parts.append(s + ")")
        out.append("Compuestos: " + "; ".join(parts) + ".")
        if len(tab) >= 2:
            fast = min(tab, key=lambda t: t["pace"])
            out.append(f"El más rápido por vuelta fue el {S.NAME.get(fast['comp'], '?')}; compara también el desgaste antes de elegir.")
    out.append("Es una estimación: en entrenos no conozco el combustible ni el modo motor de los demás.")
    return out


def rank_table(tr):
    """Orden por mejor vuelta válida (entrenos y clasificación): [{rank, car, best_ms, name}]."""
    best = {}
    for i, h in tr.hist.items():
        ms = [l["ms"] for l in h["laps"] if l["ms"] > 0 and l["valid"]]
        if ms:
            best[i] = min(ms)
    order = sorted(best, key=best.get)
    return [{"rank": k, "car": i, "best_ms": best[i], "name": tr.name(i)} for k, i in enumerate(order, 1)]


def laps_to_limit(tr, wear_rate):
    """Vueltas hasta el límite de desgaste del modelo con ese ritmo de desgaste (%/vuelta)."""
    return (tr.params.wear_limit / wear_rate) if wear_rate and wear_rate > 0 else None


def compound_advice(tr, comps=None):
    """Frases sobre lo MEDIDO por compuesto en entrenos (ritmo, vueltas limpias, desgaste, vueltas hasta el límite)."""
    tab = compound_table(tr)
    if comps:
        tab = [t for t in tab if t["comp"] in comps]
    if not tab:
        names = " ni ".join(S.NAME.get(c, "?") for c in comps) if comps else "ningún compuesto"
        return [f"Aún no has rodado con {names} en una tanda de {MIN_RUN}+ vueltas limpias: no tengo datos para recomendar."]
    out = []
    for t in sorted(tab, key=lambda t: t["pace"]):
        s = f"Con {S.NAME.get(t['comp'], '?')}: ritmo de {_lap(t['pace'])} en {t['laps']} vueltas limpias"
        if t["wear_rate"]:
            lim = laps_to_limit(tr, t["wear_rate"])
            s += f", desgasta {t['wear_rate']:.1f}% por vuelta (llega al límite en ~{lim:.0f} vueltas)"
        elif t["trend"] is not None:
            s += (f", {'pierdes' if t['trend'] > 0 else 'ganas'} {abs(t['trend']):.2f} s por vuelta"
                  " (incluye aligerarse por combustible)")
        out.append(s + ".")
    if len(tab) >= 2:
        fast = min(tab, key=lambda t: t["pace"])
        slow = max(tab, key=lambda t: t["pace"])
        out.append(f"El {S.NAME.get(fast['comp'], '?')} es {slow['pace'] - fast['pace']:.2f} s más rápido por vuelta que el "
                   f"{S.NAME.get(slow['comp'], '?')}; decide con el desgaste y las vueltas de la carrera.")
    return out
