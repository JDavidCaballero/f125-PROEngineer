"""Tercera ronda tras el informe de Spa: voz con números en palabras, ERS en porcentaje y contra el límite permitido, parada fuera de plan,
pérdida de parada medida, carpeta de envío con los paquetes grabados."""
import gzip, json, re, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import cfg, demo
import assistant as A
import coach as K
import engineer as E
import quali as Q
import speech as SP
import speaker as SPK
import voice as V
from f1_parse import read_capture
from test_brain import Scripted, race_engine
from test_raceplan import TestPractice
from test_sessions import patched, run


class Speech(unittest.TestCase):
    def test_the_bug_from_the_report_1452_is_not_read_as_a_thousand(self):
        out = SP.to_speech("Ocon está a 1.452 s delante.")
        self.assertEqual(out, "Ocon está a 1 segundo y 452 milésimas delante.")
        self.assertNotRegex(out, r"\d\.\d")

    def test_gaps_in_tenths_hundredths_and_thousandths(self):
        self.assertEqual(SP.to_speech("a 0.5 s"), "a 5 décimas")
        self.assertEqual(SP.to_speech("a 0.19 s"), "a 19 centésimas")
        self.assertEqual(SP.to_speech("a 0.442 s"), "a 442 milésimas")
        self.assertEqual(SP.to_speech("a 2.0 s"), "a 2 segundos")
        self.assertEqual(SP.to_speech("a 1.4 s"), "a 1 segundo y 4 décimas")
        self.assertEqual(SP.to_speech("a 0.0 s"), "a cero segundos")
        self.assertEqual(SP.to_speech("recortas 0.1 s por vuelta"), "recortas 1 décima por vuelta")

    def test_lap_times(self):
        self.assertEqual(SP.to_speech("Vuelta 1:43.450."), "Vuelta 1 minuto, 43 segundos y 450 milésimas.")
        self.assertEqual(SP.to_speech("mejor 1:06.2"), "mejor 1 minuto, 6 segundos y 2 décimas")
        self.assertEqual(SP.to_speech("2:06.962"), "2 minutos, 6 segundos y 962 milésimas")
        self.assertEqual(SP.to_speech("1:05.007"), "1 minuto, 5 segundos y 7 milésimas")

    def test_percent_degrees_speed_and_positions(self):
        self.assertEqual(SP.to_speech("Batería 49%"), "Batería 49 por ciento")
        self.assertEqual(SP.to_speech("136°C"), "136 grados")
        self.assertEqual(SP.to_speech("200 km/h"), "200 kilómetros por hora")
        self.assertEqual(SP.to_speech("Esperado P1.2"), "Esperado posición 1 coma 2")
        self.assertEqual(SP.to_speech("(km 1.3)"), "(kilómetro 1 coma 3)")
        self.assertEqual(SP.to_speech("+0.98 s respecto"), "más 98 centésimas respecto")     # el signo ya no se pierde
        self.assertEqual(SP.to_speech("margen de -0.6 vueltas"), "margen de menos 0 coma 6 vueltas")

    def test_no_decimal_point_survives_in_real_phrases(self):
        for s in ("Vas P4 en la vuelta 5 de 15. Hansen va delante, a 0.5 s. Tu mejor vuelta es 1:15.811 (P1 de 22).",
                  "Neumático Medio, 8 vueltas de uso, desgaste máximo 49% en el delantero derecho. Al ritmo actual llegan al 70% en unas 4 vueltas.",
                  "Combustible 15.6 kg, margen de 0.6 vueltas.", "Parada de 22 s (medida). Alternativa: +13.6 s en total."):
            self.assertNotRegex(SP.to_speech(s), r"\d\.\d", s)

    def test_the_speaker_pronounces_the_converted_text_but_logs_the_original(self):
        from test_voice import make_speaker, FakePopen
        sp = make_speaker()
        sp.say("Ocon está a 1.452 s.", 2)
        sp.wait(2)
        self.assertIn("1 segundo y 452 milésimas", " ".join(map(str, FakePopen.log)))
        self.assertIn("Ocon está a 1.452 s.", sp.spoken)


def fake_rec(deployed_mj, store_end, ran_out_at=None, nb=200):
    ers = [deployed_mj * 1e6 * k / nb for k in range(nb + 1)]
    store = [store_end * 4e6] * (nb + 1)
    if ran_out_at is not None:
        for k in range(ran_out_at, nb + 1):
            store[k] = 0.02 * 4e6
        store[-1] = store_end * 4e6
    return SimpleNamespace(ers=ers, store=store, km=lambda b: b * 0.025, track_m=5000, thr=[1.0] * nb, speed=[250.0] * nb, t=list(range(nb + 1)),
                           brk=[0.0] * nb, time_ms=100000)


class ErsAgainstThePermittedLimit(unittest.TestCase):
    """«Te sobró batería, úsala más» no tiene sentido si el juego ya no deja desplegar más (contorno verde = 4 MJ por vuelta)."""

    def text(self, *a, **k):
        return " ".join(Q.ers_report(fake_rec(*a, **k)))

    def test_using_the_whole_limit_with_battery_left_is_not_a_mistake(self):
        t = self.text(4.0, 0.30)
        self.assertIn("Usaste el 100% del despliegue permitido", t)
        self.assertIn("acabaste con 30% de batería", t)
        self.assertIn("Aprovechaste todo el despliegue permitido", t)
        self.assertNotRegex(t, r"(?i)sobr|úsala más|úsalo más")

    def test_using_less_than_the_limit_with_battery_says_how_much_was_missing(self):
        t = self.text(2.0, 0.60)
        self.assertIn("Usaste el 50% del despliegue permitido", t)
        self.assertIn("Te faltó usar el 50% del despliegue permitido", t)

    def test_if_the_battery_ran_out_first_it_says_so_not_that_you_left_deployment(self):
        t = self.text(2.4, 0.05, ran_out_at=120)
        self.assertIn("La batería se agotó", t)
        self.assertIn("antes de usar todo el despliegue permitido", t)
        self.assertNotIn("Te faltó usar", t)

    def test_battery_empty_at_the_end_below_the_limit(self):
        t = self.text(3.0, 0.04)
        self.assertIn("la batería no daba para más", t)

    def test_everything_is_in_percent_never_kilojoules_or_megajoules(self):
        for args in ((4.0, 0.30), (2.0, 0.60), (2.4, 0.05, 120)):
            t = self.text(args[0], args[1], ran_out_at=args[2] if len(args) > 2 else None)
            self.assertNotRegex(t, r"\b(kJ|MJ)\b")
        rows = [{"seg": {"name": "la recta hacia la curva 3", "kind": "straight", "b0": 10, "b1": 15}, "dt_s": 0.3, "d_ers_kj": -80, "d_vmin": 0}]
        txt = " ".join(K.advice("Gasly", rows, -0.4, rec=SimpleNamespace(km=lambda b: b * 0.03)))
        self.assertIn("2% más de batería", txt)
        self.assertNotRegex(txt, r"kJ")

    def test_with_a_reference_it_compares_the_share_of_the_limit(self):
        t = " ".join(Q.ers_report(fake_rec(4.0, 0.3), fake_rec(3.0, 0.3), "tu mejor vuelta"))
        self.assertIn("100% del despliegue permitido frente al 75% de tu mejor vuelta", t)

    def test_the_limit_is_configurable_and_over_the_limit_counts_as_full_use(self):
        old = Q.DEPLOY_LIMIT_J
        try:
            Q.DEPLOY_LIMIT_J = 8e6
            self.assertAlmostEqual(Q.ers_summary(fake_rec(4.0, 0.3))["used"], 0.5)
        finally:
            Q.DEPLOY_LIMIT_J = old
        self.assertEqual(Q.ers_summary(fake_rec(9.6, 0.3))["used"], 1.0)         # captura real de Singapur: hasta 9.6 MJ contados


class PitLossAndOffPlan(unittest.TestCase):
    def test_pit_loss_measured_in_earlier_sessions_is_used_before_the_default(self):
        eng = race_engine(Scripted())
        tid = eng.tr.ses.get("track_id")
        eng.tr.hist = {}
        eng.memory._t(tid).update({"tables": {16: {"comp": 16, "pace": 90, "laps": 4, "wear_rate": 2.0, "trend": 0}}, "pit_loss": [19.0, 19.4, 19.8, 20.2],
                                   "t": eng.memory.clock()})
        v, src = eng.tr.pit_loss()
        self.assertAlmostEqual(v, 19.6, 1)
        self.assertIn("sesiones anteriores", src)

    def test_with_fewer_than_three_samples_the_default_stays(self):
        eng = race_engine(Scripted())
        tid = eng.tr.ses.get("track_id")
        eng.tr.hist = {}
        eng.memory._t(tid).update({"tables": {16: {}}, "pit_loss": [19.0, 19.4], "t": eng.memory.clock()})
        self.assertEqual(eng.tr.pit_loss()[1], "valor por defecto")

    def stop_engine(self, planned):
        eng = race_engine(Scripted())
        eng.preplan = {"stints": planned, "stops": len(planned) - 1}
        said = []
        eng.say = lambda text, *a, **k: said.append(text) or True
        return eng, said

    def test_a_stop_when_the_plan_was_no_stop_is_commented_once(self):
        eng, said = self.stop_engine([(16, 11)])
        eng._off_plan_stop({"lap": 5})
        eng._off_plan_stop({"lap": 6})
        self.assertEqual(len(said), 1)
        self.assertIn("el plan previo era acabar sin parar", said[0])

    def test_an_early_stop_is_commented_a_stop_on_time_is_not(self):
        eng, said = self.stop_engine([(17, 8), (16, 7)])
        eng._off_plan_stop({"lap": 5})
        self.assertIn("era parar al final de la vuelta 8", said[0])     # como lo dice el plan («para en la vuelta 8»)
        self.assertIn("Antes de lo previsto", said[0])
        eng2, said2 = self.stop_engine([(17, 8), (16, 7)])
        eng2._off_plan_stop({"lap": 9})
        self.assertEqual(said2, [])

    def test_no_comment_if_a_plan_was_accepted_with_the_pilot(self):
        eng, said = self.stop_engine([(16, 11)])
        eng.dialog.accepted = object()
        eng._off_plan_stop({"lap": 5})
        self.assertEqual(said, [])


class YesNo(unittest.TestCase):
    def test_a_yes_no_question_gets_the_short_answer_first(self):
        eng = TestPractice.engine()
        r = eng.ask("¿Tienes datos para estrategia? ¿Sí o no?")
        self.assertTrue(r.startswith(("Sí.", "No.")), r)

    def test_without_the_yes_no_the_answer_is_unchanged(self):
        eng = TestPractice.engine()
        self.assertFalse(eng.ask("¿Qué estrategia planeas para la carrera?").startswith(("Sí.", "No.")))


class PaceGap(unittest.TestCase):
    def test_slow_laps_do_not_count_for_the_pace_comparison(self):
        import compare as CMP
        rows_me = [{"clean": True, "ms": m} for m in (100000, 100200, 100100)]
        rows_rv = [{"clean": True, "ms": m} for m in (101000, 101200, 109000, 101100)]     # una vuelta de 109 s (bandera / incidente)
        old = CMP.laps_table
        try:
            CMP.laps_table = lambda tr, i: rows_me if i == 0 else rows_rv
            gap = K.pace_gap(SimpleNamespace(player=0), 5)
        finally:
            CMP.laps_table = old
        self.assertAlmostEqual(gap, 1.0, 1)


class SharingFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.folder = self.tmp / "runs" / "session-20261028-100000-lab"
        self.folder.mkdir(parents=True)
        (self.folder / "INFORME.txt").write_text("informe")
        path, _ = demo("sprint")
        pk = list(read_capture(path))[:200]
        import f1_udp as U, struct
        with open(self.folder / "udp.f1raw", "wb") as f:
            f.write(U.MAGIC)
            for t, d in pk:
                f.write(U.REC.pack(t, len(d)) + d)
        self.pk = pk

    def conf(self, **kw):
        return {"paths": {"export_dir": str(self.tmp / "out"), "open_folder_at_end": False, **kw}}

    def test_report_and_compressed_packets_land_in_one_folder_and_replay(self):
        out = V.export_for_sharing(self.folder, self.folder / "INFORME.txt", self.conf())
        self.assertEqual(out.name, "f1-informe-20261028-100000-lab")
        self.assertEqual((out / "INFORME.txt").read_text(), "informe")
        back = list(read_capture(out / "udp.f1raw.gz"))
        self.assertEqual([d for _, d in back], [d for _, d in self.pk])

    def test_no_recording_still_exports_the_report(self):
        (self.folder / "udp.f1raw").unlink()
        out = V.export_for_sharing(self.folder, self.folder / "INFORME.txt", self.conf())
        self.assertTrue((out / "INFORME.txt").exists())
        self.assertFalse((out / "udp.f1raw.gz").exists())

    def test_an_empty_export_dir_disables_it_and_failures_never_raise(self):
        self.assertIsNone(V.export_for_sharing(self.folder, self.folder / "INFORME.txt", self.conf(export_dir="")))
        self.assertIsNone(V.export_for_sharing(self.folder, self.folder / "NO_EXISTE.txt", self.conf()))

    def test_learning_from_the_recording_updates_the_profile(self):
        path, _ = demo("media")
        import shutil
        shutil.copy(path, self.folder / "udp.f1raw")
        prof = self.tmp / "profile.json"
        c = {"paths": {"profile": str(prof), "learn_at_end": True}, **{k: v for k, v in cfg().items() if k != "paths"}}
        V.learn_from_recording(self.folder, c)
        data = json.loads(prof.read_text())
        self.assertTrue(data["tracks"])
        V.learn_from_recording(self.folder, c)                                       # la misma carrera no se cuenta dos veces
        again = json.loads(prof.read_text())
        self.assertEqual(again["global"]["races"], data["global"]["races"])

    def test_the_report_names_the_recording(self):
        import blackbox as BB
        BB.pack(self.folder)
        self.assertIn("GRABACIÓN DE PAQUETES", (self.folder / "INFORME.txt").read_text())


if __name__ == "__main__":
    unittest.main()


class WhisperJargon(unittest.TestCase):
    """Errores de oído repetidos con la jerga (informe de Spa)."""

    def test_known_mishearings_are_fixed(self):
        import stt
        self.assertEqual(stt.fix_jargon("Quema 1."), "Q1.")
        self.assertEqual(stt.fix_jargon("Entran los bits."), "Entran los pits.")
        self.assertIn("quali", stt.fix_jargon("empezaré mi vuelta de QALE"))
        self.assertIn("telemetría", stt.fix_jargon("¿Tienes datos de teledimetría?"))
        self.assertIn("sesión", stt.fix_jargon("¿En qué sección estamos?"))

    def test_normal_phrases_are_untouched(self):
        import stt
        for s in ("¿Qué 3 neumáticos tengo?", "El vuelo de Ocon", "Dame la posición", "¿Cuántas vueltas faltan?"):
            self.assertEqual(stt.fix_jargon(s), s)

    def test_clean_applies_it(self):
        import stt
        text, why = stt.clean({"text": "Quema 1.", "avg_logprob": -0.3, "no_speech_prob": 0.01, "duration_s": 2.0})
        self.assertEqual((text, why), ("Q1.", None))


class EchoWarning(unittest.TestCase):
    def test_repeated_echo_warns_on_screen_and_a_real_phrase_resets_the_streak(self):
        from test_voice import session
        eng, sess, clock, log, sp = session()
        shown = []
        sess.echo = shown.append
        said = "Aún no tengo una vuelta completa tuya medida: necesito una vuelta cronometrada y válida."
        sess._on_tts("start", said, clock())
        sess.utterance(said, 4.0)
        sess.utterance(said, 4.0)
        self.assertTrue([s for s in shown if "auriculares" in s and "**" in s])
        sess.utterance("¿Cuánto combustible me queda?", 2.0)
        self.assertEqual(sess.echo_hits, 0)


class GuardsFromRun7(unittest.TestCase):
    """Fallos con el modelo real (28 oct): «Entran los bits» -> plan_carrera con 18 vueltas inventadas."""

    def plan(self, text, obj):
        return race_engine(Scripted()).assistant.brain.to_plan(text, obj)

    def test_plan_carrera_needs_race_words_and_the_lap_count_must_be_said(self):
        self.assertEqual(self.plan("entran los bits", {"tools": [{"t": "plan_carrera", "laps": 18}], "say": ""}).tools, [])
        p = self.plan("¿qué plan hay para la carrera de 12 vueltas?", {"tools": [{"t": "plan_carrera", "laps": 12}], "say": ""})
        self.assertEqual((p.tools, p.intents[0].topic), (["plan_carrera"], "12"))
        p = self.plan("¿qué plan hay para la carrera?", {"tools": [{"t": "plan_carrera", "laps": 18}], "say": ""})
        self.assertIsNone(p.intents[0].topic)                                  # 18 no está en la frase

    def test_como_adelantar_needs_overtaking_words(self):
        self.assertEqual(self.plan("¿cómo voy contra Alves?", {"tools": [{"t": "como_adelantar"}], "say": ""}).tools, [])
        self.assertEqual(self.plan("¿cómo adelanto a Alves?", {"tools": [{"t": "como_adelantar"}], "say": ""}).tools, ["como_adelantar"])

    def test_rule_overrides_for_tyre_questions_the_model_sent_elsewhere(self):
        eng = race_engine(Scripted(route=lambda c: {"tools": [{"t": "estrategia"}], "say": ""}), t_stop=200)
        self.assertEqual(eng.assistant.prepare("¿Qué neumático pongo en la próxima parada?").intents[0].name, "elegir_compuesto")
        eng2 = race_engine(Scripted(route=lambda c: {"tools": [{"t": "hueco"}], "say": ""}), t_stop=200)
        eng2.tr.names[5] = "Fischer"
        p = eng2.assistant.prepare("como van los neumaticos del de atras")
        self.assertIn(p.intents[0].name, ("rival_neumaticos", "neumaticos"))

    def test_only_the_live_session_exports_files(self):
        import re
        src = open(V.__file__, encoding="utf-8").read()
        self.assertRegex(src, r'if a\.cmd == "lab" and a\.live:[^\n]*\n\s+learn_from_recording')


class RealRun8Fixture(unittest.TestCase):
    """Batería difícil con Qwen real tras la ronda 8 (66 frases, 28 oct): evidencia guardada; no llama al servidor."""

    def test_measured_quality(self):
        d = json.loads((Path(__file__).parent / "data" / "llm_real_run8.json").read_text(encoding="utf-8"))["filas"]
        self.assertEqual(len(d), 66)
        self.assertGreaterEqual(sum(r["ok"] for r in d) / 66, 0.90)
        lat = sorted(r["ruta_s"] for r in d if r["ruta_s"])
        self.assertLess(lat[len(lat) // 2], 4.0)
        said = " ".join(r["respuesta"].lower() for r in d)
        for bad in ("iniciando", "no entiendo qué", "victoria"):
            self.assertNotIn(bad, said)
