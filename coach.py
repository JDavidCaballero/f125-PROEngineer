#!/usr/bin/env python3
"""Entrenador de carrera: cómo adelantar (o recuperar una posición) a un rival concreto, con las trazas medidas de los dos.

Todo sale de datos del juego: tu mejor vuelta reciente y la del rival por tramos de pista (tiempo, despliegue de ERS, velocidad
de vértice), su batería y su desgaste, y los huecos. No se inventa nada: si falta la vuelta de uno de los dos, se dice y se da solo
lo que sí se mide. Las «curvas» se numeran por orden de aparición, como en quali.py (pueden no coincidir con la numeración oficial).
"""
import statistics

import compare as CMP
import quali as Q
import strategy as S

MIN_GAIN_S = 0.03          # ventaja mínima por tramo para recomendar atacar ahí
MAX_SEG_S = 1.2            # diferencia por tramo por encima de esto = vuelta con incidente o bandera: se ignora
ERS_LOW = 0.20             # batería por debajo de esto: el rival no puede defenderse con ERS
WEAR_EDGE = 8.0            # puntos de desgaste de diferencia para mencionarlo


def straight_attacks(rows, n=2):
    """Rectas donde vas más rápido que el rival (dt_s < 0): ahí se ataca. Ordenadas por ventaja."""
    cand = [r for r in rows if r["seg"]["kind"] == "straight" and r["dt_s"] <= -MIN_GAIN_S]
    return sorted(cand, key=lambda r: r["dt_s"])[:n]


def corner_trouble(rows, n=1):
    """Tramos donde el rival te saca más tiempo (dt_s > 0): ahí se te va y no conviene gastar ERS en seguirlo."""
    cand = [r for r in rows if r["dt_s"] >= MIN_GAIN_S]
    return sorted(cand, key=lambda r: -r["dt_s"])[:n]


def pace_gap(tr, car):
    """Segundos por vuelta del rival MENOS los tuyos en tus 3 últimas vueltas limpias (positivo = él es más lento) o None."""
    a, b = CMP.laps_table(tr, tr.player), CMP.laps_table(tr, car)
    def pace_laps(rows):
        ms = [r["ms"] for r in rows if r["clean"]]
        best = min(ms, default=None)
        return [m for m in ms if m <= best * 1.02][-3:] if best else []         # sin vueltas lentas (bandera, tráfico, incidente)
    ra, rb = pace_laps(a), pace_laps(b)
    if len(ra) < 2 or len(rb) < 2:
        return None
    return (statistics.median(rb) - statistics.median(ra)) / 1000.0


def advice(name, rows, gap_s=None, pace_s=None, ers_store=None, comp=None, age=None, wear_edge=None, mode="attack", rec=None):
    """Frases de consejo (lista). rows: quali.compare(tu_vuelta, vuelta_del_rival) o []."""
    out = []
    head = []
    if gap_s is not None:
        head.append(f"{name} está a {abs(gap_s):.1f} s {'delante' if gap_s < 0 else 'detrás'}")
    if pace_s is not None and abs(pace_s) >= 0.05:
        head.append(f"su ritmo es {abs(pace_s):.1f} s por vuelta {'más lento' if pace_s > 0 else 'más rápido'} que el tuyo")
    if comp:
        head.append(f"lleva {comp}" + (f" con {age} vueltas" if age is not None else ""))
    if head:
        out.append(", ".join(head)[0].upper() + ", ".join(head)[1:] + ".")
    if wear_edge is not None and abs(wear_edge) >= WEAR_EDGE:
        out.append(f"Sus neumáticos están {abs(wear_edge):.0f} puntos {'más gastados' if wear_edge > 0 else 'menos gastados'} que los tuyos.")
    if ers_store is not None and ers_store <= ERS_LOW:
        out.append(f"Le queda un {ers_store:.0%} de batería: no podrá defenderse con ERS.")
    if rows:
        att = straight_attacks(rows)
        if att:
            parts = []
            for r in att:
                s = f"{r['seg']['name']} (km {rec.km(r['seg']['b0']):.1f}) le ganas {abs(r['dt_s']):.2f} s" if rec else f"{r['seg']['name']} le ganas {abs(r['dt_s']):.2f} s"
                if r["d_ers_kj"] >= 40:
                    s += f" y gastas un {Q.pct(r['d_ers_kj']):.0f}% más de batería"
                parts.append(s)
            out.append(("Para pasarlo: ataca en " if mode != "recover" else "Para recuperar la posición: ataca en ") + "; en ".join(parts)
                       + ". Guarda la batería para esas rectas.")
        else:
            out.append("No veo ninguna recta donde seas más rápido que él: acércate en las curvas y usa el DRS.")
        tr_ = corner_trouble(rows)
        if tr_:
            r = tr_[0]
            extra = ""
            if r["seg"]["kind"] == "corner" and r["d_vmin"] <= -4:
                extra = f" con {abs(r['d_vmin']):.0f} km/h más en el vértice"
            elif r["seg"]["kind"] == "straight" and r["d_ers_kj"] <= -40:
                extra = f" y gasta un {Q.pct(abs(r['d_ers_kj'])):.0f}% más de batería"
            out.append(f"Cuidado en {r['seg']['name']}: ahí te saca {r['dt_s']:.2f} s{extra}; no gastes ERS siguiéndolo.")
    else:
        out.append("Aún no tengo una vuelta limpia de los dos para comparar curvas y rectas.")
    return out


def how_to_pass(eng, car, mode="attack"):
    """Texto de consejo para pasar a `car` (idx). None si el coche no es válido."""
    tr = eng.tr
    if car is None or car == tr.player or not tr.lap_all[car]:
        return None
    if tr.lap_all[car]["pit_status"] != 0:
        return f"{tr.name(car)} está en boxes."
    me_rec = tr.traces.last_timed(tr.player) or tr.traces.best.get(tr.player)
    rv_rec = tr.traces.last_timed(car) or tr.traces.best.get(car)
    if me_rec and rv_rec and abs(me_rec.time_ms - rv_rec.time_ms) > 0.03 * me_rec.time_ms:
        me_rec = rv_rec = None                       # una de las dos vueltas es de entrada/salida de boxes o con bandera: no son comparables
    rows = [r for r in (Q.compare(me_rec, rv_rec) if me_rec and rv_rec else []) if abs(r["dt_s"]) <= MAX_SEG_S]   # un tramo de >1.2 s es una vuelta con incidente, no un dato
    gaps, order = tr.gaps()
    gap_s = None
    ahead, behind = tr.neighbours()
    if ahead and ahead[0] == car:
        gap_s = -abs(ahead[1])
    elif behind and behind[0] == car:
        gap_s = abs(behind[1])
    elif car in gaps and tr.player in gaps:
        gap_s = gaps[car] - gaps[tr.player]
    sa, dm, dme = tr.sta_all[car], tr.dmg_all[car], tr.dmg_all[tr.player]
    ers = (sa["ers_store_j"] / Q.CAP_J) if sa else None
    wear_edge = (max(dm["tyre_wear_pct"]) - max(dme["tyre_wear_pct"])) if dm and dme else None
    comp = S.NAME.get(sa["visual_compound"]) if sa else None
    age = sa["tyre_age_laps"] if sa else None
    lines = advice(tr.name(car), rows, gap_s, pace_gap(tr, car), ers, comp, age, wear_edge, mode, me_rec)
    return " ".join(lines)


def car_ahead(tr):
    """Coche que va justo delante del jugador, o None."""
    g, order = tr.gaps()
    if tr.player not in order:
        return None
    k = order.index(tr.player)
    return order[k - 1] if k > 0 else None
