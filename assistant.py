#!/usr/bin/env python3
"""Lo que dices tú: intención -> consulta en código -> respuesta con datos. Nada de suponer.

Orden: 1) reglas (palabras clave sin acentos), 2) si fallan y hay modelo, el LLM solo CLASIFICA entre intenciones
fijas (el piloto que nombre se valida contra la lista real), 3) si nada encaja: "No he entendido".
Las respuestas las arma el código con los datos del juego; el modelo nunca redacta cifras aquí.
"""
import json, re
from dataclasses import dataclass, field

import dialog as D
from answers import SessionAnswers
import llm as L
import strategy as S
from state import WHEELS

INTENTS = ["si", "no", "explica", "repite", "silencio", "reanuda", "hueco", "hueco_delante", "hueco_detras", "posicion",
           "neumaticos", "combustible", "ahorro", "ers", "estrategia", "undercut", "parar_ya", "gomas_mal", "incomodo",
           "ritmo", "por_que_pierdo", "danos", "clima", "vueltas", "objetivo_pos", "objetivo_pasar", "objetivo_estado",
           "objetivo_borrar", "dejar_pasar", "vs_rival", "viabilidad", "sugerir_objetivo", "campeonato", "companero", "alternativa", "overcut", "resumen", "analisis_vuelta", "ers_vuelta", "compara_vuelta", "estimacion", "sesion", "mejor_vuelta", "piloto", "actualizacion", "ack", "cerrar", "plan_carrera", "como_adelantar", "animo",
           "que_pasa_si", "ritmo_rival", "estado_coche", "ventana_parada", "faltan_parar", "alcance"]
NEEDS_DRIVER = {"objetivo_pasar", "dejar_pasar", "vs_rival"}
TOPIC_WORDS = {"combustible": "combustible", "gasolina": "combustible", "fuel": "combustible", "ers": "ers",
               "bateria": "ers", "energia": "ers", "drs": "drs", "sector": "sectores", "sectores": "sectores",
               "adelantamiento": "adelantamiento", "adelantamientos": "adelantamiento", "ataque": "adelantamiento",
               "alternativas": "alternativas", "arriesgadas": "alternativas", "riesgo": "alternativas",
               "rivales": "rivales", "rival": "rivales", "vueltas": "vueltas", "analisis": "vueltas",
               "defensa": "adelantamiento"}
RACE_POINTS = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]
MAX_WORDS = 14            # una orden de radio es corta; algo más largo es otra conversación y no se interpreta
# El LLM solo puede clasificar CONSULTAS. Nada que cambie el estado (sí/no, silencio, parar, borrar objetivo, gomas...):
LLM_SAFE = {"hueco", "hueco_delante", "hueco_detras", "posicion", "neumaticos", "combustible", "ahorro", "ers", "danos",
            "clima", "vueltas", "ritmo", "por_que_pierdo", "estrategia", "objetivo_estado", "repite", "explica",
            "analisis_vuelta", "ers_vuelta", "estimacion", "sesion", "mejor_vuelta", "actualizacion"}
COMP = {"duro": 18, "duros": 18, "dura": 18, "medio": 17, "medios": 17, "blando": 16, "blandos": 16, "blanda": 16}
ORD = {"primero": 1, "primer": 1, "segundo": 2, "tercero": 3, "tercer": 3, "cuarto": 4, "quinto": 5, "sexto": 6,
       "septimo": 7, "octavo": 8, "noveno": 9, "decimo": 10, "undecimo": 11, "duodecimo": 12}
# si el modelo solo charla pero las reglas reconocen estas consultas (sin cambiar nada), mandan las reglas: no se contesta con ánimos a una pregunta
RULE_OVER_CHAT = {"posicion", "hueco", "hueco_delante", "hueco_detras", "neumaticos", "combustible", "ers", "danos", "clima", "vueltas", "ritmo",
                  "viabilidad", "estrategia", "como_adelantar", "undercut", "overcut", "alternativa", "companero", "vs_rival", "analisis_vuelta", "ers_vuelta",
                  "compara_vuelta", "estimacion", "sesion", "mejor_vuelta", "piloto", "actualizacion", "silencio", "reanuda", "resumen", "plan_carrera", "animo"}
NO_UNDERSTAND = "No he entendido. ¿Puedes repetirlo?"
FAST = {"si", "no", "repite", "silencio", "reanuda", "explica"}        # con el LLM como cerebro, solo estas van directas por reglas
YES = {"si", "dale", "ok", "okay", "vale", "hazlo", "confirmo", "confirmado", "afirmativo", "adelante", "claro",
       "de acuerdo", "venga", "por favor"}
NO = {"no", "negativo", "descarta", "descartalo", "olvidalo", "rechazo", "ni hablar", "mejor no"}
YES_W = {w for p in YES for w in p.split()}
NO_W = {w for p in NO for w in p.split()}
# palabras que pueden acompañar a un sí/no sin cambiar su sentido («no, gracias», «vale, perfecto»)
FILLER = {"gracias", "ingeniero", "eso", "perfecto", "bueno", "va", "ya", "ahora", "todavia", "aun", "entonces", "porfa", "hombre", "tio"}


def yes_no(t, words):
    """'si' / 'no' solo si TODA la frase es un sí o un no («no agarra nada» o «vale cuánto queda» no lo son)."""
    if len(words) > 4:
        return None
    if t in YES:
        return "si"
    if t in NO:
        return "no"
    ws = set(words)
    if ws & (NO_W - {"mejor", "ni", "hablar"}) and ws <= NO_W | FILLER | {"por"}:
        return "no"
    if "no" not in ws and ws <= YES_W | FILLER and ws & (YES_W - {"por", "favor", "de", "acuerdo"}):
        return "si"
    return None


@dataclass
class Intent:
    name: str
    driver: tuple | None = None       # (idx, nombre)
    pos: int | None = None
    compound: int | None = None
    comps: tuple = ()                 # elegir_compuesto: compuestos a comparar (vacío = todos los de seco)
    topic: str | None = None          # avisos: tema (combustible, ers...)
    level: int = 0                    # avisos: -1 menos, 0 normal, +1 más
    ambiguous: list = field(default_factory=list)
    source: str = "reglas"


CARD = {"uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
        "diez": 10, "once": 11, "doce": 12}


def position_in(t):
    m = re.search(r"\bp\s?(\d{1,2})\b", t) or re.search(r"\b(?:posicion|puesto|lugar)\s+(?:numero\s+)?(\d{1,2})\b", t)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(?:posicion|puesto|lugar|p)\s+(?:numero\s+)?(" + "|".join(CARD) + r")\b", t)
    if m:
        return CARD[m.group(1)]
    for w, n in ORD.items():
        if re.search(rf"\b{w}\b", t):
            return n
    m = re.search(r"\b(?:en|el|al)\s+(\d{1,2})\b", t)
    return int(m.group(1)) if m else None


# Confusiones de transcripción OBSERVADAS en pruebas reales (se amplía con los informes; nada de corrección "difusa" que asuma cosas)
MISHEARS = {"cuento": "cuanto", "blanco": "blando", "blanca": "blanda"}


def canon_tokens(t, protect=()):
    return " ".join(MISHEARS.get(w, w) if w not in protect else w for w in t.split())


def _avisos(t, words):
    """(tema|None, nivel) si pide más/menos avisos ("menos avisos de combustible", "no me avises del ERS")."""
    topic = next((TOPIC_WORDS[w] for w in words if w in TOPIC_WORDS), None)
    level = None
    if re.search(r"\b(menos|reduce|reducir|baja|bajar)\b", t) or re.search(r"\bno me (avises|avise|digas)\b|\bdeja de (avisar|decir)\w*\b", t):
        level = -1
    elif re.search(r"\b(mas|aumenta|aumentar|sube|subir)\b", t) or re.search(r"\bavisame (mas|siempre)\b", t):
        level = 1
    elif re.search(r"\b(avisos|actualizaciones)\b.*\bnormales\b|\bvuelve a lo normal\b", t):
        level = 0
    if level is None:
        return None
    if topic is None and not re.search(r"\b(avis\w*|actualizaci\w*|mensajes|informacion|novedades)\b", t):
        return None
    if topic and level != 0 and not re.search(r"\b(avis\w*|actualizaci\w*|mensajes|informacion|novedades|digas|decir)\b", t):
        return None
    return topic, level


def comp_w(words):
    return next((COMP[w] for w in words if w in COMP), None)


def parse_rules(tr, text):
    """Intent o None si no está claro."""
    t = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", tr.plain(text))).strip()
    t = canon_tokens(t, {tr.plain(n) for n in tr.names.values() if n})
    if not t:
        return None
    words = t.split()
    if len(words) > MAX_WORDS:
        return None
    has = lambda *p: any(re.search(rf"\b{x}\b", t) for x in p)
    drv = tr.find_driver(text)
    d1 = drv[0] if len(drv) == 1 else None
    amb = drv if len(drv) > 1 else []
    mk = lambda n, **k: Intent(n, driver=d1, ambiguous=amb, **k)
    # --- respuestas cortas a una propuesta (solo si la frase entera es un sí o un no)
    yn = yes_no(t, words)
    if yn:
        return mk(yn)
    av = _avisos(t, words)
    if av:
        return mk("avisos", topic=av[0], level=av[1])
    if re.search(r"\b(como|que hago para|donde|en que (curva|recta|zona)|cuando)\b.*\b(adelant\w*|pas(?:o|a|ar|arlo|arle|arla)|pillo|gano|ataco|atacar|supero|superarle)\b", t) \
            and not has("neumatico", "gomas", "parar", "boxes", "pit"):
        return mk("como_adelantar")                          # «¿cómo adelanto a X?», «¿dónde puedo pasarlo?»
    if has("animo", "animos", "suerte", "motivame", "motivacion", "dame fuerza", "dame fuerzas") and not has("no"):
        return mk("animo")                                # ánimos: respuesta fija y variada, sin esperar al modelo
    if has("campeonato", "mundial", "constructores", "puntos"):
        return mk("campeonato")
    if has("silencio", "callate", "calla", "no hables", "sin avisos"):
        return mk("silencio")
    if has("reanuda", "ya puedes hablar", "puedes hablar", "habla otra vez", "vuelve a hablar"):
        return mk("reanuda")
    if has("repite", "repitelo", "otra vez", "que dijiste", "no te escuche"):
        return mk("repite")
    if re.search(r"^(explica|explicame|por ?que|razon|motivo|detalla|dame mas detalles|y por ?que)\b", t) \
            and not has("pierdo", "perdemos", "perdiendo", "tiempo", "lento"):
        return mk("explica")
    # --- viabilidad ("¿podemos ganar?", "¿podemos acabar quinto?"): pregunta, NO fija objetivo
    tq = t.replace("si es posible", "")
    if re.search(r"\b(podemos|puedo|podria|se puede|es posible|llegamos a|vamos a poder)\b", tq) and \
            re.search(r"\b(ganar|victoria|podio|acabar|terminar|quedar|llegar)\b", t) and not has("pasar"):
        n = 1 if has("ganar", "victoria") else 3 if has("podio") else position_in(t)
        if n:
            return mk("viabilidad", pos=n)
    if re.search(r"\b(quiero|vamos a|intenta|a por)\b.*\b(podio|ganar|victoria)\b", t) and not has("pasar"):
        return mk("objetivo_pos", pos=1 if has("ganar", "victoria") else 3)
    if has("mejor posicion") or (has("objetivo") and re.search(r"\b(propon\w*|suger\w*|recomiend\w*|cual (seria|es) (un |el )?objetivo)\b", t)):
        return mk("sugerir_objetivo")
    if has("neumatico", "neumaticos", "gomas", "goma") and not comp_w(words):
        if d1:
            return mk("rival_neumaticos")
        if has("delante", "adelante", "detras", "atras"):
            return mk("rival_neumaticos", topic="detras" if has("detras", "atras") else "delante")
        if re.search(r"\b(otros?|demas|rivales|resto)\b", t):
            return mk("rivales_neumaticos")
    if re.search(r"\b(resumen|resumeme)\b", t) or re.search(r"\bcomo (fue|ha ido|va) (la|esta) carrera\b", t) \
            or re.search(r"\bcomo (lo )?(hice|hicimos)\b", t):
        return mk("resumen")
    # --- sesión, tiempos, pilotos y «¿alguna actualización?»
    if re.search(r"\b(en que sesion|que sesion|sesion estamos|en que (parte|fase)|estamos en (entrenamiento|entrenamientos|clasificacion|carrera|practica|libres))\b", t):
        return mk("sesion")
    if re.search(r"\b(mejor vuelta|vuelta rapida|mi tiempo|que tiempo (hice|tengo|llevo)|ultima vuelta|tiempo cronometrado|cual es mi vuelta|tiempo de (mi )?vuelta)\b", t) \
            and not re.search(r"\b(como|mejorar|donde)\b", t):
        return mk("mejor_vuelta", topic="sesion" if has("sesion", "carrera", "practica", "entrenamiento", "clasificacion", "general", "mundo") else None)
    if (d1 and re.search(r"\b(informacion|info|datos|donde esta|que tal|como esta|dime de|hablame de|cuentame de)\b", t)) or \
            (position_in(t) and re.search(r"^(quien|como|que tal|dime|y)\b", t) and re.search(r"\b(va|esta|es|clasificado|lleva)\b", t)
             and not re.search(r"\b(quiero|acabar|terminar|objetivo|meta|pasar)\b", t)):
        return mk("piloto", pos=None if d1 else position_in(t))
    if re.search(r"\b(actualizacion|actualizaciones|novedad|novedades|alguna noticia|algo nuevo|como (va|vamos|estamos|esta) (todo|la cosa|la sesion))\b", t):
        return mk("actualizacion")
    # --- vueltas medidas (clasificación y entrenos, también en carrera): análisis, ERS, comparación con otro coche, estimación
    if re.search(r"\b(comparame|compara|comparar|comparado|compararme)\b", t):
        target = ("primero" if has("primero", "p1", "lider", "pole") else "companero" if re.search(r"\bcompa\w*ero\b", t)
                  else "delante" if has("delante", "adelante") else None)
        if d1 or target:
            return mk("compara_vuelta", topic=target)
    if re.search(r"\b(estimas|estimacion|estimar|estimo|prediccion|calculas)\b", t) or \
            re.search(r"\bcomo (va|voy|fue|fueron|van)\b.*\b(practica|practicas|entreno|entrenos|entrenamiento|entrenamientos)\b", t):
        return mk("estimacion")
    if re.search(r"\b(ers|bateria|energia|despliegue)\b", t) and re.search(r"\b(uso|usar|usando|despliego|desplegar|gasto|gastando|mejor|donde|como)\b", t) \
            and re.search(r"\b(vuelta|curva|curvas|recta|rectas|usar|uso|despliego|desplegar|gasto|gastando)\b", t):
        return mk("ers_vuelta")
    if re.search(r"\b(donde|en que curvas?|que curvas?)\b.*\b(pierdo|mejorar|mejoro|gano)\b", t) or \
            re.search(r"\b(mi vuelta|la vuelta)\b.*\b(como|analiza|analisis|mejorar)\b|\b(como|analiza)\b.*\bmi vuelta\b", t):
        return mk("analisis_vuelta")
    # --- compañero de equipo
    if re.search(r"\bcompa\w*ero\b|\bcompanera\b|\bmi mate\b|\bmi equipo\b", t):
        return mk("companero")
    # --- ¿qué compuesto pongo? (comparar con datos de los demás)
    comps = tuple(dict.fromkeys(COMP[w] for w in words if w in COMP))
    tyre_w = has("neumatico", "neumaticos", "gomas", "goma", "ruedas", "compuesto", "compuestos")
    about_others = re.search(r"\b(otros|demas|rivales|resto)\b", t)
    if not about_others and (comps or tyre_w):
        excl = re.search(r"\b(que no sea|excepto|menos|sin)\b", t)
        if excl and comps and (tyre_w or has("mejor")):
            return mk("elegir_compuesto", comps=tuple(c for c in (16, 17, 18) if c not in comps))
        if len(comps) >= 2 and re.search(r"\b(mejor|o|conviene|prefieres)\b", t):
            return mk("elegir_compuesto", comps=comps)
        if (has("mejor", "conviene", "recomiendas") and tyre_w and not comps) or \
                re.search(r"\bque (neumatico|neumaticos|gomas|compuesto)\b.*\b(pongo|monto|elijo|uso|ponemos|montamos)\b", t):
            return mk("elegir_compuesto")
    # --- compuestos concretos: lo que llevan los demás, o parar ahora a ese compuesto
    comp = next((COMP[w] for w in words if w in COMP), None)
    if comp and (has("neumatico", "neumaticos", "gomas", "goma", "ruedas", "compuesto") or
                 re.search(r"\b(quiero|pongo|monto|montar|cambio a|cambiar a|vamos con|con el|un)\s+(el |un |los |las )?(duro|medio|blando|dura|blanda)s?\b", t)):
        if re.search(r"\b(otros|demas|rivales|resto)\b", t):
            return mk("compuesto_rivales", compound=comp)
        return mk("compuesto", compound=comp)
    # --- rivales: "me ha pasado", "dejo pasar", comparaciones de velocidad
    if re.search(r"\bme (ha |han )?(pasad\w*|paso|adelant\w*)\b", t) and not has("quiero", "puedo"):
        return mk("hueco") if (d1 or amb) else mk("posicion")
    if re.search(r"\b(dejo|dejar|dejamos|dejale|le dejo) pasar\b", t) and (d1 or amb):
        return mk("dejar_pasar")
    if (d1 or amb) and (has("recta", "rectas", "curva", "curvas", "rapido", "rapida", "velocidad", "sectores") or
                        (has("saca", "sacando") and not has("cuanto"))):
        return mk("vs_rival")
    # --- objetivos
    if has("olvida el objetivo", "sin objetivo", "quita el objetivo", "borra el objetivo"):
        return mk("objetivo_borrar")
    if re.search(r"\b(necesito|quiero|tengo que|hay que|vamos a|intenta)\b.*\bpasar\b", t) and (d1 or amb):
        return mk("objetivo_pasar")
    if re.search(r"\b(quiero|quedar|acabar|terminar|objetivo|meta)\b", t) and position_in(t) and \
            re.search(r"\b(acabar|terminar|quedar|objetivo|meta|llegar)\b", t):
        return mk("objetivo_pos", pos=position_in(t))
    if has("objetivo", "vamos bien", "vamos bien encaminados", "llegamos") and not has("combustible"):
        return mk("objetivo_estado")
    # --- el piloto actúa o se queja (antes que las consultas generales)
    if re.search(r"\b(overcut|[uao]ver ?c[ua]t|alargar|alargo|alarga|alargamos|parar despues|paro despues|esperar para parar|aguantar)\b", t) \
            and not has("undercut"):
        return mk("overcut")
    if re.search(r"\b(parar antes|paro antes|adelantar la parada|adelanto la parada|la alternativa|la arriesgada|prefiero la alternativa)\b", t):
        return mk("alternativa")
    if re.search(r"\b(quiero|voy a|vamos a|puedo|paro|parar|entro|entrar)\b.*\b(parar|boxes|box|pit|paro|entrar)\b", t) \
            and not has("undercut", "overcut", "conviene") or has("parar ya", "paro ya", "boxes ya", "boxes ahora", "parar ahora"):
        return mk("parar_ya")
    if has("undercut", "overcut") or re.search(r"\b[uao]nder ?c[ua]t\b", t) or re.search(r"\bme conviene (parar|entrar)\b", t):
        return mk("undercut")
    if re.search(r"(no me siento (comodo|bien)|no estoy (comodo|bien)|incomodo)", t):
        return mk("gomas_mal") if has("gomas", "neumaticos", "neumatico", "ruedas") else mk("incomodo")
    if has("sin agarre", "no tengo agarre", "patina", "patinan", "no agarra", "no agarran") or \
            re.search(r"\b(gomas?|neumaticos?|ruedas?)\b.*\b(muert\w*|gastad\w*|rot\w*|acabad\w*|fundid\w*)\b", t):
        return mk("gomas_mal")
    if re.search(r"\b(no se que hacer|que hago|que hacemos|ayudame|cambiamos de estrategia|cambiamos la estrategia|"
                 r"cambiar de estrategia|que estrategia|cual es el plan|que plan|cuando (paro|paramos|entro|entramos|boxes)|"
                 r"en que vuelta (paro|paramos|entro|entramos))\b", t) or has("estrategia", "estrategias"):
        if drv and not has("estrategia", "estrategias", "parar", "paro", "boxes", "neumatico", "neumaticos", "gomas", "pit", "adelantar", "undercut"):
            return None                                   # «Ocon está molesto conmigo, ¿qué hago?»: no es una pregunta de estrategia
        return mk("estrategia")
    # --- consultas
    if has("ahorrar", "ahorro") or re.search(r"donde (levanto|ahorro)", t):
        return mk("ahorro")
    if re.search(r"por ?que (estoy|estamos|pierdo|perdemos|voy|vamos).*(lento|tiempo|ritmo|pierdo)", t) or \
            has("pierdo tiempo", "perdemos tiempo", "perdiendo tiempo", "voy lento", "estoy lento"):
        return mk("por_que_pierdo")
    if has("hueco", "distancia", "diferencia") or re.search(r"como voy (respecto|con|contra|frente)\b", t) \
            or re.search(r"a cuanto esta", t) or (d1 and has("como voy", "como estoy", "cuanto")):
        if has("delante", "adelante") and not d1:
            return mk("hueco_delante")
        if has("detras", "atras") and not d1:
            return mk("hueco_detras")
        if d1 or amb:
            return mk("hueco")
        if has("ritmo"):
            return mk("ritmo")
        return mk("hueco")
    if has("delante", "adelante") and not has("posicion"):
        return mk("hueco_delante")
    if has("detras", "atras"):
        return mk("hueco_detras")
    if has("ritmo") or has("tiempos de vuelta", "tiempo de vuelta", "mis vueltas"):
        return mk("ritmo")
    if has("neumatico", "neumaticos", "gomas", "goma", "desgaste", "aguantan", "ruedas"):
        return mk("neumaticos")
    if has("combustible", "gasolina", "fuel"):
        return mk("combustible")
    if has("ers", "bateria", "energia", "despliegue"):
        return mk("ers")
    if has("dano", "danos", "aleron", "alerones", "alero", "roto", "rota"):
        return mk("danos")
    if has("lluvia", "llover", "llueve", "clima", "mojado", "seca") or re.search(r"\bllov\w*\b", t):
        return mk("clima")
    if re.search(r"(cuantas vueltas|cuanto).*(aguant\w*|duran?|quedan? a|faltan? a|le quedan|le faltan).*(neumatic\w*|gomas?|ruedas?)|"
                 r"(neumatic\w*|gomas?|ruedas?).*(cuantas vueltas|cuanto).*(aguant\w*|duran?|quedan|faltan)|"
                 r"cuantas vueltas (mas )?(aguant\w*|le (quedan|faltan))", t):
        return mk("neumaticos")                          # vida restante del neumático (no es «vueltas de carrera»)
    m = re.search(r"(?:carrera|gp|race)\s+(?:es\s+|son\s+|dura\s+)?(?:de|a)?\s*(\d{1,2})\s+vueltas|(\d{1,2})\s+vueltas\s+(?:de\s+)?(?:la\s+)?carrera", t)
    if m:
        return mk("plan_carrera", topic=m.group(1) or m.group(2))            # «carrera de 18 vueltas»: anota la distancia y propone plan
    if re.search(r"(plan|estrategia) (de|para) (la )?carrera|como (afronto|planteo|corro) la carrera|que plan (tienes|propones) para la carrera", t):
        return mk("plan_carrera")
    if has("finalizar la sesion", "finaliza la sesion", "terminar la sesion", "termina la sesion", "cerrar la sesion",
           "cerrar el programa", "cierra el programa", "apagar el programa", "apaga el programa"):
        return mk("cerrar")
    if re.search(r"cuantas vueltas|vueltas (quedan|faltan)|cuanto falta|cuanto dura la carrera", t):
        return mk("vueltas")
    if re.search(r"\b(voy a|toca|debo|tengo que)\b.*\b(pits?|box\w*|par\w+|entr\w+)\b|\b(pits?|boxes|paro|entro|parar)\b", t) \
            and "?" in text and len(t.split()) <= 5 and not has("pasar", "posicion", "puesto"):
        return mk("parar_ya")                            # «¿paro?», «¿voy a pits?», «¿entro?»: pregunta si entrar a boxes (sin LLM)
    if (has("posicion", "puesto") or (has("voy") and not re.search(r"\bvoy a\b", t))) and not has("pasar"):
        return mk("posicion")
    # afirmaciones del piloto sobre lo que hace («empiezo vuelta lanzada», «entro a boxes»): se acusa recibo, sin inventar nada
    if "?" not in text and re.match(r"^(ok |vale |bien |listo |bueno )?(voy a|empiezo|inicio|termino|finalizo|me preparo|salgo|entro|entran?|entren|entrando|regreso|vuelvo|acabo|comienzo)\b", t):
        return mk("ack")
    return None


SYS_CLASSIFY = (
    "Clasificas la frase de un piloto de F1 en UNA intención de esta lista: " + ", ".join(INTENTS) + ". "
    "Responde SOLO JSON {\"intencion\":\"<una de la lista o ninguna>\",\"piloto\":\"<apellido mencionado o null>\","
    "\"posicion\":<número o null>}. Si no estás seguro, responde {\"intencion\":\"ninguna\"}. No inventes pilotos."
)


def classify_llm(client, tr, text):
    if client is None or len(text.split()) > MAX_WORDS:
        return None
    try:
        raw = client.chat(SYS_CLASSIFY, json.dumps({"frase": text}, ensure_ascii=False))
    except L.LLMError:
        return None
    obj = L.first_json(raw)
    if not isinstance(obj, dict) or obj.get("intencion") not in LLM_SAFE:
        return None
    drv = tr.find_driver(obj["piloto"]) if isinstance(obj.get("piloto"), str) and obj["piloto"] else []
    pos = obj.get("posicion") if isinstance(obj.get("posicion"), int) and 1 <= obj["posicion"] <= 22 else None
    return Intent(obj["intencion"], driver=drv[0] if len(drv) == 1 else None, pos=pos,
                  ambiguous=drv if len(drv) > 1 else [], source="llm")


# ---------------------------------------------------------------------------------------------- asistente
class Assistant(SessionAnswers):
    def __init__(self, eng):
        self.eng = eng
        self.awaiting = None
        self.misses = 0
        self.last = {"intent": None, "source": None, "understood": False}   # lo último que entendió (auditoría y radio)
        c = eng.cfg.get("llm", {})
        from brain import Brain
        self.brain = Brain(eng, eng.client, eng.cfg) if (eng.client is not None and c.get("primary")) else None

    def classify(self, text):
        """Intent o None: reglas primero; si fallan, el LLM solo clasifica."""
        return parse_rules(self.eng.tr, text) or classify_llm(self.eng.client, self.eng.tr, text)

    # -------------------------------------------------------------------------------- flujo: preparar -> ejecutar -> redactar
    def prepare(self, text):
        """Decide QUÉ hacer con la frase (puede llamar al LLM; no toca el estado, así no bloquea la telemetría)."""
        from brain import Plan
        tr = self.eng.tr
        intent = None
        short = len(text.split()) <= 6                     # una pregunta abierta solo se responde con una frase corta, y dura un turno
        if isinstance(self.awaiting, tuple) and self.awaiting[0] == "avisos":
            lvl, self.awaiting = self.awaiting[1], None
            topic = next((TOPIC_WORDS[w] for w in re.findall(r"\w+", tr.plain(text)) if w in TOPIC_WORDS), None) if short else None
            intent = Intent("avisos", topic=topic, level=lvl) if topic else None
        elif isinstance(self.awaiting, tuple) and self.awaiting[0] == "confirm":
            pending, self.awaiting = self.awaiting[1], None
            r = parse_rules(tr, text)
            if r is not None and r.name == "si":
                return Plan([pending], source="reglas")
            if r is not None and r.name == "no":
                return Plan(say="De acuerdo, lo dejo como estaba.", source="reglas")
        elif self.awaiting == "incomodo":
            self.awaiting = None
            intent = self._follow_incomodo(text) if short else None
        if intent:
            return Plan([intent], source="reglas")
        rule = parse_rules(tr, text)
        brain = self.brain
        if brain is not None and brain.available():
            if rule is not None and len(text.split()) <= 4 and (rule.name in FAST):   # órdenes de control cortas: sin esperar al modelo
                return Plan([rule], source="reglas")
            if rule is not None and rule.name == "avisos" and rule.topic:            # «más/menos avisos de X» explícito: preciso y sin espera
                return Plan([rule], source="reglas")
            if rule is not None and rule.name == "cerrar":
                return Plan([rule], source="reglas")
            plan = brain.route(text)
            if plan is not None:
                if rule is not None and rule.name == "neumaticos" and plan.tools == ["estrategia"]:
                    return Plan([rule], source="reglas")         # «cuántas vueltas aguanta el neumático» no es pedir un plan
                if rule is not None and rule.name == "elegir_compuesto" and plan.tools == ["estrategia"]:
                    return Plan([rule], source="reglas")         # «qué neumático pongo» es elegir compuesto, no el plan completo
                if rule is not None and rule.name == "rival_neumaticos" and plan.tools == ["hueco"]:
                    return Plan([rule], source="reglas")         # «cómo van los neumáticos del de atrás» no es el hueco
                if plan.dropped and not plan.say and not plan.unclear and rule is not None:
                    return Plan([rule], source="reglas")         # el modelo no encontró herramienta (o se descartó): red de seguridad
                if not plan.intents and plan.confirm is None and not plan.unclear and rule is not None and rule.name in RULE_OVER_CHAT:
                    return Plan([rule], source="reglas")         # el modelo charló ante una pregunta de datos que las reglas sí reconocen
                return plan
        elif brain is None and rule is None:
            rule = classify_llm(self.eng.client, tr, text)       # modo clásico: el LLM solo clasifica
        return Plan([rule], source=rule.source) if rule else Plan(understood=False)

    def execute(self, plan, text):
        """Ejecuta el plan con los datos del juego (con el candado del ingeniero) y devuelve el texto en bruto."""
        from brain import UNKNOWN
        eng, tr = self.eng, self.eng.tr
        self.last = {"intent": None, "source": plan.source, "understood": False, "tools": plan.tools, "llm_s": round(plan.llm_s, 2)}
        if plan.confirm is not None:
            self.awaiting = ("confirm", plan.confirm)
        if plan.intents:
            self.misses = 0
            replies = []
            for k, intent in enumerate(plan.intents):
                r = self._run(intent, text)
                if r:
                    replies.append(r)
                if k == 0:
                    self.last.update(intent=intent.name, understood=True)
            reply = " ".join(replies)
            eng.dialog.last_said = reply
            return reply
        if plan.say:                                             # charla, acuse de recibo o pregunta de confirmación
            self.misses = 0
            self.last.update(intent="confirmar" if plan.confirm is not None else "charla", understood=True)
            eng._log({"type": "heard", "t": round(tr.t, 1), "text": text, "intent": self.last["intent"], "source": plan.source})
            return plan.say
        self.misses += 1
        eng._log({"type": "heard", "t": round(tr.t, 1), "text": text, "intent": None, "source": plan.source})
        if plan.source == "llm" and not eng.dialog.pending and not plan.unclear:   # entendió la frase pero no hay herramienta: se dice, no es un error
            self.last.update(intent="sin_dato", understood=True)
            return UNKNOWN
        return NO_UNDERSTAND

    def finish(self, plan, text, reply):
        """Redacta con el LLM las respuestas informativas (validando cifras y nombres) y recuerda el intercambio."""
        brain = self.brain
        if brain is not None and self.last.get("understood"):
            if plan.source == "llm" and plan.tools:
                reply = brain.compose(text, plan.tools, reply)
                self.eng.dialog.last_said = reply
            if plan.tools or plan.source != "llm":
                brain.remember(text, reply)                        # la charla no se recuerda: el modelo repetía sus propias frases
        return reply

    def handle(self, text):
        plan = self.prepare(text)
        with self.eng.lock:
            reply = self.execute(plan, text)
        return self.finish(plan, text, reply)

    def _run(self, intent, text):
        """Una intención -> respuesta del programa (o pregunta de aclaración)."""
        eng, tr = self.eng, self.eng.tr
        eng._log({"type": "heard", "t": round(tr.t, 1), "text": text, "intent": intent.name, "source": intent.source,
                  "driver": intent.driver[1] if intent.driver else None})
        if intent.ambiguous:
            return "¿A cuál te refieres: " + " o ".join(n for _, n in intent.ambiguous) + "?"
        if intent.name in NEEDS_DRIVER and not intent.driver:
            return "¿De qué piloto?"
        needs_data = intent.name not in ("si", "no", "explica", "repite", "silencio", "reanuda", "objetivo_borrar", "sesion", "ack", "cerrar", "plan_carrera", "animo")
        if needs_data and (tr.player is None or tr.player >= len(tr.lap_all) or not tr.lap_all[tr.player]):
            if getattr(tr, "epoch", 1) > 1:
                return "La sesión anterior ya terminó; espero los datos de la nueva."
            return "Todavía no tengo datos de la sesión."
        reply = self.non_race_reply(intent)
        fn = getattr(self, "t_" + intent.name, None)
        if reply is None:
            reply = fn(intent) if fn else NO_UNDERSTAND
        if self.brain is not None:                       # para «¿y él?»: el último piloto del que se habló
            ahead, behind = tr.neighbours() if intent.name in ("hueco_delante", "hueco_detras") else (None, None)
            near = ahead if intent.name == "hueco_delante" else behind
            if intent.driver:
                self.brain.last_driver = intent.driver
            elif near:
                self.brain.last_driver = (near[0], tr.name(near[0]))
        pt = tr.plain(text)
        if intent.name in ("estrategia", "estimacion", "plan_carrera", "viabilidad", "compuesto") and re.search(r"\bsi o no\b", pt) \
                and not reply.startswith(("Sí", "No ", "No.")):
            first = tr.plain(re.split(r"(?<=[.!?])\s", reply, maxsplit=1)[0])
            neg = re.search(r"\b(aun no|todavia no|no tengo|no puedo|sin datos|no hay|no veo)\b", first)
            reply = ("No. " if neg else "Sí. ") + reply                       # «¿…? ¿Sí o no?»: primero la respuesta corta
        return reply

    def _follow_incomodo(self, text):
        t = self.eng.tr.plain(text)
        if re.search(r"\b(gomas|neumaticos|neumatico|ruedas)\b", t):
            return Intent("gomas_mal")
        if re.search(r"\b(ritmo|lento|tiempo)\b", t):
            return Intent("por_que_pierdo")
        if re.search(r"\b(coche|auto|frenos|balance|alerones?|dano)\b", t):
            return Intent("danos")
        return None

    # ------------------------------------------------------------------ respuestas a la propuesta
    def t_si(self, i):
        return self.eng.dialog.accept(self.eng.tr.cur_lap)

    def t_no(self, i):
        return self.eng.dialog.decline(self.eng.tr.cur_lap)

    def t_explica(self, i):
        return self.eng.dialog.explain()

    def t_repite(self, i):
        return self.eng.dialog.last_said or "Todavía no he dicho nada."

    def t_silencio(self, i):
        self.eng.dialog.muted_until = 10 ** 6
        return "Silencio. Solo te avisaré de lo crítico. Di 'reanuda' para volver."

    def t_reanuda(self, i):
        self.eng.dialog.muted_until = -1
        return "Vuelvo a avisarte."

    # ------------------------------------------------------------------ consultas
    def t_posicion(self, i):
        if self._kind() != "race":
            return self.nr_position()
        s = self.eng.tr.summary()
        return f"Vas P{s['posicion']} en la vuelta {s['vuelta']} de {s['vueltas_totales']}." if s else NO_UNDERSTAND

    def t_vueltas(self, i):
        ll = self.eng.tr.laps_left()
        return f"Quedan {ll:.0f} vueltas." if ll else "No tengo las vueltas totales."

    def _hueco(self, idx, name):
        if self._kind() != "race":
            return self.nr_gap(idx, name)
        tr = self.eng.tr
        gap = tr.rel_gap(idx)
        if gap is None:
            return f"No tengo el hueco con {name} ahora."
        txt = f"{name} va {'delante' if gap < 0 else 'detrás'}, a {abs(gap):.1f} s."
        tend = tr.gap_trend(idx) if tr.cur_lap >= 3 else None      # en las primeras vueltas el hueco aún viene de la parrilla
        if tend is not None and 0.1 <= abs(tend) <= 4.0:
            if gap < 0:
                txt += f" En las últimas 2 vueltas has {'recortado' if tend > 0 else 'perdido'} {abs(tend):.1f} s."
            else:
                txt += f" En las últimas 2 vueltas {'te ha recortado' if tend > 0 else 'te has alejado'} {abs(tend):.1f} s."
        pr, pm = tr.pace(idx), tr.pace(tr.player)
        if pr and pm and abs(pm - pr) >= 0.1:
            txt += f" Su ritmo es {abs(pm - pr):.1f} s {'mejor' if pm > pr else 'peor'} que el tuyo."
        return txt

    def t_hueco(self, i):
        if i.driver:
            return self._hueco(*i.driver)
        a, b = self.eng.tr.neighbours()
        parts = []
        if a:
            parts.append(self._hueco(a[0], self.eng.tr.name(a[0])))
        if b:
            parts.append(self._hueco(b[0], self.eng.tr.name(b[0])))
        return " ".join(parts) or "No tengo huecos ahora."

    def t_hueco_delante(self, i):
        if self._kind() != "race":
            return self.nr_neighbour(-1)
        a, _ = self.eng.tr.neighbours()
        return self._hueco(a[0], self.eng.tr.name(a[0])) if a else "No hay nadie delante."

    def t_hueco_detras(self, i):
        if self._kind() != "race":
            return self.nr_neighbour(+1)
        _, b = self.eng.tr.neighbours()
        return self._hueco(b[0], self.eng.tr.name(b[0])) if b else "No hay nadie detrás."

    def t_neumaticos(self, i):
        tr = self.eng.tr
        sa, dm = tr.sta_all[tr.player], tr.dmg_all[tr.player]
        if not sa or not dm:
            return "No tengo datos de neumáticos todavía."
        w = dm["tyre_wear_pct"]
        k = max(range(4), key=lambda x: w[x])
        txt = (f"Neumático {S.NAME.get(sa['visual_compound'], '?')}, {sa['tyre_age_laps']} vueltas de uso, "
               f"desgaste máximo {w[k]:.0f}% en el {WHEELS[k]}.")
        rate = tr.stint_rate()
        limit = tr.params.wear_limit - tr.limit_delta
        ll = tr.laps_left()
        if rate:
            rem = max((limit - w[k]) / rate, 0)
            txt += f" Al ritmo actual llegan al {limit:.0f}% en unas {rem:.0f} vueltas" + (f" y quedan {ll:.0f}." if ll else ".")
        sets = tr.sets.get(tr.player)
        if sets:
            f = sets["sets"][sets["fitted_idx"]]
            if f["life_span"]:
                txt += f" El juego estima {f['life_span']} vueltas de vida."
        return txt

    def t_combustible(self, i):
        tr = self.eng.tr
        sa = tr.sta_all[tr.player]
        if not sa:
            return "No tengo datos de combustible."
        m = tr.fuel_margin()
        if m is None:
            return f"Combustible {sa['fuel_kg']:.1f} kg."
        return f"Combustible {sa['fuel_kg']:.1f} kg: " + (f"te sobra para {m:.1f} vueltas más." if m >= 0 else f"te faltan {-m:.1f} vueltas: ahorra.")

    def t_ahorro(self, i):
        tr = self.eng.tr
        m = tr.fuel_margin()
        zones = tr.fz.suggest()
        if not zones:
            return "Todavía no tengo suficientes vueltas para saber dónde ahorrar combustible."
        head = "" if m is None or m < 0 else f"Ahora te sobra combustible para {m:.1f} vueltas más, pero si quieres ahorrar: "
        return head + " ".join(zones)

    def t_ers(self, i):
        s = self.eng.tr.summary()
        return f"ERS al {s['ers_pct']}%." if "ers_pct" in s else "No tengo datos de ERS."

    def t_danos(self, i):
        tr = self.eng.tr
        dm = tr.dmg_all[tr.player]
        if not dm:
            return "No tengo datos de daños."
        from tactics import PARTS
        bad = [f"{n.split(' ', 1)[1]} {dm[k]}%" for k, n in PARTS.items() if dm[k] >= 5]     # sin el artículo: «alerón trasero 12%»
        return ("Daños: " + ", ".join(bad) + ".") if bad else "Sin daños relevantes."

    def t_clima(self, i):
        from answers import forecast_text
        return forecast_text(self.eng.tr.ses or {})

    def t_ritmo(self, i):
        tr = self.eng.tr
        h = tr.hist.get(tr.player)
        if not h or len(h["laps"]) < 2:
            return "Aún no tengo suficientes vueltas."
        laps = [l["ms"] / 1000 for l in h["laps"] if l["ms"] > 0]
        last = laps[-3:]
        txt = "Tus últimas vueltas: " + ", ".join(D.fmt_lap(x) for x in last) + f"; tu mejor {D.fmt_lap(min(laps))}."
        p = tr.pace(tr.player)
        return txt + (f" Ritmo reciente {D.fmt_lap(p)}." if p else "")

    def t_por_que_pierdo(self, i):
        tr = self.eng.tr
        h, dm = tr.hist.get(tr.player), tr.dmg_all[tr.player]
        if not h or len(h["laps"]) < 3:
            return "No tengo suficientes vueltas para compararte."
        laps = [l for l in h["laps"][1:] if l["ms"] > 0]
        if len(laps) < 2:
            return "No tengo suficientes vueltas para compararte."
        best = min(laps, key=lambda l: l["ms"])
        last = laps[-1]
        d = (last["ms"] - best["ms"]) / 1000
        if d < 0.15:
            return "No estás perdiendo tiempo: tu última vuelta está en tu ritmo."
        reasons = []
        sec = [("sector 1", last["s1"] - best["s1"]), ("sector 2", last["s2"] - best["s2"]), ("sector 3", last["s3"] - best["s3"])]
        sec = [(n, x / 1000) for n, x in sec if x > 0]
        if sec:
            n, x = max(sec, key=lambda z: z[1])
            reasons.append((x, f"la mayor pérdida está en el {n} (+{x:.2f} s)"))
        if dm:
            w = max(dm["tyre_wear_pct"])
            loss = tr.params.pace_loss_per_wear * w + S.cliff(tr.params, w)
            if loss >= 0.2:
                reasons.append((loss, f"los neumáticos con {w:.0f}% de desgaste cuestan ~{loss:.1f} s por vuelta"))
            aero = max(dm["front_left_wing"], dm["front_right_wing"])
            if aero >= 5:
                reasons.append((aero * tr.params.aero_loss_per_pct, f"el alerón delantero dañado ({aero}%)"))
        a, _ = tr.neighbours()
        if a and 0.05 < a[1] <= 1.5:
            pa, pm = tr.pace(a[0]), tr.pace(tr.player)
            if pa and pm and pa - pm >= 0.2:
                reasons.append((pa - pm, f"vas detrás de {tr.name(a[0])}, que es {pa - pm:.1f} s más lento"))
        reasons.sort(reverse=True)
        return f"Pierdes {d:.1f} s por vuelta frente a tu mejor. " + "; ".join(r for _, r in reasons[:3]).capitalize() + "."

    # ------------------------------------------------------------------ estrategia bajo demanda
    def _eval(self, include=()):
        res = self.eng.evaluate_now(include)
        if res[0] is None:
            return None, res[1]
        return res, None

    def _propose(self, rep, opt, sit, source="piloto", reason="a petición del piloto"):
        eng = self.eng
        gain = (opt.utility - rep.baseline.utility) if rep.baseline else 0.0
        p = eng.dialog.propose(opt, opt.phrase, sit.lap, eng.tr.t, source, reason, gain)
        eng.plan_hint = opt.stops[0] if opt.stops else None
        return p

    # ------------------------------------------------------------------ preguntas hipotéticas y de situación (feature IA, 11 nov)
    def t_que_pasa_si(self, i):
        """«Si paro ahora / en la vuelta N (a X), ¿cómo acabo?»: la simulación ya evaluó esa opción; no se propone nada."""
        res, err = self._eval(self.eng.goals.include())
        if err:
            return f"Ahora no puedo simularlo: {err}."
        rep, sit, me, rivals, info = res
        lap = sit.lap if i.pos is None else i.pos
        cands = [o for o in rep.options if o.stops and abs(o.stops[0][0] - lap) <= 1 and (i.compound is None or o.stops[0][1] == i.compound)]
        if i.topic == "seguir":
            cands = [o for o in rep.options if o.kind == "STAY"] or [o for o in rep.options if o.stops and o.stops[0][0] > sit.lap + 3]
        if not cands:
            return f"Esa opción no está entre las que he simulado (parar en la vuelta {lap}). Lo mejor ahora: {rep.best.desc.lower()}."
        o = max(cands, key=lambda x: x.utility)
        when = "si sigues fuera" if o.kind == "STAY" else ("si paras ahora" if o.stops[0][0] == sit.lap else f"si paras en la vuelta {o.stops[0][0]}")
        txt = f"{when[0].upper() + when[1:]} a {S.NAME[o.stops[0][1]]}" if o.stops else when[0].upper() + when[1:]
        txt += f": terminarías hacia P{round(o.e_pos)}."
        if i.driver and i.driver[1] in o.p_beat:
            txt += f" Por delante de {i.driver[1]} en el {S.pct(o.p_beat[i.driver[1]])}% de las simulaciones (con lo que suele hacer él)."
        if rep.best is not o:
            txt += f" Lo mejor según la simulación: {rep.best.desc.lower()} (hacia P{round(rep.best.e_pos)})."
        else:
            txt += " Es también lo mejor que veo."
        return txt

    def _target(self, i):
        tr = self.eng.tr
        if i.driver:
            return i.driver[0]
        ahead, behind = tr.neighbours()
        pick = behind if i.topic == "detras" else ahead
        return pick[0] if pick else None

    def t_ritmo_rival(self, i):
        import coach
        tr = self.eng.tr
        car = self._target(i)
        if car is None:
            return "No tengo a nadie cerca para comparar el ritmo."
        gap = coach.pace_gap(tr, car)
        sa = tr.sta_all[car]
        tyre = f", con {S.NAME.get(sa['visual_compound'], '?')} de {sa['tyre_age_laps']} vueltas" if sa else ""
        if gap is None:
            return f"Aún no tengo 2 vueltas limpias tuyas y de {tr.name(car)} para comparar el ritmo{tyre}."
        if abs(gap) < 0.05:
            return f"{tr.name(car)} lleva tu mismo ritmo en las últimas vueltas limpias{tyre}."
        return (f"{tr.name(car)} va {abs(gap):.1f} s por vuelta {'más lento' if gap > 0 else 'más rápido'} que tú "
                f"en las últimas vueltas limpias{tyre}.")

    def t_alcance(self, i):
        """«¿En qué vuelta me alcanza Tsunoda?»: con el hueco de ahora y cuánto ha cambiado en las últimas vueltas (informe 11 nov:
        respondía «no tengo ese dato» aunque el propio ingeniero acababa de decir «Tsunoda recorta 0.6 s por vuelta»)."""
        tr = self.eng.tr
        car = i.driver[0] if i.driver else None
        if car is None:
            ahead, behind = tr.neighbours()
            pick = ahead if i.topic == "delante" else behind
            car = pick[0] if pick else None
        now = tr.rel_gap(car) if car is not None else None
        if now is None:
            return "No tengo el hueco con ese coche ahora."
        pits = {r["lap"] + d for r in tr.pit_log for d in (-1, 0)}               # vueltas de entrada y salida de tu parada
        rec = [l for l in sorted(tr.gaps_by_lap) if car in tr.gaps_by_lap[l] and tr.player in tr.gaps_by_lap[l] and l not in pits]
        rec = [l for l in rec if l >= rec[-1] - 2] if rec else []                # las 3 últimas vueltas cerradas, como mucho
        if len(rec) < 2:
            return f"{tr.name(car)} está a {abs(now):.1f} s; necesito una vuelta más para ver si se acerca."
        gap_at = lambda l: abs(tr.gaps_by_lap[l][car] - tr.gaps_by_lap[l][tr.player])
        rate = (gap_at(rec[0]) - gap_at(rec[-1])) / (rec[-1] - rec[0])           # s por vuelta que se cierra el hueco
        name, gap = tr.name(car), abs(now)
        if rate < 0.05:
            return f"{name} no se está acercando: está a {gap:.1f} s y el hueco se mantiene o crece."
        n = gap / rate
        left = tr.laps_left() or 99
        who = f"{name} te alcanza" if now > 0 else f"alcanzas a {name}"
        if n > left:
            return f"A este ritmo ({rate:.1f} s por vuelta) no hay alcance antes del final: está a {gap:.1f} s y quedan {left:.0f} vueltas."
        laps_txt = lambda x: "en la próxima vuelta" if x < 1.5 else f"en unas {round(x)} vueltas"
        drs = (gap - 1.0) / rate if gap > 1.0 else 0
        txt = f"A este ritmo ({rate:.1f} s por vuelta), {who} {laps_txt(n)}"
        if n >= 1.5:
            txt += f" (hacia la vuelta {tr.cur_lap + round(n)})"
        if drs >= 0.5 and round(drs) < round(n):
            txt += f"; en DRS, a menos de 1 s, {laps_txt(drs)}"
        return txt + ". Si nada cambia (tráfico, gomas, paradas)."

    def t_estado_coche(self, i):
        """Todo el coche en una frase corta: daños, gomas, combustible y batería (lo que el piloto entiende por «el coche»)."""
        first = lambda txt: re.split(r"(?<=[.!?])\s", txt, maxsplit=1)[0]
        parts = [first(self.t_danos(i)), first(self.t_neumaticos(i)), self.t_combustible(i), first(self.t_ers(i))]
        return " ".join(p for p in parts if p)

    def t_ventana_parada(self, i):
        res, err = self._eval(self.eng.goals.include())
        if err:
            return f"Ahora no puedo calcular la ventana: {err}."
        rep, sit, me, rivals, info = res
        good = [o for o in rep.options if o.stops and o.id in rep.eligible]
        win = rep.window or {}
        tail = f" Tus gomas llegan hasta la vuelta {win['limit_lap']}." if win.get("limit_lap") and not win.get("reaches_end") else ""
        if not good:
            return ("Ahora mismo lo mejor es no parar." if rep.best.kind == "STAY" else f"Lo mejor: {rep.best.desc.lower()}.") + tail
        laps = sorted(o.stops[0][0] for o in good)
        best = rep.best.stops[0][0] if rep.best.stops else laps[0]
        span = f"de la vuelta {laps[0]} a la {laps[-1]}" if laps[-1] > laps[0] else f"en la vuelta {laps[0]}"
        return f"La ventana buena para parar va {span}; la mejor, la {best} a {S.NAME[rep.best.stops[0][1]] if rep.best.stops else '?'}.{tail}"

    def t_faltan_parar(self, i):
        tr = self.eng.tr
        g, order = tr.gaps()
        if tr.player not in order or not tr.is_race():
            return "Eso solo lo sé en carrera."
        if i.driver or i.topic in ("delante", "detras"):          # «¿el de delante ya paró?»: ese coche concreto
            car = self._target(i)
            if car is None or not tr.lap_all[car]:
                return "No tengo a nadie ahí."
            n = tr.lap_all[car].get("pit_stops", 0)
            used = len(tr.used_compounds(car))
            state = ("está en boxes ahora" if tr.lap_all[car]["pit_status"] else
                     f"ya ha parado {'una vez' if n == 1 else f'{n} veces'}" if n else "aún no ha parado")
            rule = tr.params.rule_applies((tr.ses or {}).get("total_laps") or 0)
            tail = " y todavía le falta usar otro compuesto" if rule and n == 0 and used < 2 else ""
            return f"{tr.name(car)} {state}{tail}."
        k = order.index(tr.player)
        rule = tr.params.rule_applies((tr.ses or {}).get("total_laps") or 0)
        near = order[max(0, k - 5):k] + order[k + 1:k + 4]
        todo = [c for c in near if (len(tr.used_compounds(c)) < 2 if rule else (tr.lap_all[c] or {}).get("pit_stops", 0) == 0)]
        if not todo:
            return "Todos los que tienes cerca ya han parado."
        ahead = [tr.name(c) for c in todo if order.index(c) < k]
        behind = [tr.name(c) for c in todo if order.index(c) > k]
        out = []
        if ahead:
            out.append("Delante aún tienen que parar: " + ", ".join(ahead) + ".")
        if behind:
            out.append("Detrás: " + ", ".join(behind) + ".")
        return " ".join(out)

    def t_estrategia(self, i):
        res, err = self._eval(self.eng.goals.include())
        if err:
            tr, d = self.eng.tr, self.eng.dialog
            # informe del 11 nov: en el pit lane (o con pocas vueltas tras parar) salía otra vez el plan PREVIO entero, aunque había un
            # plan aceptado. En carrera ya empezada se dice lo que se sigue; el plan previo solo al principio.
            if d.accepted and d.accepted.option.stops:
                lap = d.accepted.option.stops[0][0]
                when = "esta vuelta" if lap == tr.cur_lap else f"en la vuelta {lap}"
                return f"Seguimos con el plan aceptado: parar {when} a {S.NAME.get(d.accepted.option.stops[0][1], '?')}."
            if tr.is_race() and tr.cur_lap > 1:
                if tr.prev_pit:
                    return "Estás en boxes: en cuanto salgas recalculo la estrategia."
                pre = getattr(self.eng, "preplan", None)
                if pre and tr.lap_all[tr.player] and tr.lap_all[tr.player].get("pit_stops", 0) == 0 and len(pre["stints"]) > 1:
                    return (f"Con el plan previo: parar al final de la vuelta {pre['stints'][0][1]} a {S.NAME.get(pre['stints'][1][0], '?')}. "
                            f"Aún no puedo recalcular: {err}.")
                return f"Ahora no puedo recalcular la estrategia: {err}."
            import raceplan
            plan, why = raceplan.build(self.eng)                 # al principio no hay ritmo de carrera: vale el plan con lo medido en entrenos
            return plan if plan else f"Ahora no puedo evaluar la estrategia: {err}."
        rep, sit, me, rivals, info = res
        best = rep.best
        parts = []
        acc = self.eng.dialog.accepted
        if best.kind == "PIT" and acc and acc.option.stops and best.stops and D.same_stop(best.stops[0], acc.option.stops[0]):
            parts.append(f"El plan aceptado sigue siendo el mejor: {acc.option.desc.lower()}. Esperado P{best.e_pos:.1f}.")
        elif best.kind == "PIT":
            p = self._propose(rep, best, sit)
            parts.append("Recomiendo: " + best.phrase + " " + " ".join(best.evidence[1:3]) +
                         f" Esperado P{best.e_pos:.1f}. ¿Lo hacemos?")
            alt = next((o for o in rep.options if o.id != best.id and o.id in rep.eligible), None)
            if alt:
                parts.append(f"Alternativa: {alt.desc.lower()} (P{alt.e_pos:.1f}).")
        else:
            parts.append(f"Mantén el plan: {best.desc.lower()}. Esperado P{best.e_pos:.1f}. " + rep.win_view)
        if self.eng.goals.goal:
            parts.append(self.eng.goals.status_text(best, sit, me, rivals))
        return " ".join(parts)

    def _pit_now_option(self, rep, sit, comp=None):
        cands = [o for o in rep.options if o.stops and o.stops[0][0] == sit.lap and (comp is None or o.stops[0][1] == comp)]
        return max(cands, key=lambda o: o.utility) if cands else None

    def t_undercut(self, i):
        res, err = self._eval()
        if err:
            return f"Ahora no puedo evaluarlo: {err}."
        rep, sit, me, rivals, info = res
        opt = self._pit_now_option(rep, sit)
        if opt is None:
            return "Ahora no hay una parada válida que evaluar (quedan pocas vueltas o falta algún dato)."
        better = rep.best.id == opt.id
        self._propose(rep, opt, sit)
        txt = f"Parar ahora a {S.NAME[opt.stops[0][1]]}: esperado P{opt.e_pos:.1f}. " + " ".join(opt.evidence[1:4])
        dirty = self.eng.tr.dirty_air()
        if dirty:
            txt += f" Detrás de otro coche pierdes ~{dirty['loss_s']:.1f} s por vuelta ({dirty['n']} vueltas medidas)."
        if not better:
            txt += f" Aun así, la mejor opción es: {rep.best.desc.lower()} (P{rep.best.e_pos:.1f})."
        return txt + " ¿Lo hacemos?"

    def _timing_reply(self, direction):
        import timing
        res, err = self._eval(self.eng.goals.include())
        if err:
            return f"Ahora no puedo evaluarlo: {err}."
        rep, sit, me, rivals, info = res
        plan = self._plan_option(rep, sit)
        if plan is None:
            return "Ahora no hay una parada planificada que ajustar: dime «quiero parar ya» o pregúntame la estrategia."
        alts = timing.alternatives(rep, plan, sit.lap, direction, max_cost=1.0, min_dp=0.05)
        label = {"antes": "adelantar", "despues": "retrasar"}[direction]
        if not alts:
            return (f"No veo una forma de {label} la parada que valga la pena: el plan es {plan.desc.lower()} "
                    f"(P{plan.e_pos:.1f}) y ninguna variante de 1 a 3 vueltas mejora tus opciones frente a un rival.")
        a = alts[0]
        txt = timing.describe(a, plan, sit.lap, self.eng.tr.dirty_air())
        self._propose(rep, a["option"], sit, reason="riesgo: " + a["kind"])
        self.eng.dialog.pending.frase = txt
        return txt + " ¿Lo hacemos?"

    def _plan_option(self, rep, sit):
        keep = self.eng.plan_keep()
        if keep:
            for o in rep.options:
                if o.stops and D.same_stop(o.stops[0], keep, 0):
                    return o
        return rep.best if rep.best.kind == "PIT" else None

    def t_overcut(self, i):
        return self._timing_reply("despues")

    def t_alternativa(self, i):
        eng = self.eng
        alt = eng.dialog.alt
        if alt and alt["option"].stops[0][0] >= eng.tr.cur_lap:
            o = alt["option"]
            eng.dialog.propose(o, "alternativa", eng.tr.cur_lap, eng.tr.t, "piloto", "riesgo: " + alt["kind"], 0.0)
            eng.plan_hint = o.stops[0]
            return eng.dialog.accept(eng.tr.cur_lap)
        return self._timing_reply("antes")

    def t_parar_ya(self, i):
        res, err = self._eval()
        if err:
            return f"Ahora no puedo evaluarlo: {err}."
        rep, sit, me, rivals, info = res
        opt = self._pit_now_option(rep, sit)
        if opt is None:
            return "No hay una parada válida ahora (quedan pocas vueltas o falta algún dato)."
        eng = self.eng
        pend, acc = eng.dialog.pending, eng.dialog.accepted
        ctx = ""
        if pend:
            ctx = "Esto sustituye a la propuesta anterior. "
        elif acc and getattr(acc.option, "stops", None):
            ctx = f"El plan aceptado era parar en la vuelta {acc.option.stops[0][0]}. "
        self._propose(rep, opt, sit)
        cost = rep.best.utility - opt.utility
        txt = ctx + f"Si paras ahora a {S.NAME[opt.stops[0][1]]}, la posición final esperada es P{opt.e_pos:.1f}."
        if rep.best.id != opt.id:
            txt += f" La mejor opción es {rep.best.desc.lower()} (P{rep.best.e_pos:.1f}); parar ahora cuesta {cost:.1f} puntos."
            return txt + " ¿Entro a boxes igualmente?"
        return txt + " Es también la mejor opción. ¿Entro a boxes?"

    def t_avisos(self, i):
        from tactics import TOPICS
        if i.topic is None:
            self.awaiting = ("avisos", i.level)
            return "¿De qué avisos: " + ", ".join(TOPICS) + "?"
        self.eng.tac.topics[i.topic] = i.level
        what = {"combustible": "de combustible", "ers": "del ERS", "drs": "del DRS", "sectores": "de sectores",
                "adelantamiento": "de adelantamiento y defensa", "rivales": "de los rivales cercanos",
                "alternativas": "de alternativas de parada"}.get(i.topic, "de " + i.topic)
        if i.level < 0:
            return f"Vale: no te daré avisos {what}." + (" Solo los críticos al final de la carrera." if i.topic == "combustible" else "")
        if i.level > 0:
            return f"Vale: más avisos {what}."
        return f"Vale: avisos {what} como siempre."

    def t_campeonato(self, i):
        s = self.eng.tr.summary()
        base = ("El juego no me envía la clasificación del campeonato ni los puntos acumulados, solo tu posición en esta carrera. ")
        if not s:
            return base.strip()
        n = s["posicion"]
        pts = RACE_POINTS[n - 1] if 1 <= n <= len(RACE_POINTS) else 0
        return base + f"Ahora vas P{n}: con el reparto estándar serían {pts} puntos de esta carrera."

    def _options_by_compound(self, rep, comps):
        """Mejor opción (por utilidad) de cada compuesto como PRÓXIMA parada, en cualquier vuelta."""
        best = {}
        for o in rep.options:
            if o.stops and (not comps or o.stops[0][1] in comps) and o.id in rep.eligible:
                c = o.stops[0][1]
                if c not in best or o.utility > best[c].utility:
                    best[c] = o
        return best

    def t_elegir_compuesto(self, i):
        import compare as C
        from answers import wet_tyres
        tr = self.eng.tr
        wet = wet_tyres(tr.ses)
        if wet:
            return wet                                    # en mojado la simulación no corre: se dice qué neumático y el pronóstico
        res, err = self._eval(self.eng.goals.include())
        if err:
            return f"Ahora no puedo evaluarlo: {err}."
        rep, sit, me, rivals, info = res
        comps = tuple(c for c in (i.comps or (16, 17, 18)) if c in S.NAME)
        by = self._options_by_compound(rep, comps)
        if not by:
            return "No tengo ninguna parada válida que comparar (el reglamento, los juegos o las vueltas que quedan no lo permiten)."
        ranked = sorted(by.items(), key=lambda kv: -kv[1].utility)
        c0, o0 = ranked[0]
        txt = f"Mejor opción: {S.NAME[c0]}, parando en la vuelta {o0.stops[0][0]} (P{o0.e_pos:.1f} esperado)."
        if len(ranked) > 1:
            c1, o1 = ranked[1]
            gap = o0.utility - o1.utility
            txt += (f" {S.NAME[c1]} (vuelta {o1.stops[0][0]}): P{o1.e_pos:.1f}, " +
                    ("prácticamente igual." if gap < 0.1 else f"cuesta {gap:.1f} puntos."))
        miss = [S.NAME[c] for c in comps if c not in by]
        if miss and i.comps:
            txt += " Sin opción válida con " + " ni ".join(miss) + " en la simulación."
        txt += " " + C.evidence_text(tr, c0)
        if len(ranked) > 1:
            txt += " " + C.evidence_text(tr, ranked[1][0])
        txt += " " + " ".join(o0.evidence[1:3])
        self._propose(rep, o0, sit, reason="comparé los compuestos con datos de otros pilotos")
        return txt + " ¿Lo hacemos?"

    def t_resumen(self, i):
        import debrief
        return " ".join(debrief.build(self.eng))

    # ------------------------------------------------------------------ vueltas medidas
    def _my_laps(self):
        """(última vuelta cronometrada, mejor anterior) con trazas, o (None, None)."""
        eng = self.eng
        last = eng.tr.traces.last_timed(eng.tr.player)
        prev = [r for r in eng.my_timed if r is not last]
        return last, (min(prev, key=lambda r: r.time_ms) if prev else None)

    NO_LAP = ("Aún no tengo una vuelta completa tuya medida: necesito una vuelta cronometrada y válida con el programa "
              "escuchando desde la salida.")

    def t_analisis_vuelta(self, i):
        import quali as Q
        last, prev = self._my_laps()
        if last is None:
            return self.NO_LAP
        if prev is None:
            return " ".join([f"Vuelta {Q.fmt(last.time_ms)}."] + Q.ers_report(last) + ["Aún no tengo otra vuelta tuya para compararte."])
        if last.time_ms < prev.time_ms:
            return " ".join(Q.lap_report(last, prev, "tu mejor vuelta anterior", n=5) + Q.ers_report(last, prev, "tu vuelta anterior")[:1])
        return " ".join(Q.lap_report(last, prev, "tu mejor vuelta", n=5) + Q.ers_report(last, prev, "tu mejor vuelta")[:2])

    def t_ers_vuelta(self, i):
        import quali as Q
        last, prev = self._my_laps()
        if last is None:
            return self.NO_LAP
        return " ".join(Q.ers_report(last, prev, "tu mejor vuelta") if prev else Q.ers_report(last))

    def t_compara_vuelta(self, i):
        import quali as Q, compare as C
        tr = self.eng.tr
        mine = tr.traces.best.get(tr.player)
        if mine is None:
            return self.NO_LAP
        car = None
        if i.driver:
            car = i.driver[0]
        elif i.topic == "companero":
            car = C.teammate(tr)
        else:
            order = sorted((c["position"], k) for k, c in enumerate(tr.lap_all) if c and c["result_status"] == 2 and c["position"] > 0)
            mypos = tr.lap_all[tr.player]["position"]
            want = 1 if i.topic == "primero" else mypos - 1
            car = next((k for p, k in order if p == want and k != tr.player), None)
        if car is None:
            return "No identifico con quién compararte."
        ref = tr.traces.best.get(car)
        if ref is None:
            return (f"No tengo una vuelta completa medida de {tr.name(car)}: su telemetría no llegó o no ha dado una vuelta "
                    "cronometrada con el programa escuchando.")
        if abs(mine.track_m - ref.track_m) > 5:
            return "Esas vueltas no son de la misma pista."
        return " ".join(Q.rival_report(mine, ref, tr.name(car)) + Q.ers_report(mine, ref, tr.name(car))[:2])

    def t_estimacion(self, i):
        import practice as PR
        return " ".join(PR.summary(self.eng.tr))

    def t_companero(self, i):
        import compare as C
        tr = self.eng.tr
        j = C.teammate(tr)
        if j is None:
            return "No identifico a tu compañero de equipo en los datos de esta sesión."
        name = tr.name(j)
        la, sa, dm = tr.lap_all[j], tr.sta_all[j], tr.dmg_all[j]
        if not la or la["result_status"] != 2:
            return f"{name} ya no está en carrera."
        if self._kind() != "race":
            return "Tu compañero: " + self.nr_gap(j, name)
        gap = tr.rel_gap(j)
        txt = f"Tu compañero {name} va P{la['position']}"
        if gap is not None:
            txt += f", a {abs(gap):.1f} s {'delante' if gap < 0 else 'detrás'} de ti"
        txt += "."
        if sa:
            txt += f" Lleva {S.NAME.get(sa['visual_compound'], '?')} con {sa['tyre_age_laps']} vueltas"
            if dm:
                txt += f" y {max(dm['tyre_wear_pct']):.0f}% de desgaste"
            txt += f"; paradas: {la['pit_stops']}."
        story = C.stint_story(tr, j)
        if story and la["pit_stops"]:
            txt += f" Ha rodado {story}."
        pm, pj = tr.pace(tr.player), tr.pace(j)
        if pm and pj and abs(pm - pj) >= 0.1:
            txt += f" Su ritmo reciente es {abs(pm - pj):.1f} s {'mejor' if pj < pm else 'peor'} que el tuyo."
        r = C.stop_reactions(tr, [j])
        if r:
            r = r[0]
            txt += (f" Desde que paró a {S.NAME.get(r['to'], '?')} rueda {abs(r['delta']):.1f} s "
                    f"{'más rápido' if r['delta'] < 0 else 'más lento'} que antes ({r['laps']} vueltas limpias; incluye ir más ligero).")
        return txt

    def t_compuesto(self, i):
        res, err = self._eval()
        if err:
            return f"Ahora no puedo evaluarlo: {err}."
        rep, sit, me, rivals, info = res
        name = S.NAME[i.compound]
        opt = self._pit_now_option(rep, sit, i.compound)
        if opt is None:
            return f"No tengo evaluada una parada ahora a {name} (el reglamento, los juegos disponibles o las vueltas que quedan no lo permiten)."
        self._propose(rep, opt, sit)
        txt = f"Si paras ahora a {name}, la posición final esperada es P{opt.e_pos:.1f}."
        if rep.best.id != opt.id:
            txt += f" La mejor opción es {rep.best.desc.lower()} (P{rep.best.e_pos:.1f}); esta cuesta {rep.best.utility - opt.utility:.1f} puntos."
            return txt + " ¿Entro a boxes igualmente?"
        return txt + " Es la mejor opción. ¿Entro a boxes?"

    def t_compuesto_rivales(self, i):
        tr = self.eng.tr
        name = S.NAME[i.compound]
        ages = [sa["tyre_age_laps"] for k, sa in enumerate(tr.sta_all)
                if sa and k != tr.player and sa.get("visual_compound") == i.compound]
        if not ages:
            return f"Ahora ningún rival lleva {name}."
        ages.sort()
        return f"{len(ages)} rivales llevan {name}, con una mediana de {ages[len(ages) // 2]} vueltas de uso."

    def t_rival_neumaticos(self, i):
        tr = self.eng.tr
        if i.driver:
            idx, name = i.driver
        else:
            a, b = tr.neighbours()
            n = a if i.topic == "delante" else b
            if not n:
                return "No hay nadie " + ("delante." if i.topic == "delante" else "detrás.")
            idx, name = n[0], tr.name(n[0])
        sa = tr.sta_all[idx] if idx < len(tr.sta_all) else None
        if not sa:
            return f"No tengo datos de los neumáticos de {name}."
        return f"{name} lleva {S.NAME.get(sa['visual_compound'], '?')} con {sa['tyre_age_laps']} vueltas de uso."

    def t_rivales_neumaticos(self, i):
        tr = self.eng.tr
        cnt = {}
        for k, sa in enumerate(tr.sta_all):
            if sa and k != tr.player:
                cnt[sa["visual_compound"]] = cnt.get(sa["visual_compound"], 0) + 1
        if not cnt:
            return "No tengo datos de los neumáticos de los rivales."
        mine = tr.sta_all[tr.player]["visual_compound"] if tr.sta_all[tr.player] else None
        txt = "Rivales: " + ", ".join(f"{n} con {S.NAME.get(c, '?')}" for c, n in sorted(cnt.items(), key=lambda x: -x[1])) + "."
        diff = [S.NAME.get(c, "?") for c in cnt if c != mine]
        return txt + (f" Distintos al tuyo: {', '.join(diff)}." if diff else " Todos llevan tu mismo compuesto.")

    def t_dejar_pasar(self, i):
        idx, name = i.driver
        return self._hueco(idx, name) + " No uso la bandera azul ni órdenes de equipo en mis cálculos: dejarle pasar lo decides tú."

    @staticmethod
    def _sector_means(h, n=3):
        laps = [l for l in (h or {}).get("laps", []) if l["ms"] > 0 and min(l["s1"], l["s2"], l["s3"]) > 0][-n:]
        if len(laps) < 2:
            return None
        return [sum(l[k] for l in laps) / len(laps) / 1000 for k in ("s1", "s2", "s3")], len(laps)

    def t_vs_rival(self, i):
        tr = self.eng.tr
        idx, name = i.driver
        mine, his = self._sector_means(tr.hist.get(tr.player)), self._sector_means(tr.hist.get(idx))
        if not mine or not his:
            return f"No tengo vueltas suficientes tuyas y de {name} para compararos por sectores."
        n = min(mine[1], his[1])
        parts = []
        for k, (a, b) in enumerate(zip(mine[0], his[0]), 1):
            d = a - b
            parts.append(f"sector {k}: " + ("igual" if abs(d) < 0.05 else f"{'pierdes' if d > 0 else 'ganas'} {abs(d):.2f} s"))
        return (f"Frente a {name}, en tus últimas {n} vueltas: " + ", ".join(parts) +
                ". No tengo velocidad punta ni datos por curva de los rivales, solo tiempos por sector.")

    def t_viabilidad(self, i):
        n = i.pos
        if not n or not 1 <= n <= 22:
            return "¿En qué posición?"
        res, err = self._eval(self.eng.goals.include())
        if err:
            return f"Ahora no puedo evaluarlo: {err}."
        rep, sit, me, rivals, info = res
        best = rep.best
        a, b = S.p_range(best.cdf)
        what = "ganar" if n == 1 else f"acabar P{n} o mejor"
        return (f"Con la estrategia recomendada, la probabilidad de {what} es {S.pct(best.cdf[n - 1])}%. "
                f"Rango probable de llegada: P{a} a P{b}.")

    def t_sugerir_objetivo(self, i):
        res, err = self._eval(self.eng.goals.include())
        if err:
            return f"Ahora no puedo evaluarlo: {err}."
        rep, sit, me, rivals, info = res
        best = rep.best
        a, b = S.p_range(best.cdf)
        mid = next((k + 1 for k, v in enumerate(best.cdf) if v >= 0.5), 22)
        return (f"Lo realista es P{mid} (rango probable P{a} a P{b}). Si lo quieres como objetivo, dime «quiero acabar P{mid}».")

    def t_gomas_mal(self, i):
        eng, tr = self.eng, self.eng.tr
        before = eng.last_best_epos
        tr.felt_grip, tr.limit_delta = True, 10.0
        res, err = self._eval()
        if err:
            return f"Anotado que las gomas no rinden. Ahora no puedo recalcular: {err}."
        rep, sit, me, rivals, info = res
        best = rep.best
        txt = (f"Anotado: asumo que las gomas rinden peor (−{tr.params.felt_grip_loss_s:.1f} s por vuelta y límite de "
               f"desgaste {tr.params.wear_limit - tr.limit_delta:.0f}%). ")
        if best.kind == "PIT":
            self._propose(rep, best, sit, reason="por tu reporte de las gomas")
            txt += best.phrase + f" Esperado P{best.e_pos:.1f}" + (f" (antes P{before:.1f})" if before else "") + ". ¿Lo hacemos?"
        else:
            txt += f"Aun así lo mejor es seguir fuera: P{best.e_pos:.1f}. Si el agarre empeora, dímelo y paramos."
        return txt

    def t_incomodo(self, i):
        self.awaiting = "incomodo"
        return "Dime con qué no te sientes cómodo: las gomas, el coche o el ritmo."

    # ------------------------------------------------------------------ objetivos
    def t_objetivo_pos(self, i):
        n = i.pos
        if not n or not 1 <= n <= 22:
            return "¿En qué posición quieres terminar?"
        eng = self.eng
        eng.goals.set_pos(n, eng.tr.cur_lap)
        return self._goal_report()

    def t_objetivo_pasar(self, i):
        idx, name = i.driver
        eng = self.eng
        gap = eng.tr.rel_gap(idx)
        if gap is not None and gap >= 0:
            return f"{name} ya va detrás de ti."
        eng.goals.set_pass(idx, name, eng.tr.cur_lap)
        return self._goal_report()

    def _goal_report(self):
        eng = self.eng
        res, err = self._eval(eng.goals.include())
        g = eng.goals.goal
        if err:
            return f"Objetivo anotado: {g.desc()}. Aún no puedo evaluarlo: {err}."
        rep, sit, me, rivals, info = res
        opt = rep.best
        eng.goals.update(opt, sit, me, rivals, sit.lap)
        g = eng.goals.goal
        g.last_msg_lap = sit.lap
        return f"Objetivo anotado: {g.desc()}. " + eng.goals.status_text(opt, sit, me, rivals)

    def t_objetivo_estado(self, i):
        eng = self.eng
        if eng.goals.goal is None:
            return eng.goals.status_text(None, None, None, [])
        res, err = self._eval(eng.goals.include())
        if err:
            return f"No puedo evaluar el objetivo ahora: {err}."
        rep, sit, me, rivals, info = res
        eng.goals.update(rep.best, sit, me, rivals, sit.lap)
        return eng.goals.status_text(rep.best, sit, me, rivals)

    def t_objetivo_borrar(self, i):
        self.eng.goals.clear()
        return "Objetivo borrado."
