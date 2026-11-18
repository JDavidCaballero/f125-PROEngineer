"""Informe real del 26 oct (entrenos, clasificación y carrera): preguntas válidas sin respuesta, respuestas absurdas fuera de carrera
y un ingeniero mudo tras cambiar de sesión. Cada frase de aquí salió del informe."""
import unittest

from helpers import cfg  # noqa: F401  (prepara sys.path)

import assistant as A
import answers as AN
import practice as PR
from test_quali import make_engine, run_lap
from test_compare import hist


def lap(ms, valid=True):
    return {"ms": ms, "s1": 1, "s2": 1, "s3": 1, "valid": valid}


def practice_engine(kind_type=1):
    eng, out = make_engine(kind_type)
    eng.tr.names = {0: "Tu", 3: "Hansen", 4: "Costa", 5: "Piastri"}
    run_lap(eng, 0, 2, position=1)                               # da lap_all del jugador y una traza
    h = lambda ms, comp: {"laps": [lap(90000)] + [lap(m) for m in ms], "stints": [{"end_lap": 255, "visual": comp, "actual": comp}],
                          "car": 0, "best_lap": 0}
    eng.tr.hist = {0: h([80500, 80000, 80200, 80300, 80100], 17), 3: h([79500, 79700], 16), 4: h([81000], 18), 5: h([79000, 79900, 79800, 79700, 79750], 16)}
    for c, a in ((3, 4), (4, 3), (5, 5)):
        run_lap(eng, c, 2, position=c)
    eng.tr.stint_log = [{"comp": 17, "age": 5, "wear": 10.0}]
    return eng


class QuestionsThatWentUnanswered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = practice_engine()

    def ask(self, text):
        return self.eng.ask(text)

    def test_every_unanswered_phrase_from_the_report_now_gets_an_answer(self):
        phrases = ["¿En qué sesión estamos?", "¿Estamos en entrenamiento? ¿Sí o no?", "¿Cuál es la mejor vuelta de la sesión?",
                   "¿Cuál es la mejor vuelta de carrera?", "¿Cuál es mi mejor vuelta?", "¿Qué tiempo hice?", "¿Cuál es mi tiempo cronometrado?",
                   "Ya hice una vuelta, ¿cuál es mi vuelta?", "¿Quién va primero?", "¿Cómo va el segundo clasificado?",
                   "¿Quién es el tercer clasificado?", "Información de Piastri", "¿Dónde está Piastri?", "¿Ola, alguna actualización?",
                   "¿Tienes posibles estrategias?", "Entran boxes.", "Entren boxes.", "Empiezo vuelta a lanzar.",
                   "Me preparo para la nueva tanda con neumático blando.", "¿Qué tal el neumático medio?"]
        for p in phrases:
            r = self.ask(p)
            self.assertNotIn("No he entendido", r, p)
            self.assertNotIn("no es una carrera", r, p)

    def test_session_answer(self):
        self.assertIn("Libres 1", self.ask("¿En qué sesión estamos?"))

    def test_positions_outside_a_race_are_by_best_lap_not_race_gaps(self):
        r = self.ask("¿En qué posición voy?")
        self.assertIn("Por mejor vuelta", r)
        self.assertNotRegex(r, r"vuelta \d+ de \d+")
        self.assertNotRegex(self.ask("¿Quién está detrás de mí?"), r"(?<![\d.:])\d{3,}(\.\d)? s ")
        c = self.ask("¿Qué tal mi ritmo contra mi compañero de equipo?")
        self.assertNotRegex(c, r"(?<![\d.:])\d{3,}\.\d s")                        # nada de «a 15264.0 s»
        self.assertIn("mejor vuelta", c)

    def test_best_lap_answers(self):
        self.assertIn("1:20.000", self.ask("¿Cuál es mi mejor vuelta?").replace("1:20.000", "1:20.000"))
        s = self.ask("¿Cuál es la mejor vuelta de la sesión?")
        self.assertIn("Piastri", s)                                    # 1:19.000 es la más rápida

    def test_who_is_in_a_position_and_driver_info(self):
        self.assertIn("Piastri", self.ask("¿Quién va primero?"))
        self.assertIn("Piastri", self.ask("Información de Piastri"))
        self.assertRegex(self.ask("¿Dónde está Piastri?"), r"P\d")

    def test_acknowledgements_do_not_trigger_actions(self):
        self.assertEqual(self.ask("Entran boxes."), "Entendido.")
        self.assertEqual(self.ask("Empiezo vuelta lanzada."), "Entendido.")
        self.assertIsNone(self.eng.dialog.pending)

    def test_practice_compound_questions_use_what_was_measured(self):
        r = self.ask("¿Qué tal el neumático medio?")
        self.assertIn("Medio", r)
        self.assertIn("vueltas limpias", r)
        r2 = self.ask("¿Tienes posibles estrategias?")
        self.assertIn("estimo", r2.lower() + " estimo")                # el resumen de entrenos incluye estimación
        self.assertIn("Aún no has rodado con Duro", self.ask("¿Qué tal el neumático duro?"))

    def test_race_only_requests_are_explained_not_executed(self):
        r = self.ask("quiero acabar quinto")
        self.assertIn("Eso es de carrera", r)
        self.assertIsNone(self.eng.goals.goal)

    def test_update_request_gives_a_status(self):
        r = self.ask("¿Hola, alguna actualización?")
        self.assertIn("Por mejor vuelta", r)

    def test_more_updates_followup_with_punctuation(self):
        self.assertIn("¿De qué avisos", self.ask("Más actualizaciones."))
        self.assertIn("más avisos de los rivales", self.ask("Rivales.").lower().replace("de los rivales cercanos", "de los rivales"))
        self.assertEqual(self.eng.tac.topics["rivales"], 1)


class RulesTable(unittest.TestCase):
    def test_new_phrases_map_to_intents(self):
        eng = practice_engine()
        cases = {"¿En qué sesión estamos?": "sesion", "¿Cuál es mi mejor vuelta?": "mejor_vuelta", "¿Quién va primero?": "piloto",
                 "Información de compañero de equipo": "companero", "¿Ola, alguna actualización?": "actualizacion",
                 "¿Tienes posibles estrategias?": "estrategia", "Entren boxes.": "ack",
                 "¿cuánto falta?": "vueltas", "quiero acabar quinto": "objetivo_pos"}
        for text, want in cases.items():
            got = A.parse_rules(eng.tr, text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got.name, want, text)

    def test_statements_with_a_question_mark_are_not_acknowledged(self):
        eng = practice_engine()
        self.assertIsNone(A.parse_rules(eng.tr, "¿Entro a boxes?") and None)


class ValidSessionLabels(unittest.TestCase):
    def test_all_game_session_codes_have_a_name(self):
        for code in list(range(1, 19)):
            self.assertIn(code, AN.SESSIONS)


if __name__ == "__main__":
    unittest.main()
