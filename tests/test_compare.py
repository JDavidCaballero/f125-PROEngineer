"""Ritmo de los demás por compuesto, compañero de equipo y elección de compuesto. Más las redes de seguridad que
comprueban que lo nuevo no rompe lo que ya funcionaba (cada intención tiene respuesta, nada lanza excepciones)."""
import re, time, unittest
from types import SimpleNamespace as NS

from helpers import demo, cfg
import assistant as A
import compare as C
import engineer as E
import strategy as S
import tactics as T
from f1_parse import read_capture
from test_assistant import run_until


def hist(laps_ms, stints, valid=True):
    return {"laps": [{"ms": ms, "s1": 1, "s2": 1, "s3": 1, "valid": valid} for ms in laps_ms],
            "stints": [{"end_lap": e, "visual": c, "actual": c} for e, c in stints], "car": 0, "best_lap": 0}


def fake_tr(hists, player=0, team=None, wear=None, pace=None, ages=None):
    n = max(hists) + 1
    lap = lambda i: {"result_status": 2, "position": i + 1, "pit_status": 0, "pit_stops": 0} if i in hists else None
    sta = lambda i: {"visual_compound": hists[i]["stints"][-1][1] if False else hists[i]["stints"][-1]["visual"],
                     "tyre_age_laps": (ages or {}).get(i, 5)} if i in hists else None
    dmg = lambda i: {"tyre_wear_pct": (wear or {}).get(i, (10, 10, 10, 10))} if i in hists else None
    tr = NS(hist=hists, player=player, lap_all=[lap(i) for i in range(n)], sta_all=[sta(i) for i in range(n)],
            dmg_all=[dmg(i) for i in range(n)], team=team or {}, names={i: f"Piloto{i}" for i in range(n)})
    tr.name = lambda i: tr.names[i]
    tr.pace = lambda i: (pace or {}).get(i, 80.0)
    return tr


class StintTable(unittest.TestCase):
    def test_bounds_and_laps(self):
        h = hist([90000, 80000, 80100, 81000, 79000, 79100, 79200], [(4, 17), (255, 18)])
        self.assertEqual(C.stint_bounds(h), [{"start": 1, "end": 4, "comp": 17}, {"start": 5, "end": None, "comp": 18}])
        rows = C.laps_table(NS(hist={0: h}), 0)
        self.assertEqual([r["comp"] for r in rows], [17] * 4 + [18] * 3)
        self.assertEqual([r["age"] for r in rows], [1, 2, 3, 4, 1, 2, 3])
        # vuelta 1, vuelta de entrada (4) y de salida (5) no son limpias
        self.assertEqual([r["clean"] for r in rows], [False, True, True, False, False, True, True])

    def test_slow_laps_are_not_clean(self):
        rows = C.laps_table(NS(hist={0: hist([90000, 80000, 80100, 99999], [(255, 17)])}), 0)
        self.assertEqual([r["clean"] for r in rows], [False, True, True, False])      # >107% (SC, error)

    def test_no_history_is_empty_not_a_crash(self):
        self.assertEqual(C.laps_table(NS(hist={}), 3), [])
        self.assertEqual(C.laps_table(NS(hist={3: {"laps": [], "stints": [], "best_lap": 0}}), 3), [])
        self.assertEqual(C.pace_on([], 17), (None, 0))

    def test_pace_on_filters_by_compound(self):
        h = hist([90000] + [80000] * 3 + [90000] + [79000] * 4, [(5, 17), (255, 18)])
        rows = C.laps_table(NS(hist={0: h}), 0)
        self.assertEqual(C.pace_on(rows, 17), (80.0, 3))
        self.assertEqual(C.pace_on(rows, 18)[0], 79.0)
        self.assertEqual(C.pace_on(rows, 16), (None, 0))


class RivalEvidence(unittest.TestCase):
    def setUp(self):
        mine = hist([90000] + [80000] * 8, [(255, 17)])
        similar = hist([90000] + [79700] * 3 + [90000, 90000] + [79500] * 4, [(5, 17), (255, 18)])    # parecido, ya paró a Duro
        slow = hist([90000] + [85000] * 8, [(255, 18)])                                                # 5 s más lento: no cuenta
        self.tr = fake_tr({0: mine, 1: similar, 2: slow}, wear={1: (12, 10, 9, 9)}, ages={1: 4})

    def test_similar_rivals_exclude_much_slower_cars(self):
        self.assertEqual(C.similar_rivals(self.tr), [1])

    def test_compound_evidence_uses_only_similar_cars_with_enough_laps(self):
        ev = C.compound_evidence(self.tr, 18)
        self.assertEqual([e["car"] for e in ev], [1])
        self.assertAlmostEqual(ev[0]["delta"], 79.5 - 80.0, places=3)
        self.assertTrue(ev[0]["now"])
        self.assertAlmostEqual(ev[0]["wear_rate"], 3.0, places=3)             # 12% en 4 vueltas
        self.assertEqual(C.compound_evidence(self.tr, 16), [])

    def test_evidence_text_states_laps_and_admits_no_data(self):
        self.assertIn("4 vueltas limpias", C.evidence_text(self.tr, 18))
        self.assertIn("Nadie de ritmo parecido", C.evidence_text(self.tr, 16))

    def test_stop_reactions(self):
        r = C.stop_reactions(self.tr)
        self.assertEqual((r[0]["car"], r[0]["from"], r[0]["to"]), (1, 17, 18))
        self.assertAlmostEqual(r[0]["delta"], 79.5 - 79.7, places=3)

    def test_teammate_needs_a_unique_match(self):
        tr = fake_tr({0: hist([80000] * 3, [(255, 17)]), 1: hist([80000] * 3, [(255, 17)]), 2: hist([80000] * 3, [(255, 17)])},
                     team={0: 5, 1: 5, 2: 6})
        self.assertEqual(C.teammate(tr), 1)
        tr.team = {0: 5, 1: 5, 2: 5}
        self.assertIsNone(C.teammate(tr))                        # dos posibles: no se adivina
        tr.team = {}
        self.assertIsNone(C.teammate(tr))


class AssistantIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng, _, _ = run_until("corta", 330)        # vuelta 5 de 15, antes del Safety Car
        cls.tr = cls.eng.tr

    def parse(self, text):
        return A.parse_rules(self.tr, text)

    def test_compound_choice_phrases(self):
        cases = {
            "¿Es mejor el medio o el duro?": (17, 18), "¿Es mejor el medio o el blanco?": (17, 16),
            "¿El medio es bueno en la carrera o mejor intentamos por el duro?": (17, 18),
            "¿Cuál es el mejor neumático que no sea el neumático blando?": (17, 18),
            "Necesito que me digas cuál es el mejor neumático para parar.": (),
            "¿Qué neumático pongo en la próxima parada?": (),
        }
        for text, comps in cases.items():
            got = self.parse(text)
            self.assertIsNotNone(got, text)
            self.assertEqual((got.name, tuple(sorted(got.comps))), ("elegir_compuesto", tuple(sorted(comps))), text)

    def test_teammate_phrases(self):
        for text in ("¿Cómo va mi compañero?", "¿qué estrategia lleva mi compañero de equipo?", "¿Y mi compañero?"):
            self.assertEqual(self.parse(text).name, "companero", text)

    def test_compound_choice_answers_with_evidence_and_proposes(self):
        r = self.eng.ask("¿Es mejor el medio o el duro?")
        self.assertNotIn("No he entendido", r)
        self.assertTrue("Mejor opción" in r or "No tengo ninguna parada válida" in r, r)
        if "Mejor opción" in r:
            self.assertIn("¿Lo hacemos?", r)
            self.assertTrue(re.search(r"Con (Medio|Duro|Blando)", r), r)
            self.assertIsNotNone(self.eng.dialog.pending)

    def test_missing_compound_is_reported_not_silently_dropped(self):
        r = self.eng.ask("¿Es mejor el medio o el duro?")
        if "Mejor opción" in r:
            for name in ("Medio", "Duro"):
                self.assertTrue(name in r.split("Con ")[0] or f"Sin opción válida con {name}" in r, (name, r))

    def test_evidence_compares_against_my_current_compound(self):
        tr = fake_tr({0: hist([90000] + [80000] * 8, [(255, 16)]), 1: hist([90000] + [80500] * 8, [(255, 17)])}, ages={1: 5})
        self.assertIn("tu ritmo actual con Blando", C.evidence_text(tr, 17))

    def test_numbers_in_compound_answer_come_from_the_options(self):
        r = self.eng.ask("¿Cuál es el mejor neumático?")
        res, _ = self.eng.assistant._eval()
        if "Mejor opción" in r and res:
            first = re.search(r"P(\d+\.\d)", r).group(1)
            self.assertIn(first, {f"{o.e_pos:.1f}" for o in res[0].options})

    def test_teammate_answer(self):
        r = self.eng.ask("¿Cómo va mi compañero?")
        self.assertIn("Tu compañero", r)
        self.assertRegex(r, r"va P\d+")


class RivalAlerts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path, _ = demo("media")

    def run_it(self, topics=None):
        out = []
        eng = E.Engineer(cfg(), "normal", None, out=out.append, sync=True)
        eng.tac.topics.update(topics or {})
        for t, d in read_capture(self.path):
            eng.on_packet(t, d)
        return out

    def test_rival_alerts_are_rare_and_can_be_silenced(self):
        out = self.run_it()
        pits = [l for l in out if re.search(r"entra a boxes\.", l) and "Entra a boxes" not in l]
        wear = [l for l in out if "debería parar pronto" in l]
        self.assertLessEqual(len(pits) + len(wear), 12, "demasiados avisos de rivales en 30 vueltas")
        quiet = self.run_it({"rivales": -1})
        self.assertFalse([l for l in quiet if "debería parar pronto" in l or re.search(r"entra a boxes\.", l)])


# ---------------------------------------------------------------------------------------------- redes de seguridad
BATTERY = [
    "sí", "no", "explica", "repite", "silencio", "reanuda", "¿qué hay delante?", "¿qué hay detrás?", "¿cuánto me saca el de delante?",
    "¿en qué posición voy?", "¿cómo van las gomas?", "¿cuánto combustible tengo?", "¿dónde puedo ahorrar combustible?",
    "¿cuánta batería tengo?", "¿tengo daños?", "¿va a llover?", "¿cuántas vueltas faltan?", "¿cómo voy de ritmo?",
    "¿por qué estoy perdiendo tiempo?", "no sé qué hacer", "quiero parar ya", "¿puedo hacer undercut?",
    "no me siento cómodo con las gomas", "no me siento cómodo", "quiero acabar quinto", "quiero luchar por el podio",
    "¿podemos ganar la carrera?", "¿podemos acabar quinto?", "propón un objetivo", "¿vamos bien?", "olvida el objetivo",
    "¿es mejor el medio o el duro?", "¿cuál es el mejor neumático?", "quiero un neumático duro",
    "¿qué tal van los otros coches con neumático duro?", "¿cómo va mi compañero?", "¿cómo voy en el campeonato?",
    "menos avisos de combustible", "más avisos del ERS", "menos actualizaciones", "avisos normales",
    "¿dejo pasar a {P}?", "¿{P} me ha pasado?", "{P} me saca en recta", "¿qué neumáticos tiene {P}?", "¿cómo voy respecto a {P}?",
    "necesito pasar a {P}", "parar antes", "¿hago overcut?", "prefiero la alternativa", "resumen de la carrera",
    "¿cómo va la carrera?", "¿cómo fue mi vuelta?", "¿dónde pierdo tiempo?", "¿cómo uso el ERS?", "compárame con el primero",
    "compárame con mi compañero", "compárame con {P}", "¿qué estimas para la carrera?", "¿cómo va la práctica?",
    "menos avisos de vueltas", "menos avisos de rivales", "menos avisos de alternativas", "", "blablabla", "la casa azul", "x " * 30,
]
SCENARIOS = ("sprint", "corta", "media", "sc_final", "vsc", "pinchazo", "roja", "datos_perdidos")


class NothingBreaks(unittest.TestCase):
    def test_every_intent_has_a_handler(self):
        eng, _, _ = run_until("corta", 100)
        names = set(A.INTENTS) | {"compuesto", "compuesto_rivales", "rival_neumaticos", "rivales_neumaticos", "elegir_compuesto"}
        missing = [n for n in names if not hasattr(eng.assistant, "t_" + n)]
        self.assertEqual(missing, [], "intención sin respuesta")

    def test_battery_on_every_scenario_never_raises_and_always_answers(self):
        """Antes de la primera vuelta, a mitad de carrera y tras el final, con paquetes perdidos, SC, pinchazo..."""
        for sc in SCENARIOS:
            for t_stop in (3, 150, 10 ** 6):
                eng, _, _ = run_until(sc, t_stop)
                # solo las frases que no evalúan estrategia en todos los puntos, para no tardar; las de estrategia, en uno
                phrases = [p for p in BATTERY if t_stop == 150 or not re.search(
                    r"parar|undercut|overcut|alternativa|estrategia|hacer|duro|medio|neumático\?|podemos|objetivo|quinto|podio|propón|vamos bien", p)]
                for ph in phrases:
                    text = ph.replace("{P}", next(iter(eng.tr.names.values()), "Hansen"))
                    with self.subTest(scenario=sc, t=t_stop, phrase=text[:40]):
                        r = eng.ask(text)
                        self.assertIsInstance(r, str)
                        self.assertTrue(r.strip())

    def test_alerts_have_a_topic_or_are_never_silenced(self):
        """Silenciar un tema no puede tragarse avisos críticos: solo claves declaradas en TOPIC_OF."""
        for k, topic in T.TOPIC_OF.items():
            self.assertIn(topic, T.TOPICS, k)
        for crit in ("wear", "sc", "red", "pena", "final", "blown"):
            self.assertNotIn(crit, T.TOPIC_OF)

    def test_all_scenarios_still_finish_and_do_not_crash_with_all_topics_muted_or_boosted(self):
        for sc in ("sprint", "corta"):
            path, _ = demo(sc)
            for level in (-1, 1):
                out = []
                eng = E.Engineer(cfg(), "normal", None, out=out.append, sync=True)
                eng.tac.topics.update({t: level for t in T.TOPICS})
                for t, d in read_capture(path):
                    eng.on_packet(t, d)
                self.assertTrue(any("Fin de carrera" in l for l in out), (sc, level))


if __name__ == "__main__":
    unittest.main()
