#!/usr/bin/env python3
"""Análisis de vuelta (clasificación y entrenos): dónde pierdes tiempo y cómo usas el ERS, contra tu mejor vuelta o contra
la vuelta de un rival. Todo sale de las trazas medidas (traces.py); las cifras de ERS son las del juego.

Honestidad sobre lo que NO se sabe:
  * el juego no marca las curvas: se detectan por la velocidad y se numeran en orden de aparición («la curva 5»); puede no
    coincidir con la numeración oficial del circuito (curvas encadenadas se funden en una);
  * el despliegue del MGU-K está limitado a 4 MJ por vuelta (el contorno verde del indicador del juego; fuentes: foros de EA y guías de ERS de
    F1 25) y tus informes lo confirman: nunca pasa de 4.0 MJ. Se compara con ESE límite, no con la batería: puede sobrar batería y aun así
    no poder desplegar más. Todo se dice en porcentaje (4 MJ = 100 %): el piloto no piensa en kilojulios.
"""
from traces import NB

CAP_J = 4_000_000          # capacidad de la batería (máximo medido en la captura real)
DEPLOY_LIMIT_J = 4_000_000  # despliegue máximo del MGU-K por vuelta (config [ers] deploy_limit_mj)
FULL_USE = 0.95            # a partir de este uso del límite se considera «aprovechado todo»
MIN_LOSS_S = 0.04          # pérdida mínima por tramo para mencionarla
ZONE = 3                   # tramos a cada lado del vértice que cuentan como «la curva»


def fmt(ms):
    return f"{int(ms // 60000)}:{ms % 60000 / 1000:06.3f}"


def pct(kj):
    """kilojulios -> % de 4 MJ (batería / límite de despliegue de una vuelta)."""
    return kj / (CAP_J / 1000.0) * 100.0


def _smooth(v, k=2):
    n = len(v)
    return [sum(v[max(0, i - k):i + k + 1]) / len(v[max(0, i - k):i + k + 1]) for i in range(n)]


def find_corners(rec, drop_in=25.0, drop_out=18.0, win=8, merge=4):
    """Tramos donde está el vértice de cada curva: mínimo local de velocidad con bajada previa y subida posterior claras."""
    sp = _smooth(rec.speed)
    n = len(sp)
    cands = []
    for i in range(1, n - 1):
        if sp[i] <= sp[i - 1] and sp[i] < sp[i + 1]:
            before = max(sp[max(0, i - win):i])
            after = max(sp[i + 1:i + 1 + win])
            if before - sp[i] >= drop_in and after - sp[i] >= drop_out:
                cands.append(i)
    out = []
    for i in cands:
        if out and i - out[-1] < merge:
            if sp[i] < sp[out[-1]]:
                out[-1] = i
        else:
            out.append(i)
    return out


def segments(rec):
    """Divide la vuelta en curvas y rectas que cubren los NB tramos: [{name, kind, b0, b1}]."""
    corners = find_corners(rec)
    segs, pos = [], 0
    for k, c in enumerate(corners, 1):
        b0, b1 = max(pos, c - ZONE), min(NB, c + ZONE + 1)
        if b0 - pos >= 2:
            segs.append({"name": f"la recta hacia la curva {k}", "kind": "straight", "b0": pos, "b1": b0})
            pos = b0
        segs.append({"name": f"la curva {k}", "kind": "corner", "b0": pos, "b1": b1})
        pos = b1
    if NB - pos >= 2:
        segs.append({"name": "la recta final hacia meta" if corners else "la vuelta", "kind": "straight", "b0": pos, "b1": NB})
    elif segs:
        segs[-1]["b1"] = NB
    return segs


def seg_stats(rec, s):
    b0, b1 = s["b0"], s["b1"]
    return {"ms": rec.t[b1] - rec.t[b0], "ers_kj": (rec.ers[b1] - rec.ers[b0]) / 1000.0,
            "vmin": min(rec.speed[b0:b1]), "full": sum(1 for x in rec.thr[b0:b1] if x >= 0.95)}


def compare(a, ref):
    """Por tramo de a frente a ref: [{seg, dt_s (positivo = pierdes), d_ers_kj, d_vmin}]. Ambas deben ser de la misma pista."""
    if abs(a.track_m - ref.track_m) > 5:
        return []
    out = []
    for s in segments(ref):                       # los tramos se definen con la vuelta de referencia
        sa, sr = seg_stats(a, s), seg_stats(ref, s)
        out.append({"seg": s, "dt_s": (sa["ms"] - sr["ms"]) / 1000.0, "d_ers_kj": sa["ers_kj"] - sr["ers_kj"],
                    "d_vmin": sa["vmin"] - sr["vmin"], "a": sa, "ref": sr})
    return out


def ers_summary(rec):
    """Despliegue de la vuelta (y su % del límite permitido), batería final y dónde se agotó (km) si ocurrió antes del 90% de la vuelta."""
    store = rec.store
    out_at = next((b for b in range(NB) if store[b] <= 0.03 * CAP_J and b < 0.9 * NB), None)
    return {"deployed_mj": rec.ers[-1] / 1e6, "used": min(1.0, rec.ers[-1] / DEPLOY_LIMIT_J), "store_end": store[-1] / CAP_J,
            "store_min": min(store) / CAP_J, "ran_out_km": rec.km(out_at) if out_at is not None else None}


def underused_straights(rec, n=2):
    """Rectas donde vas a fondo mucho rato y desplegaste poco ERS por tramo a fondo, respecto a tu propia mediana."""
    rows = []
    for s in segments(rec):
        if s["kind"] != "straight":
            continue
        st = seg_stats(rec, s)
        if st["full"] >= 4:
            rows.append((st["ers_kj"] / st["full"], st["full"], s, st))
    if not rows:
        return []
    if len(rows) == 1:
        return [(rows[0][2], rows[0][3], 0.0)]
    dens = sorted(r[0] for r in rows)
    med = dens[len(dens) // 2]
    low = [r for r in rows if r[0] < 0.6 * med]
    if low:
        low.sort(key=lambda r: -(med - r[0]) * r[1])
    else:                                          # despliegas parecido en todas: las rectas más largas a fondo
        low = sorted(rows, key=lambda r: -r[1])
    return [(r[2], r[3], med * r[1] - r[3]["ers_kj"]) for r in low[:n]]


def ers_report(rec, ref=None, who=None):
    """Frases sobre el ERS de la vuelta, en porcentaje. Se compara con el despliegue PERMITIDO de la vuelta (4 MJ), no con la batería."""
    e = ers_summary(rec)
    head = f"Usaste el {e['used']:.0%} del despliegue permitido de la vuelta y acabaste con {e['store_end']:.0%} de batería."
    if ref is not None:
        r = ers_summary(ref)
        head = (f"Usaste el {e['used']:.0%} del despliegue permitido frente al {r['used']:.0%} de {who or 'la referencia'}; "
                f"acabaste con {e['store_end']:.0%} de batería.")
    parts = [head]
    if e["ran_out_km"] is not None:
        parts.append(f"La batería se agotó en el km {e['ran_out_km']:.1f}" +
                     (" antes de usar todo el despliegue permitido" if e["used"] < FULL_USE else "") +
                     ": ahorra en las curvas lentas anteriores y úsala en las rectas largas.")
    elif e["used"] >= FULL_USE:
        parts.append("Aprovechaste todo el despliegue permitido de la vuelta.")
    elif e["store_end"] >= 0.10:
        tips = underused_straights(rec)
        parts.append(f"Te faltó usar el {1 - e['used']:.0%} del despliegue permitido: úsalo más" + (" en " + " y en ".join(
            f"{s['name']} (km {rec.km(s['b0']):.1f}, solo el {pct(st['ers_kj']):.0f}% de la batería)" for s, st, _ in tips) if tips else "") + ".")
    else:
        parts.append("No llegaste al despliegue permitido porque la batería no daba para más: el uso es razonable.")
    return parts


def losses(rec, ref, n=3, min_loss=MIN_LOSS_S):
    return sorted([r for r in compare(rec, ref) if r["dt_s"] >= min_loss], key=lambda r: -r["dt_s"])[:n]


def lap_report(rec, ref, who="tu mejor vuelta", n=3):
    """Frases: diferencia total y los tramos donde más pierdes, con el ERS y la velocidad de vértice como contexto."""
    delta = (rec.time_ms - ref.time_ms) / 1000.0
    parts = [f"Vuelta {fmt(rec.time_ms)}: {abs(delta):.3f} s {'más lenta' if delta > 0 else 'más rápida'} que {who}."]
    rows = losses(rec, ref, n)
    if not rows:
        parts.append("No veo tramos donde pierdas más de 0.04 s: la diferencia está repartida.")
    for r in rows:
        s, txt = r["seg"], f"En {r['seg']['name']} (km {rec.km(r['seg']['b0']):.1f}) pierdes {r['dt_s']:.2f} s"
        ctx = []
        if s["kind"] == "straight" and r["d_ers_kj"] <= -40:
            ctx.append(f"gastaste un {pct(abs(r['d_ers_kj'])):.0f}% menos de batería que {who}")
        if s["kind"] == "corner" and r["d_vmin"] <= -4:
            ctx.append(f"con {abs(r['d_vmin']):.0f} km/h menos en el vértice")
        parts.append(txt + (": " + ", ".join(ctx) if ctx else "") + ".")
    return parts


def rival_report(rec, ref, who, n=2):
    """Qué hace el rival mejor que tú: tramos donde te gana y cuánto ERS despliega ahí. Solo con datos de su vuelta."""
    rows = losses(rec, ref, n)
    gap = (rec.time_ms - ref.time_ms) / 1000.0
    parts = [f"{who} te saca {gap:.3f} s en su mejor vuelta." if gap > 0 else f"Tu mejor vuelta es {abs(gap):.3f} s más rápida que la de {who}."]
    for r in rows:
        s = r["seg"]
        t = f"En {s['name']} te gana {r['dt_s']:.2f} s"
        if s["kind"] == "straight" and r["d_ers_kj"] <= -40:
            t += f" y gasta un {pct(abs(r['d_ers_kj'])):.0f}% más de batería"
        elif s["kind"] == "corner" and r["d_vmin"] <= -4:
            t += f" con {abs(r['d_vmin']):.0f} km/h más en el vértice"
        parts.append(t + ".")
    return parts
