#!/usr/bin/env python3
"""Lo que se MIDE de los demás coches: ritmo por compuesto, desgaste por vuelta y qué pasó tras sus paradas.

Todo sale de los paquetes que el juego ya manda para los 22 coches (historial de vueltas con sus tramos de neumático,
estado de coche y daños). Nada se inventa: cada cifra lleva cuántas vueltas la respaldan y, si son pocas, se dice.
Limitaciones que conviene recordar:
  * el combustible de los rivales no se conoce: un coche que se aligera mejora ~décimas a lo largo del stint;
  * la edad del neumático se cuenta desde el inicio del tramo (se asume juego nuevo);
  * en carreras cortas hay pocas vueltas limpias por compuesto.
"""
import statistics

import strategy as S

CLEAN_BAND = 1.07          # una vuelta cuenta si es <= 107% de la mejor limpia de ese coche (descarta SC, tráfico, errores)
MIN_LAPS = 2               # menos vueltas que esto no se considera evidencia
SIMILAR_S = 0.8            # "ritmo parecido": mejor vuelta limpia a menos de esto de la tuya


def stint_bounds(h):
    """[{start, end|None, comp}] a partir de los tramos del historial (end_lap 255 = tramo actual)."""
    out, prev = [], 0
    for s in (h or {}).get("stints", []):
        end = None if s["end_lap"] == 255 else s["end_lap"]
        out.append({"start": prev + 1, "end": end, "comp": s["visual"]})
        if end is None:
            break
        prev = end
    return out


def laps_table(tr, i):
    """Una fila por vuelta del coche i: lap, ms, comp, age (vueltas desde el inicio del tramo), stint, clean."""
    h = tr.hist.get(i)
    st = stint_bounds(h)
    if not h or not st:
        return []
    rows = []
    for j, l in enumerate(h["laps"]):
        n = j + 1
        k = next((x for x, s in enumerate(st) if s["end"] is None or n <= s["end"]), len(st) - 1)
        s = st[k]
        prev_end = st[k - 1]["end"] if k else None
        near_stop = (s["end"] == n) or (prev_end is not None and n <= prev_end + 1)     # vuelta de entrada y de salida
        rows.append({"lap": n, "ms": l["ms"], "comp": s["comp"], "age": n - s["start"] + 1, "stint": k,
                     "clean": l["ms"] > 0 and n >= 2 and l["valid"] and not near_stop})
    best = min((r["ms"] for r in rows if r["clean"]), default=None)
    for r in rows:
        r["clean"] = bool(r["clean"] and best and r["ms"] <= best * CLEAN_BAND)
    return rows


def best_clean(rows):
    return min((r["ms"] for r in rows if r["clean"]), default=None)


def pace_on(rows, comp=None, n=3, stint=None):
    """(mediana s de las últimas n vueltas limpias, cuántas), filtrando por compuesto y/o tramo."""
    sel = [r["ms"] for r in rows if r["clean"] and (comp is None or r["comp"] == comp) and (stint is None or r["stint"] == stint)]
    sel = sel[-n:]
    return (statistics.median(sel) / 1000, len(sel)) if sel else (None, 0)


def active(tr, i):
    la = tr.lap_all[i] if i < len(tr.lap_all) else None
    return bool(la and la["result_status"] == 2 and la["position"] > 0)


def wear_rate(tr, i, rows=None):
    """%/vuelta de desgaste de la peor rueda en el tramo actual (None con menos de 2 vueltas de uso)."""
    dm = tr.dmg_all[i] if i < len(tr.dmg_all) else None
    sa = tr.sta_all[i] if i < len(tr.sta_all) else None
    if not dm or not sa or sa["tyre_age_laps"] < 2:
        return None
    return max(dm["tyre_wear_pct"]) / sa["tyre_age_laps"]


def similar_rivals(tr, tol=SIMILAR_S):
    """Rivales activos cuya mejor vuelta limpia está a <= tol s de la tuya, del más parecido al menos."""
    mine = best_clean(laps_table(tr, tr.player))
    if not mine:
        return []
    out = []
    for i in range(len(tr.lap_all)):
        if i == tr.player or not active(tr, i):
            continue
        b = best_clean(laps_table(tr, i))
        if b and abs(b - mine) / 1000 <= tol:
            out.append((abs(b - mine), i))
    return [i for _, i in sorted(out)]


def compound_evidence(tr, comp, tol=SIMILAR_S, limit=3):
    """Rivales de ritmo parecido con vueltas limpias en `comp`: [{car, name, laps, pace, delta, now, wear_rate}].
    delta = su ritmo con ese compuesto menos tu ritmo reciente (negativo = va más rápido que tú)."""
    mine = tr.pace(tr.player)
    out = []
    for i in similar_rivals(tr, tol):
        rows = laps_table(tr, i)
        p, n = pace_on(rows, comp)                                  # ritmo: mediana de las últimas 3 limpias
        n = sum(1 for r in rows if r["clean"] and r["comp"] == comp)    # respaldo: todas las vueltas limpias con ese compuesto
        if p is None or n < MIN_LAPS:
            continue
        sa = tr.sta_all[i]
        now = bool(sa and sa["visual_compound"] == comp)
        out.append({"car": i, "name": tr.name(i), "laps": n, "pace": p, "delta": (p - mine) if mine else None,
                    "now": now, "wear_rate": wear_rate(tr, i) if now else None})
    return out[:limit]


def evidence_text(tr, comp, tol=SIMILAR_S):
    name = S.NAME.get(comp, "?")
    ev = compound_evidence(tr, comp, tol)
    if not ev:
        return f"Nadie de ritmo parecido ha rodado aún con {name}: me baso solo en el modelo."
    parts = []
    for e in ev:
        t = e["name"]
        if e["delta"] is not None:
            sa = tr.sta_all[tr.player]
            mine = S.NAME.get(sa["visual_compound"]) if sa and sa["visual_compound"] != comp else None
            ref = f"tu ritmo actual con {mine}" if mine else "tú"
            t += f" rueda {abs(e['delta']):.1f} s {'más rápido' if e['delta'] < 0 else 'más lento'} que {ref}"
        t += f" ({e['laps']} vueltas limpias"
        if e["wear_rate"]:
            t += f", desgasta {e['wear_rate']:.1f}% por vuelta"
        parts.append(t + ")")
    return f"Con {name}, " + "; ".join(parts) + "."


def stop_reactions(tr, idxs=None, tol=SIMILAR_S):
    """Qué pasó tras la parada de rivales de ritmo parecido: [{car, name, lap, from, to, before, after, delta}].
    delta = ritmo del tramo nuevo - ritmo del anterior (negativo = mejoró). Incluye el efecto de ir más ligero."""
    out = []
    for i in (idxs if idxs is not None else similar_rivals(tr, tol)):
        rows = laps_table(tr, i)
        stints = sorted({r["stint"] for r in rows})
        if len(stints) < 2:
            continue
        k = stints[-1]
        before, nb = pace_on(rows, stint=k - 1)
        after, na = pace_on(rows, stint=k)
        if before is None or after is None or nb < MIN_LAPS or na < MIN_LAPS:
            continue
        prev = next(r for r in rows if r["stint"] == k - 1)
        cur = next(r for r in rows if r["stint"] == k)
        out.append({"car": i, "name": tr.name(i), "lap": cur["lap"] - 1, "from": prev["comp"], "to": cur["comp"],
                    "before": before, "after": after, "delta": after - before, "laps": na})
    return out


def teammate(tr):
    """Índice del compañero de equipo (mismo equipo en los participantes), o None si no hay uno único."""
    team = getattr(tr, "team", {})
    t = team.get(tr.player)
    mates = [i for i, x in team.items() if x == t and i != tr.player and i < len(tr.lap_all) and tr.lap_all[i]]
    return mates[0] if t is not None and len(mates) == 1 else None


def stint_story(tr, i):
    """'Medio (7 v.) → Duro (3 v. hasta ahora)' con los compuestos usados por el coche i."""
    st = stint_bounds(tr.hist.get(i))
    if not st:
        return None
    laps = len(tr.hist[i]["laps"])
    parts = []
    for s in st:
        end = s["end"] if s["end"] is not None else max(laps, s["start"])
        parts.append(f"{S.NAME.get(s['comp'], '?')} ({max(end - s['start'] + 1, 0)} v.)")
    return " → ".join(parts)
