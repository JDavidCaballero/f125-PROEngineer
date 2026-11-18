#!/usr/bin/env python3
"""El ingeniero: el LLM entiende lo que dices, elige qué herramientas del programa consultar y redacta la respuesta.

Reparto de trabajo (la regla de siempre: el modelo NO aporta datos ni hace cuentas):
  1. ROUTER  — el LLM lee tu frase (+ sesión, pilotos, último intercambio) y devuelve JSON: qué herramientas llamar con qué
               argumentos, o una frase breve si solo quieres charla/ánimos. Una frase que no es una pregunta del coche no es un error.
  2. HERRAMIENTAS — el código ejecuta las consultas (posición, huecos, neumáticos, ERS, estrategia...) con los datos del juego.
  3. REDACCIÓN — el LLM reescribe el resultado en lenguaje natural; si cambia una cifra o un nombre, se descarta y se dice el texto original.
Validaciones: pilotos contra la lista real, números contra el resultado, acciones que cambian el estado solo en frases cortas
(si no, pregunta antes). Si el servidor no responde o devuelve basura, se usan las reglas de siempre (assistant.parse_rules).
"""
import json
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field

import llm as L

# Herramientas visibles para el modelo: nombre -> (intención interna, descripción de argumentos). Solo lo que el código sabe hacer.
TOOLS = {
    "posicion": "", "hueco": "driver | dir=delante|detras", "piloto": "driver | pos=N (quién va PN)", "companero": "",
    "neumaticos": "driver | dir=delante|detras | otros=true; sin args=los míos; también vida restante («cuántas vueltas aguanta»)", "combustible": "", "ers": "batería ahora",
    "ers_vuelta": "uso del ERS en mi vuelta", "danos": "", "clima": "ahora y pronóstico: cuándo llueve, cuándo para, la próxima sesión", "vueltas": "las que quedan", "ritmo": "mis tiempos",
    "mejor_vuelta": "driver | sesion=true", "analisis_vuelta": "dónde pierdo tiempo", "comparar": "driver | con=primero|companero|delante",
    "estimacion": "según entrenos", "sesion": "qué sesión es", "actualizacion": "novedades/resumen ahora", "campeonato": "",
    "estrategia": "qué hacer, plan", "compuesto": "comps=[blando|medio|duro]; vacío=el mejor", "parar_ya": "", "undercut": "parar ANTES que un rival para ganarle la posición",
    "overcut": "alargar: parar DESPUÉS que el rival", "objetivo": "pos=N | driver | accion=estado|borrar", "viabilidad": "pos=N (¿podemos?)",
    "sugerir_objetivo": "", "avisos": "topic=combustible|ers|drs|sectores|adelantamiento|rivales|alternativas|vueltas, level=-1|0|1",
    "silencio": "", "reanuda": "", "gomas_mal": "no rinden", "resumen": "de la carrera", "repite": "", "explica": "",
    "dejar_pasar": "driver", "vs_rival": "driver (sectores)", "cerrar": "terminar/cerrar el programa o la sesión",
    "como_adelantar": "driver (si no, el de delante): cómo pasarlo, dónde atacar",
    "plan_carrera": "laps=N vueltas de la carrera (si las dice); plan de carrera con lo medido en entrenos y clasificación",
    "que_pasa_si": "hipótesis: parar=ahora|N (vuelta) | parar=no (seguir fuera) | comps=[...] | driver (rival); «si paro ahora…», «¿y si paro en la 12?»",
    "ritmo_rival": "driver | dir=delante|detras: ritmo de un rival frente al mío",
    "estado_coche": "cómo está el coche: daños, gomas, combustible y batería a la vez",
    "ventana_parada": "entre qué vueltas conviene parar",
    "faltan_parar": "qué rivales cercanos aún tienen que parar (o si el de delante ya paró)",
    "alcance": "driver | dir=delante|detras: en cuántas vueltas me alcanza un rival o le alcanzo yo",
}
# herramientas que cambian el estado: con frases largas o varias herramientas a la vez, primero se pregunta
RISKY = {"silencio", "objetivo", "gomas_mal", "avisos", "parar_ya"}
# solo estas se reescriben (las propuestas conservan su texto exacto: llevan «¿Lo hacemos?»)
COMPOSE_OK = {"posicion", "hueco", "piloto", "companero", "neumaticos", "combustible", "ers", "ers_vuelta", "danos", "clima",
              "vueltas", "ritmo", "mejor_vuelta", "analisis_vuelta", "comparar", "estimacion", "sesion", "actualizacion",
              "campeonato", "vs_rival", "dejar_pasar", "resumen"}
COMP_IDS = {"blando": 16, "medio": 17, "duro": 18, "blandos": 16, "medios": 17, "duros": 18}
MAX_WORDS_RISKY = 14
# una herramienta que cambia el estado solo vale si la frase del piloto lo respalda con palabras (el modelo se equivocó con
# «no me siento cómodo con las gomas» -> avisos): sin respaldo se descarta y mandan las reglas
SUPPORT = {
    "silencio": r"\b(calla\w*|silencio|no hables|sin avisos|no digas|dejes de hablar|dejar de hablar)\b",
    "avisos": r"\b(avis\w*|actualiz\w*|mensajes?|informacion|novedades|menos|mas|digas|decir)\b",
    "objetivo": r"\b(quiero|objetivo|acabar|terminar|quedar|pasar|meta|borra\w*|olvida\w*|ganar|podio|llegar)\b",
    "gomas_mal": r"\b(gomas?|neumaticos?|ruedas?|agarre|comod\w*|patina\w*|rinden?|gastad\w*|muert\w*)\b",
    "clima": r"\b(llov\w*|lluvi\w*|clima|meteo\w*|nubl\w*|sol|mojad\w*|seco|seca\w*|humed\w*|temperatura|tormenta|tiempo hace|"
             r"tiempo va a hacer|tiempo hara|cambi\w* el tiempo|el tiempo|pronostico)\b",
    "campeonato": r"\b(campeonato|mundial|constructores|puntos|clasificacion general)\b",
    "ers": r"\b(ers|bater\w*|energi\w*|desplieg\w*|carga\w*|mgu|electric\w*|adelantamiento|overtake)\b",
    "danos": r"\b(dan\w*|rot\w*|alero\w*|aleron\w*|golpe\w*|choque\w*|contacto|pieza\w*|suspension|averia\w*|estado del coche)\b",
    "combustible": r"\b(combustible|gasolina|fuel|deposito|kg|litros?|consum\w*|margen|ahorr\w*|queda\w*|llevo)\b",
    "plan_carrera": r"\b(carrera|plan|estrategia|vueltas|gp|distancia)\b",
    "como_adelantar": r"\b(adelant\w*|pas(?:o|a|ar|arlo|arle|arla)|pillo|pillar|gano|ganar|ataco|atacar|supero|superar\w*)\b",
    "parar_ya": r"\b(par\w*|box\w*|entr\w*|pit|cambi\w*|neumatic\w*|gomas?)\b",
    # una hipótesis de parada habla de parar, alargar o de un compuesto («¿me alcanza la gasolina si ataco?» no lo es)
    "que_pasa_si": r"\b(par[oaei]\w*|box\w*|entr\w*|alarg\w*|aguant\w*|sigo|seguir|duros?|medios?|blandos?|neumatic\w*|gomas?)\b",
}
# una frase larga sin pregunta ni orden es una afirmación del piloto («termino el entrenamiento con el medio»): se acusa recibo
ASK_MARK = re.compile(r"\b(que|cual|cuales|cuanto|cuanta|cuantos|cuantas|como|donde|cuando|quien|quienes|puedes|puedo|podria|podemos|"
                      r"debo|deberia|conviene|hay|dime|dame|decime|avisame|explica\w*|repite\w*|calla\w*|quiero|necesito|ayuda\w*|"
                      r"compara\w*|informacion|recomiendas|recomienda|sugieres|piensas)\b")
MULTI_MARK = re.compile(r"\b(y|ademas|tambien|luego|despues)\b")
MIN_COMPOSE_WORDS = 6          # respuestas más cortas no se reescriben (no ganan nada y cuestan una llamada)
COMPOSE_KEEP, COMPOSE_NEW = 0.70, 0.40   # solapamiento de palabras con el texto técnico (calibrado con Qwen real)
COMPOSE_BANNED = {"hola", "animo", "vamos", "genial", "sigue", "pilotos", "felicidades", "excelente"}

# una afirmación del piloto habla de sí mismo («termino el entrenamiento con el medio», «voy a entrar»); sin eso, una frase sin
# signos («mejor neumático para estas condiciones», Whisper no pone «¿?») es una pregunta: antes se respondía «Entendido.»
FIRST = re.compile(r"\b(voy|vamos|estoy|entro|termino|empiezo|salgo|paro|tengo|llevo|he|hice|acabo|vengo|me|mi|creo|quiero|ya|"
                   r"\w+(?:amos|emos|imos))\b")                     # «ganamos», «terminamos», «hemos»
# frase que se refiere al piloto anterior: «¿y él?», «¿y sus gomas?», «¿y qué lleva?», «lo paso?»
FOLLOW = re.compile(r"^(y|e)\b|\b(el|su|sus|le|lo|ese|este)\b")
THIRD = re.compile(r"\b(lleva|tiene|va|esta|ha|hace|hizo|para|paro|su|sus|el|le|lo)\b")     # habla de otro, no de mí
_bare = lambda t: re.sub(r"[^\w\s]", " ", t).strip()                            # sin «¿», comas…: «¿y él?» empieza por «y»
DRIVER_TOOLS = {"neumaticos", "ritmo_rival", "alcance", "hueco", "como_adelantar", "mejor_vuelta", "comparar", "vs_rival", "piloto"}
# opcional ([llm] router_plan = true): el modelo escribe primero un plan corto (qué pide el piloto, qué datos hacen falta)
ROUTER_PLAN = ("\n- Antes de \"tools\", escribe \"plan\": como mucho 12 palabras con lo que pide el piloto y qué datos hacen falta "
               "(p. ej. \"pregunta hipotética: parar ahora frente a rival que alarga\"). Formato: {\"plan\":\"...\",\"tools\":[...],\"say\":\"\"}.")
SYS_ROUTER = (
    "Eres el cerebro de un ingeniero de pista de F1 que habla con su piloto por radio. Recibes la frase del piloto (transcrita "
    "de voz, puede tener errores) y decides qué HERRAMIENTAS del programa consultar. No inventes datos.\n"
    "Responde SOLO JSON: {\"tools\":[{\"t\":\"<herramienta>\", ...args}], \"say\":\"\"}.\n"
    "- Usa UNA herramienta, salvo que el piloto pida claramente dos cosas. Pon 'driver' SOLO si el piloto nombra a alguien "
    "(nombre EXACTO de la lista 'pilotos', corrigiendo errores de voz); 'neumaticos' sin driver = los míos.\n"
    "- Si el piloto solo charla, pide ánimos o saluda: tools=[] y en say una frase corta (máx 20 palabras, sin cifras, sin datos); reacciona SOLO a esta frase, nunca a lo dicho antes; no menciones la vuelta, la carrera ni neumáticos salvo que el piloto lo diga; no repitas frases hechas.\n"
    "- Si pide algo que ninguna herramienta puede dar: tools=[] y say=\"\".\n"
    "- Si la frase parece ruido o una mala transcripción sin sentido: {\"tools\":[],\"say\":\"\",\"unclear\":true}.\n"
    "- En say nunca prometas victorias ni digas que haces algo (volumen, música, ajustes) que no puedes hacer.\n"
    "- Preguntas sobre cuándo parar o si debe entrar a boxes -> estrategia/parar_ya; 'tiempo' de vuelta de alguien NO es clima.\n"
    "- Argumentos en el mismo objeto de la herramienta, sin anidar.\n"
    "- Una afirmación del piloto ('entro a boxes', 'empiezo vuelta lanzada') no es una orden: tools=[] y say='Entendido.'\n"
    "- 'yo', 'delante', 'detras', 'propuesta_pendiente', 'ultimo_piloto' y 'previo' son contexto para entender a quién o a qué "
    "se refiere ('¿y él?' = ultimo_piloto; 'el de delante' = delante). No repitas esos datos en say.\n"
    "- Una pregunta hipotética ('si paro ahora…', '¿y si paro en la 12 a duros?', '¿y si no paro?') es que_pasa_si.\n"
    "Ejemplos: '¿quién va segundo?' -> {\"tools\":[{\"t\":\"piloto\",\"pos\":2}],\"say\":\"\"}; "
    "'compárame con mi compañero' -> {\"tools\":[{\"t\":\"comparar\",\"con\":\"companero\"}],\"say\":\"\"}; "
    "'menos avisos de combustible' -> {\"tools\":[{\"t\":\"avisos\",\"topic\":\"combustible\",\"level\":-1}],\"say\":\"\"}; "
    "'más actualizaciones del ERS' -> {\"tools\":[{\"t\":\"avisos\",\"topic\":\"ers\",\"level\":1}],\"say\":\"\"}; "
    "'no me siento cómodo con las gomas' -> {\"tools\":[{\"t\":\"gomas_mal\"}],\"say\":\"\"}; "
    "'¿qué tal el neumático medio?' -> {\"tools\":[{\"t\":\"compuesto\",\"comps\":[\"medio\"]}],\"say\":\"\"}; "
    "'qué tiempo ha hecho el primero' -> {\"tools\":[{\"t\":\"piloto\",\"pos\":1}],\"say\":\"\"}; "
    "'¿cuándo para de llover?' / '¿cuándo se seca la pista?' / '¿cuándo cambia el tiempo?' -> {\"tools\":[{\"t\":\"clima\"}],\"say\":\"\"}; "
    "'cuándo paro' -> {\"tools\":[{\"t\":\"estrategia\"}],\"say\":\"\"}; "
    "'¿cómo adelanto a Verstappen?' -> {\"tools\":[{\"t\":\"como_adelantar\",\"driver\":\"Verstappen\"}],\"say\":\"\"}; "
    "'¿cómo voy contra Alves?' -> {\"tools\":[{\"t\":\"vs_rival\",\"driver\":\"Alves\"}],\"say\":\"\"}  (comparar, NO es como_adelantar); "
    "'¿qué hago para pasar al de delante?' -> {\"tools\":[{\"t\":\"como_adelantar\"}],\"say\":\"\"}; "
    "'ganamos la carrera' -> {\"tools\":[],\"say\":\"¡Buen trabajo, piloto!\"}; "
    "'termino el entrenamiento con el medio' -> {\"tools\":[],\"say\":\"Entendido.\"}; "
    "'¿cuántas vueltas más aguanta este neumático?' -> {\"tools\":[{\"t\":\"neumaticos\"}],\"say\":\"\"}; "
    "'mándame ánimos' -> {\"tools\":[],\"say\":\"Tranquilo, tienes el ritmo.\"}.\n"
    "Herramientas: " + "; ".join(f"{k}({v})" if v else k for k, v in TOOLS.items())
)
SYS_COMPOSE = (
    "Eres el ingeniero de pista de F1 y hablas por radio. Reescribe la respuesta técnica en español natural y directo, con las "
    "MISMAS frases en el mismo orden: solo une frases y quita repeticiones. Conserva EXACTAMENTE cada cifra, nombre y el significado "
    "de cada palabra ('vueltas de uso' sigue siendo 'vueltas de uso'). No añadas saludos, ánimos, datos, causas ni consejos. "
    "Responde solo con el texto."
)
UNKNOWN = ("No tengo ese dato en la telemetría. Puedo decirte posición, huecos, neumáticos, combustible, ERS, daños, tiempos, "
           "estrategia o comparar con otro piloto.")


@dataclass
class Plan:
    intents: list = field(default_factory=list)       # Intent a ejecutar
    say: str = ""                                     # respuesta directa (charla, acuse) o pregunta de confirmación
    source: str = "reglas"                            # reglas | llm
    understood: bool = True
    unclear: bool = False                             # el modelo cree que es ruido/mala transcripción: se pide repetir
    confirm: object = None                            # Intent pendiente de «sí»
    tools: list = field(default_factory=list)         # nombres de herramientas (para auditoría)
    llm_s: float = 0.0
    raw: str = ""
    dropped: bool = False                             # el modelo pidió herramientas pero los controles las descartaron


def unaccent(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


CHAT_TOPICS = {"blando", "medio", "duro", "intermedio", "lluvia", "boxes", "undercut", "overcut", "combustible", "bateria", "ers", "drs"}


CHAT_PHRASES = re.compile(r"\bno (te )?(entiendo|comprendo)\b")
CHAT_BANNED = {"iniciando", "abriendo", "arrancando", "configurando", "preparando", "victoria", "ganar", "ganaremos", "ganaras", "ganamos", "podio", "posible", "seguro", "ajustando", "subiendo", "bajando",
               "poniendo", "activando", "muestro", "mostrando", "aplicando", "cambiando", "guardado", "guardados"}


def clean_say(text, said=None):
    """Frase de charla válida: corta, sin cifras (no puede aportar datos) y sin saltos. Si se da la frase del piloto (sin
    acentos), tampoco puede hablar de neumáticos, boxes, etc. que él no ha mencionado (el modelo arrastraba el contexto previo)."""
    t = re.sub(r"\s+", " ", (text or "").strip().strip('"'))
    if not t or len(t.split()) > 30 or re.search(r"\d|%", t):
        return ""
    if CHAT_PHRASES.search(unaccent(t)):
        return ""                                         # «no entiendo qué quieres decir»: eso lo decide el programa, no el modelo
    if set(re.findall(r"[a-z]+", unaccent(t))) & CHAT_BANNED:   # promesas de resultado o acciones que el programa no hace
        return ""
    if said is not None and (set(re.findall(r"[a-z]+", unaccent(t))) & CHAT_TOPICS) - set(re.findall(r"[a-z]+", said)):
        return ""
    return t


class Brain:
    def __init__(self, eng, client, cfg=None):
        c = (cfg or {}).get("llm", {})
        self.cfg_llm = c
        self.eng, self.client = eng, client
        self.compose_on = c.get("compose", True)
        self.cooldown_s = c.get("down_cooldown_s", 30.0)
        self.down_until = 0.0
        self.history = deque(maxlen=2)               # (pregunta, respuesta recortada)
        self.last_driver = None                      # (idx, nombre) del último piloto del que se habló
        self.stats = {"calls": 0, "errors": 0, "compose_rejected": 0}

    # ------------------------------------------------------------------------------ disponibilidad
    def available(self):
        return self.client is not None and time.time() >= self.down_until

    def _fail(self, why):
        self.stats["errors"] += 1
        self.down_until = time.time() + self.cooldown_s
        self.eng._log({"type": "llm_error", "why": str(why)[:200], "retry_in_s": self.cooldown_s})

    # ------------------------------------------------------------------------------ router
    def context(self, text):
        tr = self.eng.tr
        names = sorted({n for i, n in tr.names.items() if n and i != tr.player})
        ses = tr.ses or {}
        ctx = {"frase": text, "sesion": tr.session_kind(), "pilotos": names[:22]}
        if self.cfg_llm.get("router_context", True):
            ctx.update(self.situation())
        if self.history:
            ctx["previo"] = [{"p": q, "r": a} for q, a in self.history]
        return ctx

    def situation(self):
        """Resumen corto de la carrera para que el modelo entienda referencias («¿y él?», «sí, pero en dos vueltas»). Sin cifras
        que pueda repetir: solo nombres, posiciones y estado; las cifras las pone el programa."""
        tr, eng = self.eng.tr, self.eng
        out = {}
        try:
            me = tr.lap_all[tr.player] if tr.player is not None and tr.player < len(tr.lap_all) else None
            if me:
                out["yo"] = {"pos": me["position"], "vuelta": tr.cur_lap, "total": (tr.ses or {}).get("total_laps")}
            ahead, behind = tr.neighbours()
            if ahead:
                out["delante"] = tr.name(ahead[0])
            if behind:
                out["detras"] = tr.name(behind[0])
            if eng.dialog.pending:
                out["propuesta_pendiente"] = eng.dialog.pending.option.desc
            if eng.dialog.accepted:
                out["plan_aceptado"] = eng.dialog.accepted.option.desc
            if self.last_driver:
                out["ultimo_piloto"] = self.last_driver[1]
        except (AttributeError, IndexError, KeyError, TypeError):
            pass
        return out

    def route(self, text):
        """Plan a partir del LLM, o None si no responde / devuelve algo inservible (entonces mandan las reglas)."""
        import json
        t0 = time.time()
        self.stats["calls"] += 1
        try:
            c = self.cfg_llm
            sys_p = SYS_ROUTER + (ROUTER_PLAN if c.get("router_plan") else "")
            raw = self.client.chat(sys_p, json.dumps(self.context(text), ensure_ascii=False),
                                   max_tokens=c.get("router_max_tokens", 180 if c.get("router_plan") else 120), temperature=0.0,
                                   reasoning=c.get("router_reasoning"))
        except L.LLMError as e:
            self._fail(e)
            return None
        obj = L.extract_json(raw)
        if not isinstance(obj, dict) or not isinstance(obj.get("tools", []), list):
            self.eng._log({"type": "llm_bad_json", "raw": raw[:200]})
            return None
        plan = self.to_plan(text, obj)
        plan.llm_s = time.time() - t0
        plan.raw = raw
        return plan

    def to_plan(self, text, obj):
        from assistant import Intent
        tr = self.eng.tr
        plan = Plan(source="llm")
        n_words = len(text.split())
        plain = tr.plain(text)
        calls, seen = [], set()
        for t in obj.get("tools", []):
            if isinstance(t, dict) and isinstance(t.get("args"), dict):      # a veces el modelo anida los argumentos
                t = {**t.pop("args"), **t}
            if not (isinstance(t, dict) and t.get("t") in TOOLS):
                continue
            key = json.dumps(t, sort_keys=True)
            if key in seen or (t["t"] in SUPPORT and not re.search(SUPPORT[t["t"]], plain)):
                continue                                         # repetida, o acción de estado sin respaldo en la frase
            seen.add(key)
            calls.append(t)
        asked = [t for t in obj.get("tools", []) if isinstance(t, dict)]
        if len(calls) > 1 and not MULTI_MARK.search(plain):
            calls = calls[:1]                                   # dos herramientas sin «y»/«además»: la segunda sobra («pole»→piloto)
        if (calls and n_words >= 5 and not re.search(r"[?¿]", text) and not ASK_MARK.search(plain) and FIRST.search(plain)
                and not any(t["t"] in RISKY for t in calls)):
            plan.say = clean_say(obj.get("say"), plain) or "Entendido."
            return plan
        for t in calls[:2]:
            intent = self.intent_from_tool(t, text)
            if intent is not None:
                plan.intents.append(intent)
                plan.tools.append(t["t"])
        plan.dropped = bool(asked) and not plan.intents
        risky = [t for t in plan.tools if t in RISKY]
        if risky and (n_words > MAX_WORDS_RISKY or len(plan.intents) > 1):
            # una frase larga o varias órdenes a la vez: antes de cambiar nada se pregunta
            plan.confirm = next(i for i, t in zip(plan.intents, plan.tools) if t in RISKY)
            plan.say = f"¿Quieres que {self.describe(risky[0], plan.confirm)}? Di sí o no."
            plan.intents, plan.tools = [], risky[:1]
            return plan
        if not plan.intents and not plan.dropped:           # si descartó su herramienta, su frase tampoco vale (hablaba de otra cosa)
            plan.say = clean_say(obj.get("say"), plain) or ""
            plan.unclear = bool(obj.get("unclear")) and not plan.say
        return plan

    @staticmethod
    def describe(tool, intent):
        return {"silencio": "me calle y solo avise de lo crítico", "objetivo": "fije ese objetivo", "gomas_mal": "asuma que las gomas rinden peor",
                "avisos": "cambie los avisos", "parar_ya": "evalúe parar ahora"}.get(tool, "haga eso")

    def intent_from_tool(self, t, text=None):
        from assistant import Intent, INTENTS
        tr = self.eng.tr
        name = t["t"]
        driver, ambiguous = None, []
        d = t.get("driver")
        if isinstance(d, str) and d.strip():
            found = tr.find_driver(d)
            if text is not None:                                 # el piloto debe estar nombrado en la frase (el modelo inventaba uno de la lista)
                said = {k for k, _ in tr.find_driver(text)}
                if self.last_driver and FOLLOW.search(_bare(tr.plain(text))):
                    said.add(self.last_driver[0])                 # «¿y él?», «¿y qué gomas lleva?»: el último piloto del que hablamos
                found = [f for f in found if f[0] in said]
            driver = found[0] if len(found) == 1 else None
            ambiguous = found if len(found) > 1 else []
        if (driver is None and not ambiguous and not d and text is not None and self.last_driver and name in DRIVER_TOOLS
                and FOLLOW.search(_bare(tr.plain(text))) and THIRD.search(tr.plain(text))
                and not re.search(r"\b(delante|detras|atras|yo|mis?|mio|me)\b", tr.plain(text))):
            driver = self.last_driver                          # «¿y qué gomas lleva?» tras hablar de Alves: el modelo no puso el piloto
        pos = t.get("pos") if isinstance(t.get("pos"), int) and 1 <= t["pos"] <= 22 else None
        mk = lambda n, **k: Intent(n, driver=driver, ambiguous=ambiguous, source="llm", **k)
        if name == "hueco":
            if t.get("dir") == "delante" and not driver:
                return mk("hueco_delante")
            if t.get("dir") == "detras" and not driver:
                return mk("hueco_detras")
            return mk("hueco")
        if name == "neumaticos":
            comps = [COMP_IDS[c] for c in (t.get("comps") or []) if c in COMP_IDS]
            if t.get("otros") or t.get("comps"):
                return mk("compuesto_rivales", compound=comps[0]) if comps else mk("rivales_neumaticos")
            if driver or t.get("dir") in ("delante", "detras"):
                return mk("rival_neumaticos", topic=t.get("dir"))
            return mk("neumaticos")
        if name == "compuesto":
            comps = tuple(COMP_IDS[c] for c in (t.get("comps") or []) if c in COMP_IDS)
            return mk("compuesto", compound=comps[0]) if len(comps) == 1 else mk("elegir_compuesto", comps=comps)
        if name == "comparar":
            con = t.get("con") if t.get("con") in ("primero", "companero", "delante") else None
            return mk("compara_vuelta", topic=con) if (driver or con) else None
        if name == "objetivo":
            acc = t.get("accion")
            if acc == "borrar":
                return mk("objetivo_borrar")
            if acc == "estado":
                return mk("objetivo_estado")
            if driver:
                return mk("objetivo_pasar")
            return mk("objetivo_pos", pos=pos) if pos else None
        if name == "avisos":
            from tactics import TOPICS
            topic = t.get("topic") if t.get("topic") in TOPICS else None
            lvl = t.get("level") if t.get("level") in (-1, 0, 1) else -1
            return mk("avisos", topic=topic, level=lvl)
        if name in ("vs_rival", "dejar_pasar") and not driver:
            return None
        if name == "viabilidad":
            return mk("viabilidad", pos=pos) if pos else None
        mine = text is not None and re.search(r"\b(voy|estoy|mi|me|llevo|vamos)\b", tr.plain(text)) and not driver
        if name == "piloto" and mine and not re.search(r"\b(quien|quienes)\b", tr.plain(text)):
            name, pos = "posicion", None                     # «¿de qué posición voy?» es la mía (informe 11 nov: respondía mi mejor vuelta)
        if name == "posicion" and (pos or driver) and not mine:  # «posicion» es la mía: con un puesto o un piloto se pregunta por otro
            name = "piloto"
        if name == "plan_carrera":
            n = t.get("laps")
            said = set(re.findall(r"\d+", text or ""))
            ok = isinstance(n, int) and n > 0 and (text is None or str(n) in said or tr.plain(text).count(" ") < 0)
            return mk("plan_carrera", topic=str(n) if ok else None)           # el número de vueltas debe estar en la frase (el modelo inventó 18)
        if name == "piloto":
            return mk("piloto", pos=None if driver else pos) if (driver or pos) else None
        if name == "parar_ya" and text is not None and (driver or re.search(
                r"\b(el de|el coche|los de|delante|adelante|detras|atras|ha parado|ya paro|ya entro|rival)\b", tr.plain(text))):
            # «el coche de adelante ya paró» habla de OTRO coche: no es pedir parar (informe 11 nov: preparó una parada mía)
            return mk("faltan_parar", topic="detras" if re.search(r"\b(detras|atras)\b", tr.plain(text)) else "delante")
        if name == "que_pasa_si":
            if text is not None and re.search(r"\b(safety|sc|vsc|coche de seguridad)\b", tr.plain(text)):
                return mk("estrategia")                         # «si sale el safety car, ¿paro?»: el plan ya contempla reaccionar al SC
            p_ = t.get("parar")
            comps = [COMP_IDS[c] for c in (t.get("comps") or []) if c in COMP_IDS]
            lap = p_ if isinstance(p_, int) and 1 <= p_ <= 99 else None
            if lap is not None and text is not None and str(lap) not in re.findall(r"\d+", text) and not re.search(r"\b(vuelta|la)\b", tr.plain(text)):
                lap = None                                     # la vuelta debe salir de la frase
            return mk("que_pasa_si", pos=lap, compound=comps[0] if comps else None,
                      topic="seguir" if p_ in ("no", "nunca", "seguir", False) else None)
        if name in ("ritmo_rival", "alcance"):
            return mk(name, topic=t.get("dir") if t.get("dir") in ("delante", "detras") else None)
        if name == "mejor_vuelta":
            return mk("mejor_vuelta", topic="sesion" if t.get("sesion") else None)
        return mk(name) if name in INTENTS else None

    # ------------------------------------------------------------------------------ redacción
    def compose(self, text, tools, raw_reply):
        """Reescribe raw_reply con el LLM; devuelve el texto nuevo o raw_reply si algo no cuadra."""
        import json
        if not (self.compose_on and self.available() and tools and all(t in COMPOSE_OK for t in tools)):
            return raw_reply
        if not MIN_COMPOSE_WORDS <= len(raw_reply.split()) <= 60 or "¿" in raw_reply:
            return raw_reply
        self.stats["calls"] += 1
        try:
            out = self.client.chat(SYS_COMPOSE, json.dumps({"pregunta": text, "respuesta_tecnica": raw_reply}, ensure_ascii=False),
                                   max_tokens=120, temperature=0.1)
        except L.LLMError as e:
            self._fail(e)
            return raw_reply
        out = re.sub(r"\s+", " ", out.strip().strip('"'))
        names = {n for n in self.eng.tr.names.values() if n}
        ok = (out and len(out.split()) <= 70 and L.numbers_ok(out, L.allowed_numbers(raw_reply), tol=0.0501)
              and not any(n in out and n not in raw_reply for n in names) and self.same_meaning(raw_reply, out))
        if not ok:
            self.stats["compose_rejected"] += 1
            self.eng._log({"type": "llm_compose_rejected", "original": raw_reply[:200], "text": out[:200]})
            return raw_reply
        return out

    @staticmethod
    def _words(t):
        import unicodedata
        t = re.sub("[\u0300-\u036f]", "", unicodedata.normalize("NFD", t.lower()))
        return {w for w in re.findall(r"[a-z]{4,}", t)}

    def same_meaning(self, raw, out):
        """Sin cambiar el sentido: casi todas las palabras del texto técnico siguen y casi ninguna es nueva (ni saludos ni ánimos)."""
        a, b = self._words(raw), self._words(out)
        if not a or not b:
            return False
        return (len(a & b) / len(a) >= COMPOSE_KEEP and len(b - a) / len(b) <= COMPOSE_NEW and not (b & COMPOSE_BANNED))

    def remember(self, q, a):
        self.history.append((q, " ".join(a.split()[:14])))
