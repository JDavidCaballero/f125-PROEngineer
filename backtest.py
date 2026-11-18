#!/usr/bin/env python3
"""Backtest: reproduce capturas y compara lo que el motor predecía con lo que pasó de verdad.

  python3 backtest.py captures/*.f1raw [--mode normal] [--json salida.json]

En cada evaluación se toma la opción que coincide con lo que el piloto hizo después (seguir fuera, o parar en esa
vuelta con ese compuesto) y se compara su predicción con el resultado final real:
  - error de la posición esperada y cobertura del rango P10-P90 (debería rondar el 80%)
  - Brier de "¿acabo por delante de X?" (con su skill frente a decir siempre 50%) y tabla de fiabilidad
  - estabilidad del consejo (cuántas veces cambia la primera parada recomendada)
  - ruido: mensajes por vuelta y motivos de silencio
No dice qué habría pasado con otro plan (eso no se puede saber); mide si el modelo acierta lo que sí ocurrió.
"""
import argparse, json, statistics, sys
from collections import Counter
from pathlib import Path

import config as C
import dialog as D
import strategy as S
from engineer import Engineer
from f1_parse import read_capture, header


def actual_stops(hist, after_lap):
    """[(vuelta de entrada, compuesto nuevo)] del jugador desde after_lap, según el historial final."""
    out = []
    comps = [s["visual"] for s in hist["stints"]]
    for k, st in enumerate(s for s in hist["stints"] if s["end_lap"] != 255):
        lap_in = st["end_lap"] + 1
        if lap_in >= after_lap and k + 1 < len(comps):
            out.append((lap_in, comps[k + 1]))
    return out


def match_option(rep, sit, stops):
    """Opción de rep que corresponde a lo que el piloto hizo realmente (o None)."""
    if not stops:
        return next((o for o in rep.options if o.kind == "STAY" and not o.sc_react), None) or \
            next((o for o in rep.options if o.kind == "STAY"), None)
    first = stops[0]
    cands = [o for o in rep.options if o.stops and D.same_stop(o.stops[0], first, 1)]
    return max(cands, key=lambda o: o.utility) if cands else None


def process_session(evals, tr_final, hist_player, names, player):
    me_final = next((c for c in tr_final["cars"] if c["idx"] == player), None)
    if not me_final or not hist_player:
        return None
    final_pos = {c["idx"]: c["position"] for c in tr_final["cars"]}
    by_name = {nm: i for i, nm in names.items()}
    rows, pair = [], []
    for ev in evals:
        lap, sit, rep = ev["lap"], ev["sit"], ev["rep"]
        opt = match_option(rep, sit, actual_stops(hist_player, lap))
        if opt is None:
            continue
        a, b = S.p_range(opt.cdf)
        actual = me_final["position"]
        rows.append({"lap": lap, "laps_left": sit.laps_left, "e_pos": opt.e_pos, "actual": actual,
                     "err": abs(opt.e_pos - actual), "signed": opt.e_pos - actual, "in_range": a <= actual <= b,
                     "lo": a, "hi": b})
        for nm, p in opt.p_beat.items():
            j = by_name.get(nm)
            if j is not None and j in final_pos:
                pair.append((p, 1 if final_pos[j] > actual else 0, lap))
    return {"rows": rows, "pairs": pair, "actual": me_final["position"], "evals": len(evals)}


def stability(evals):
    firsts = [ev["best_first"] for ev in evals]
    changes = sum(1 for a, b in zip(firsts, firsts[1:]) if not (a is None and b is None) and
                  not (a and b and D.same_stop(a, b, 1)))
    return changes, max(len(firsts) - 1, 1)


def run(path, mode, cfg):
    """Devuelve la lista de resultados de cada carrera (sesión) de la captura."""
    eng = Engineer(cfg, mode, None, out=lambda x: None, sync=True, profile=C.load_profile(cfg))
    evals, results = [], []
    eng.on_eval.append(lambda reason, lap, sit, me, rivals, rep, chosen: evals.append(
        {"lap": lap, "sit": sit, "rep": rep, "best_first": rep.best.stops[0] if rep.best.stops else None,
         "reason": reason}))

    def close_session():
        tr = eng.tr
        if tr.is_race() and tr.final and evals:
            r = process_session(list(evals), tr.final, tr.hist.get(tr.player), dict(tr.names), tr.player)
            if r:
                ch, n = stability(evals)
                laps = max(tr.ses.get("total_laps", 1), 1)
                r.update({"changes": ch, "transitions": n, "msgs": len(eng.log), "laps": laps,
                          "track": tr.ses.get("track_id"), "total_laps": laps, "mode": mode})
                results.append(r)
        evals.clear()
        eng.log.clear()

    for t, d in read_capture(path):
        if eng.tr.uid is not None and header(d)["session_uid"] != eng.tr.uid:
            close_session()
            eng.dialog = D.Dialog(cfg, mode)
        eng.on_packet(t, d)
    close_session()
    return results


def summarize(results):
    rows = [r for res in results for r in res["rows"]]
    pairs = [p for res in results for p in res["pairs"]]
    out = {"races": len(results), "evaluations": sum(r["evals"] for r in results), "matched": len(rows)}
    if rows:
        out["mean_abs_pos_error"] = round(statistics.mean(r["err"] for r in rows), 2)
        out["coverage_p10_p90"] = round(sum(r["in_range"] for r in rows) / len(rows), 2)
        out["bias_pos"] = round(statistics.mean(r["signed"] for r in rows), 2)      # + = predice peor de lo que pasó
        for name, lo, hi in (("10+", 10, 99), ("5-9", 5, 10), ("<5", 0, 5)):
            sub = [r["err"] for r in rows if lo <= r["laps_left"] < hi]
            if sub:
                out[f"err_laps_left_{name}"] = round(statistics.mean(sub), 2)
    if pairs:
        brier = statistics.mean((p - y) ** 2 for p, y, _ in pairs)
        out["pairwise_n"] = len(pairs)
        out["pairwise_brier"] = round(brier, 3)
        out["pairwise_skill_vs_50pct"] = round(1 - brier / 0.25, 3)
        rel = []
        for lo in (0.0, 0.2, 0.4, 0.6, 0.8):
            sub = [(p, y) for p, y, _ in pairs if lo <= p < lo + 0.2 + (1e-9 if lo == 0.8 else 0)]
            if sub:
                rel.append({"bin": f"{lo:.1f}-{lo + 0.2:.1f}", "n": len(sub), "predicted": round(statistics.mean(p for p, _ in sub), 2),
                            "observed": round(statistics.mean(y for _, y in sub), 2)})
        out["reliability"] = rel
    ch = sum(r["changes"] for r in results)
    tn = sum(r["transitions"] for r in results)
    out["advice_changes_per_100_evals"] = round(100 * ch / max(tn, 1), 1)
    out["msgs_per_lap"] = round(sum(r["msgs"] for r in results) / max(sum(r["laps"] for r in results), 1), 1)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--mode", default="normal", choices=list(S.MODES))
    ap.add_argument("--json")
    ap.add_argument("--config")
    a = ap.parse_args()
    cfg = C.load(a.config)
    results = []
    for f in a.files:
        r = run(f, a.mode, cfg)
        print(f"{Path(f).name}: {len(r)} carrera(s) con clasificación final")
        results += r
    s = summarize(results)
    print(json.dumps(s, indent=1, ensure_ascii=False))
    if a.json:
        Path(a.json).write_text(json.dumps(s, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
