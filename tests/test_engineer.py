import re, unittest

from helpers import demo, run_engineer, cfg, REAL
import engineer as E
from f1_parse import read_capture


class Sprint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path, cls.info = demo("sprint")
        cls.lines, cls.eng = run_engineer(cls.path)
        cls.text = "\n".join(cls.lines)

    def test_intro_adapts_to_race_length(self):
        self.assertIn("Carrera de 5 vueltas", self.lines[0])
        self.assertIn("sprint", self.lines[0])

    def test_no_strategy_chatter_in_a_5_lap_race(self):
        self.assertNotIn("Propuesta", self.text)
        self.assertNotIn("Entra a boxes", self.text)

    def test_gap_and_drs_tactics_present(self):
        self.assertIn("defiende", self.text)
        self.assertIn("DRS disponible", self.text)

    def test_ends_with_result(self):
        self.assertIn(f"terminas P{self.info['final_pos']}", self.text)


class Corta(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path, cls.info = demo("corta")
        cls.lines, cls.eng = run_engineer(cls.path)
        cls.text = "\n".join(cls.lines)

    def test_safety_car_cycle(self):
        i = self.text.index("Safety Car desplegado")
        j = self.text.index("Pista despejada")
        self.assertLess(i, j)

    def test_pits_under_safety_car(self):
        sc = [l for l in self.lines if "Propuesta: Con el Safety Car" in l]
        self.assertTrue(sc, "debe proponer algo cuando sale el SC")
        self.assertIn("esta vuelta", sc[0])
        self.assertIn("¿Lo hacemos?", sc[0])

    def test_after_sc_ends_label_is_green_flag(self):
        self.assertTrue(any("Con bandera verde" in l for l in self.lines))

    def test_fuel_alert_only_when_critical_and_near_the_end(self):
        """Antes avisaba cada vuelta desde el principio y se llevaba por delante las propuestas. Ahora solo si faltan
        más de fuel_alert_deficit vueltas de combustible en las últimas fuel_alert_laps (15 vueltas en esta carrera)."""
        fuel = [l for l in self.lines if "Combustible justo" in l]
        laps = [int(re.search(r"vuelta (\d+)", l).group(1)) for l in fuel]
        self.assertTrue(all(n >= 15 - 4 for n in laps), laps)
        self.assertEqual(len(laps), len(set(laps)))

    def test_more_fuel_updates_restores_early_alerts(self):
        eng = type(self.eng)(cfg(), "normal", None, out=lambda *_: None, sync=True)
        eng.tac.topics["combustible"] = 1
        out = []
        eng.out = out.append
        for t, d in read_capture(self.path):
            eng.on_packet(t, d)
        early = [l for l in out if "Combustible justo" in l and int(re.search(r"vuelta (\d+)", l).group(1)) < 11]
        self.assertTrue(early)

    def test_plan_is_stable_before_the_safety_car(self):
        early = [l for l in self.lines[:self.lines.index(next(l for l in self.lines if "Safety Car desplegado" in l))]
                 if "Propuesta" in l]
        self.assertLessEqual(len(early), 1, "no debe repetir la misma propuesta antes del SC")

    def test_deterministic(self):
        again, _ = run_engineer(self.path)
        self.assertEqual(again, self.lines)


class Media(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path, cls.info = demo("media")
        cls.lines, cls.eng = run_engineer(cls.path)
        cls.text = "\n".join(cls.lines)

    def test_wing_damage_and_repair_advice(self):
        self.assertIn("Daño en el alerón delantero izquierdo: 35%", self.text)
        dmg = [l for l in self.lines if "Propuesta: Con el daño" in l or "Con el daño: el plan no cambia" in l
               or "Con el daño: de momento seguimos" in l]          # daño leve: primero se mide cuánto pierdes
        self.assertTrue(dmg, "el daño debe decir algo: propuesta urgente o que el plan pendiente no cambia")
        if "Propuesta" in dmg[0]:
            self.assertIn("¿Lo hacemos?", dmg[0])

    def test_pit_stop_reported_once_and_no_stale_advice(self):
        self.assertEqual(self.text.count("Parada completada"), 1)
        i = max(k for k, l in enumerate(self.lines) if "Neumáticos nuevos puestos" in l)
        after = [l for l in self.lines[i:i + 3] if "Propuesta" in l and "esta vuelta" in l]
        self.assertFalse(after, "tras parar no debe seguir diciendo 'entra a boxes esta vuelta'")

    def test_weather_alerts_do_not_spam(self):
        self.assertIn("Lluvia", self.text)
        self.assertLessEqual(sum("Lluvia" in l and "prepárate" in l for l in self.lines), 3)
        self.assertGreaterEqual(self.text.count("Pista mojada"), 1)
        self.assertLessEqual(self.text.count("Pista mojada"), 8)

    def test_tier_media_message(self):
        self.assertIn("media", self.lines[0])


class Modes(unittest.TestCase):
    def test_all_modes_run_and_invalid_rejected(self):
        path, _ = demo("sprint")
        for m in ("conservador", "normal", "arriesgado"):
            lines, _ = run_engineer(path, m)
            self.assertTrue(lines)
        with self.assertRaises(ValueError):
            E.Engineer(cfg(), "temerario")


@unittest.skipUnless(REAL.exists(), "captura real no disponible")
class RealRace(unittest.TestCase):
    """Tu carrera real de 5 vueltas en Singapur (coche 21, P16 -> P22)."""

    @classmethod
    def setUpClass(cls):
        cls.lines, cls.eng = run_engineer(REAL)
        cls.text = "\n".join(cls.lines)

    def test_runs_and_reports(self):
        self.assertIn("Carrera de 5 vueltas en Singapur", self.text)
        self.assertIn("Fin de carrera: terminas P22", self.text)

    def test_no_absurd_numbers(self):
        for l in self.lines:
            m = re.search(r"Recortas ([\d.]+) s por vuelta", l)
            if m:
                self.assertLess(float(m.group(1)), 3.5)
            m = re.search(r"sector \d: \+([\d.]+) s", l)
            if m:
                self.assertLess(float(m.group(1)), 2.6)

    def test_no_duplicated_penalty_messages(self):
        self.assertNotIn("Aviso de la dirección de carrera", self.text)
        self.assertNotIn("Sanción acumulada", self.text)

    def test_pit_stops_measured_from_real_timers(self):
        self.assertIn("Parada completada: 7.7 s parado", self.text)     # stop-and-go cumplido en boxes
        self.assertIn("Parada completada: 2.5 s parado", self.text)     # parada normal con cambio a Medio


if __name__ == "__main__":
    unittest.main()


class FormatWarning(unittest.TestCase):
    def test_warns_when_udp_format_is_not_2025(self):
        import struct
        from f1_parse import HEADER, read_capture
        path, _ = demo("sprint")
        out = []
        eng = E.Engineer(cfg(), out=out.append)
        for t, d in read_capture(path):
            b = bytearray(d)
            b[:2] = struct.pack("<H", 2026)          # simula un juego con otro formato de paquete
            eng.on_packet(t, bytes(b))
            break
        self.assertTrue(any("formato UDP 2026" in l for l in out))


class WithModel(unittest.TestCase):
    """El LLM (simulado) elige entre las opciones elegibles y su frase llega al piloto; el log lo registra."""

    def test_model_choice_reaches_the_driver_and_log(self):
        import json, tempfile
        from pathlib import Path
        import llm as L
        from f1_parse import read_capture
        picks = []

        def transport(payload):
            user = json.loads(payload["messages"][1]["content"])
            ids = [o["id"] for o in user["opciones"]]
            picks.append(ids)
            return {"choices": [{"message": {"content": json.dumps({"opcion": ids[-1], "frase": f"Plan {ids[-1]} confirmado."})},
                                 "finish_reason": "stop"}]}

        path, _ = demo("corta")
        lines, log = [], Path(tempfile.mkdtemp()) / "run.jsonl"
        eng = E.Engineer(cfg(), "arriesgado", L.LLMClient(transport=transport), out=lines.append, sync=True, log_path=log)
        for t, d in read_capture(path):
            eng.on_packet(t, d)
        eng.close()
        self.assertTrue(picks, "en modo arriesgado hay varias opciones elegibles: debe consultar al modelo")
        self.assertTrue(any("confirmado" in l for l in lines))
        recs = [json.loads(x) for x in log.read_text().splitlines()]
        strat = [r for r in recs if r["type"] == "strategy"]
        self.assertTrue(strat)
        self.assertTrue(any(r["decision"]["source"] == "llm" for r in strat))
        self.assertTrue(all("options" in r["report"] and r["mode"] == "arriesgado" for r in strat))
        self.assertTrue(any(r["type"] == "say" for r in recs))

    def test_async_live_path(self):
        import time
        from f1_parse import read_capture
        path, _ = demo("corta")
        lines = []
        eng = E.Engineer(cfg(), out=lines.append, sync=False)       # como en --live: la estrategia corre en un hilo
        for t, d in read_capture(path):
            eng.on_packet(t, d)
            while eng._busy.locked():                                # en el juego una vuelta dura más que el cálculo: sin esta
                time.sleep(0.005)                                    # espera, en máquinas lentas (CI) se saltaban evaluaciones
        for _ in range(100):
            if not eng._busy.locked():
                break
            time.sleep(0.05)
        self.assertTrue(any("Propuesta" in l for l in lines))
        self.assertGreater(eng.evals, 3)
