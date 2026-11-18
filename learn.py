#!/usr/bin/env python3
"""Aprende de tus capturas: calibra las constantes del motor de estrategia con datos reales.

  python3 learn.py captures/*.f1raw            # actualiza data/profile.json
  python3 learn.py captures/red.f1raw --show   # solo muestra lo aprendido

De cada carrera extrae, por pista:
  - coste de una parada (vuelta de entrada + de salida - 2 vueltas limpias) de TODOS los coches
  - ritmo de desgaste (% de la peor rueda por vuelta) de cada compuesto, de todos los coches
y a nivel global la frecuencia de Safety Car/VSC por vuelta. El motor usa esos valores en lugar de sus
estimaciones. Esto es calibrar parámetros con tus datos, no entrenar el LLM (ver README).
Cada carrera (session_uid) se cuenta una sola vez aunque repitas el comando.
"""
import argparse, statistics, sys
from pathlib import Path

import config as C
import strategy as S
from f1_parse import read_capture, header, TRACKS
from state import RaceTracker, measure_pit_loss


class Learner:
    def __init__(self):
        self.cur = {}                  # coche -> {"comp", "age", "wear"}
        self.rates = []                # (compuesto, %/vuelta)

    def _close(self, i):
        c = self.cur.pop(i, None)
        if c and c["comp"] in S.DRY and c["age"] >= 4 and c["wear"] > 3:
            self.rates.append((c["comp"], c["wear"] / c["age"]))

    def observe(self, tr):
        for i in range(22):
            sa, dm = tr.sta_all[i], tr.dmg_all[i]
            if not sa or not dm:
                continue
            comp, age = sa["visual_compound"], sa["tyre_age_laps"]
            c = self.cur.get(i)
            if c and (age < c["age"] or comp != c["comp"]):
                self._close(i)
                c = None
            if c is None:
                c = self.cur[i] = {"comp": comp, "age": age, "wear": 0.0}
            c["age"], c["wear"] = age, max(dm["tyre_wear_pct"])

    def finish(self):
        for i in list(self.cur):
            self._close(i)
        return self.rates


def _mean_update(d, key, new_vals):
    """Media móvil ponderada por número de muestras."""
    if not new_vals:
        return
    n0, m0 = d.get(key + "_n", 0), d.get(key, 0.0)
    d[key] = round((m0 * n0 + sum(new_vals)) / (n0 + len(new_vals)), 3)
    d[key + "_n"] = n0 + len(new_vals)


def learn_file(path, profile, cfg, force=False):
    tr = RaceTracker(cfg, profile)
    lr, done = Learner(), []
    learned = profile.setdefault("learned_uids", [])
    stats = {"races": 0, "skipped": 0}

    def close_session():
        if not tr.is_race():
            return
        uid = str(tr.uid)
        if uid in learned and not force:
            stats["skipped"] += 1
            return
        tid = str(tr.ses.get("track_id"))
        tk = profile["tracks"].setdefault(tid, {"name": TRACKS.get(tr.ses.get("track_id"), "")})
        _mean_update(tk, "pit_loss_s", measure_pit_loss(tr.hist))
        fz = tr.fz.export()
        if fz and fz["laps"] >= tk.get("fuel_zones", {}).get("laps", 0):
            tk["fuel_zones"] = fz
        by = {}
        for comp, r in lr.finish():
            by.setdefault(comp, []).append(r)
        for comp, vals in by.items():
            _mean_update(tk.setdefault("wear", {}).setdefault(str(comp), {}), "rate", vals)
        g = profile.setdefault("global", {})
        an = g.setdefault("ai_next", {})                         # transiciones de compuesto de la IA (excluye al jugador)
        for car, h in tr.hist.items():
            if car == tr.player:
                continue
            seq = [s["visual"] for s in h["stints"]]
            for a, b in zip(seq, seq[1:]):
                if a in S.DRY and b in S.DRY:
                    d = an.setdefault(str(a), {})
                    d[str(b)] = d.get(str(b), 0) + 1
        g["races"] = g.get("races", 0) + 1
        g["laps"] = g.get("laps", 0) + tr.ses.get("total_laps", 0)
        g["sc_periods"] = g.get("sc_periods", 0) + tr.ses.get("num_sc", 0) + tr.ses.get("num_vsc", 0)
        tk["races"] = tk.get("races", 0) + 1
        learned.append(uid)
        stats["races"] += 1

    for t, d in read_capture(path):
        h = header(d)
        if tr.uid is not None and h["session_uid"] != tr.uid:
            close_session()
            lr = Learner()
        tr.on_packet(t, d)
        if h["id"] == 10 and tr.is_race():
            lr.observe(tr)
    close_session()
    return stats


def show(profile):
    g = profile.get("global", {})
    print(f"Carreras aprendidas: {g.get('races', 0)} | vueltas: {g.get('laps', 0)} | periodos SC/VSC: {g.get('sc_periods', 0)}")
    for a, nxt in sorted(g.get("ai_next", {}).items()):
        tot = sum(nxt.values())
        print(f"  la IA tras {S.NAME.get(int(a), a)}: " + ", ".join(f"{S.NAME.get(int(b), b)} {100 * n // tot}%" for b, n in sorted(nxt.items())) + f" (n={tot})")
    for tid, tk in sorted(profile.get("tracks", {}).items()):
        pl = f"{tk['pit_loss_s']:.1f} s (n={tk['pit_loss_s_n']})" if "pit_loss_s" in tk else "sin datos"
        print(f"- pista {tid} {tk.get('name', '')}: coste de parada {pl}")
        fz = tk.get("fuel_zones")
        if fz:
            print(f"    ahorro de combustible ({fz['laps']} vueltas): " + (", ".join(
                f"tramo {z['bin'] + 1} ({z['kg_per_s']:.1f} kg/s)" for z in fz["measured"][:3]) or "solo candidatas sin medir"))
        for comp, w in sorted(tk.get("wear", {}).items()):
            print(f"    {S.NAME.get(int(comp), comp):9s} {w['rate']:.2f} %/vuelta (n={w['rate_n']})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--force", action="store_true", help="contar de nuevo carreras ya aprendidas")
    ap.add_argument("--dry-run", action="store_true", help="no guarda el perfil")
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    cfg = C.load(a.config)
    profile = C.load_profile(cfg)
    profile.setdefault("tracks", {})
    for f in a.files:
        s = learn_file(f, profile, cfg, a.force)
        print(f"{Path(f).name}: carreras nuevas {s['races']}, ya aprendidas {s['skipped']}")
    if a.files and not a.dry_run:
        print("Perfil guardado en", C.save_profile(profile, cfg))
    show(profile)


if __name__ == "__main__":
    main()
