#!/usr/bin/env python3
"""Resumen de carrera: solo hechos medidos (resultado, paradas, propuestas) y cuánto se acercó la predicción al final.

La calibración (predicción de a mitad de carrera frente al resultado) también se guarda en el registro: es la base para
que, con varias carreras reales en la misma pista, se pueda corregir el sesgo del modelo.
"""
import compare as C
import strategy as S


def _proposals(dialog):
    seen, out = set(), []
    for p in list(dialog.history) + [x.summary() for x in (dialog.accepted, dialog.pending) if x]:
        if p["id"] not in seen:
            seen.add(p["id"])
            out.append(p)
    return out


def calibration(pred_log, total_laps, final_pos):
    """[(vuelta, posición esperada, error)] a ~1/3 y ~2/3 de carrera y en la última evaluación."""
    if not pred_log or not total_laps or not final_pos:
        return []
    picks = []
    for frac in (1 / 3, 2 / 3, 1.0):
        lap, e = min(pred_log, key=lambda x: abs(x[0] - frac * total_laps))
        if (lap, e) not in [(a, b) for a, b, _ in picks]:
            picks.append((lap, e, final_pos - e))
    return picks


def build(eng, final_pos=None):
    """Lista de frases del resumen. Si la carrera no ha terminado, lo dice y resume lo que lleva."""
    tr = eng.tr
    me = tr.lap_all[tr.player] if tr.player is not None and tr.player < len(tr.lap_all) else None
    fin = next((c for c in (tr.final or {}).get("cars", []) if c["idx"] == tr.player), None)
    out = []
    if fin:
        txt = f"Resumen: saliste P{fin['grid']} y terminaste P{fin['position']} con {fin['points']} puntos."
        if fin["pit_stops"] == 0:
            txt += " Sin paradas."
        final_pos = fin["position"]
    elif me:
        txt = f"La carrera no ha terminado: vas P{me['position']}."
        final_pos = None
    else:
        return ["No tengo datos de la carrera todavía."]
    story = C.stint_story(tr, tr.player)
    if story and (not fin or fin["pit_stops"]):
        txt += f" Neumáticos: {story}."
    out.append(txt)
    props = _proposals(eng.dialog)
    if props:
        acc = [p for p in props if p["state"] in ("aceptada", "ejecutada")]
        rej = [p for p in props if p["state"] == "rechazada"]
        rest = len(props) - len(acc) - len(rej)
        txt = f"Te hice {len(props)} propuestas: aceptaste {len(acc)}, rechazaste {len(rej)}" + \
              (f" y {rest} caducaron sin respuesta." if rest else ".")
        if acc and acc[-1].get("e_pos") is not None and final_pos:
            a = acc[-1]
            txt += f" La de la vuelta {a['lap']} esperaba P{a['e_pos']:.1f}; terminaste P{final_pos}."
        out.append(txt)
    cal = calibration(eng.pred_log, (tr.ses or {}).get("total_laps"), final_pos)
    if cal:
        lap, e, err = cal[1] if len(cal) > 1 else cal[0]
        out.append(f"Mi predicción en la vuelta {lap} era P{e:.1f}; el resultado fue P{final_pos} ({err:+.1f} posiciones de diferencia).")
    return out
