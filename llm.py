#!/usr/bin/env python3
"""Cliente del servidor local (LM Studio, API compatible con OpenAI) + validación de lo que responde.

Regla de diseño (la misma que descubriste probando Qwen3.5 9B): el modelo no aporta datos ni hace cuentas.
  - choose(): elige UNA opción entre las que ya calculó strategy.py (solo las dentro del margen del modo)
    y redacta el aviso. Si elige algo inválido, o cita una cifra que no está en la entrada, se ignora y se
    usa la frase de plantilla de la mejor opción.
  - answer(): responde preguntas del piloto usando solo el JSON de estado; sin dato -> "No tengo ese dato."
Si el servidor no responde, todo sigue funcionando sin IA.
"""
import json, re, urllib.request, urllib.error

NO_DATA = "No tengo ese dato."
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


class LLMError(Exception):
    pass


def numbers_in(text):
    return [float(x.replace(",", ".")) for x in _NUM.findall(text)]


def allowed_numbers(obj):
    """Todos los números que aparecen en la entrada (y su versión redondeada)."""
    out = set()

    def walk(o):
        if isinstance(o, bool):
            return
        if isinstance(o, (int, float)):
            out.update({float(o), float(round(o)), round(float(o), 1)})
        elif isinstance(o, str):
            out.update(numbers_in(o))
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(k)
                walk(v)
        elif isinstance(o, (list, tuple, set)):
            for v in o:
                walk(v)
    walk(obj)
    return out


def numbers_ok(text, allowed, tol=0.051):
    return all(any(abs(n - a) <= tol for a in allowed) for n in numbers_in(text))


def extract_json(text, _repaired=False):
    """Primer objeto JSON del texto, con llaves anidadas (el modelo a veces añade texto antes/después o ```json)."""
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[i:])
            return obj if isinstance(obj, dict) else None
        except ValueError:
            i = text.find("{", i + 1)
    if not _repaired:
        # error típico de Qwen (11 nov): {"tools":[{"t":"estado_coche"],"say":""} -> falta cerrar la herramienta
        fixed = re.sub(r"(\{[^{}\[\]]*?)\s*\]", r"\1}]", text)
        if fixed != text:
            return extract_json(fixed, True)
    return None


def first_json(text):
    m = re.search(r"\{.*?\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


class LLMClient:
    def __init__(self, url="http://127.0.0.1:1234/v1", model="", temperature=0.2, timeout=20, max_tokens=200,
                 no_think=False, transport=None, reasoning="none"):
        self.url, self.model = url.rstrip("/"), model
        self.temperature, self.timeout, self.max_tokens, self.no_think = temperature, timeout, max_tokens, no_think
        self.reasoning = reasoning          # «reasoning_effort» por petición: "none" = no pensar aunque el servidor tenga Think
        self.transport = transport      # función(payload)->dict, para pruebas

    @classmethod
    def from_cfg(cls, cfg):
        c = cfg.get("llm", {})
        if not c.get("enabled"):
            return None
        return cls(c.get("url", "http://127.0.0.1:1234/v1"), c.get("model", ""), c.get("temperature", 0.2),
                   c.get("timeout_s", 20), c.get("max_tokens", 200), c.get("send_enable_thinking_false", False),
                   reasoning=c.get("reasoning_effort", "none"))

    def _post(self, path, payload=None):
        if self.transport and payload is not None:
            return self.transport(payload)
        req = urllib.request.Request(self.url + path, data=None if payload is None else json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LLMError(f"servidor no disponible: {e}")

    def ping(self):
        try:
            self._post("/models")
            return True
        except LLMError:
            return False

    def models(self):
        """Identificadores de modelo que ofrece el servidor (los que se pueden poner en `model`). LLMError si no responde."""
        data = self._post("/models")
        return [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]

    def chat(self, system, user, max_tokens=None, temperature=None, reasoning=None):
        payload = {"model": self.model, "temperature": self.temperature if temperature is None else temperature,
                   "max_tokens": max_tokens or self.max_tokens,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        effort = reasoning or self.reasoning
        if effort and effort != "auto":
            # medido el 11 nov con Qwen3.5 9B en LM Studio: con Think activo pensaba 600 tokens (22 s) y no respondía;
            # con reasoning_effort="none" respondió en 0.5 s. «chat_template_kwargs» y «/no_think» no lo apagaban.
            payload["reasoning_effort"] = effort
        if self.no_think:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        data = self._post("/chat/completions", payload)
        try:
            ch = data["choices"][0]
            content = (ch["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("respuesta con formato inesperado")
        if not content:
            why = "se quedó pensando (reasoning) y agotó max_tokens: apaga Think en el servidor" \
                if ch.get("finish_reason") == "length" else "respuesta vacía"
            raise LLMError(why)
        return content


# ------------------------------------------------------------------------------------------ decisión
SYS_CHOOSE = (
    "Eres el ingeniero de pista de un piloto de F1. Te paso la situación y una lista de opciones de estrategia "
    "ya calculadas por un programa. Elige UNA opción de la lista y redacta el aviso por radio.\n"
    "Reglas: responde SOLO con JSON {\"opcion\":\"<id>\",\"frase\":\"<aviso>\"}. La frase va en español, en tono "
    "directo de radio, máximo 20 palabras. Usa SOLO números que aparezcan en el JSON; no inventes datos ni "
    "causas. Modo conservador: evita riesgos. Modo normal: equilibrio. Modo arriesgado: busca la victoria "
    "aunque suponga más riesgo."
)
SYS_ANSWER = (
    "Eres el ingeniero de pista de un piloto de F1. Responde en español con UNA sola frase de máximo 20 palabras. "
    "Usa SOLO los datos del JSON. No inventes datos ni causas. Si te preguntan algo que no está en el JSON, "
    f"responde exactamente: \"{NO_DATA}\""
)


def choose(client, summary, report, mode, prefer=None):
    """Devuelve {"opcion", "frase", "source": "llm"|"code", "reason"}. Nunca lanza.
    prefer: id de una opción elegible que se prefiere por defecto (mantener el plan anterior sin saltos)."""
    by_id = {o.id: o for o in report.options}
    best = by_id[prefer] if prefer in by_id and prefer in report.eligible else report.best
    code = {"opcion": best.id, "frase": best.phrase, "source": "code", "reason": ""}
    elig = [by_id[i] for i in report.eligible if i in by_id]
    if client is None:
        code["reason"] = "sin modelo"
        return code
    if len(elig) < 2:
        code["reason"] = "una sola opción dentro del margen"
        return code
    payload = {"modo": mode, "situacion": summary, "opciones": [o.brief() for o in elig],
               "notas": report.notes, "vision": report.win_view}
    allowed = allowed_numbers(payload)
    try:
        raw = client.chat(SYS_CHOOSE, json.dumps(payload, ensure_ascii=False))
    except LLMError as e:
        code["reason"] = str(e)
        return code
    obj = first_json(raw)
    if not isinstance(obj, dict) or obj.get("opcion") not in by_id or obj["opcion"] not in {o.id for o in elig}:
        code["reason"] = "el modelo no eligió una opción válida"
        return code
    pick = by_id[obj["opcion"]]
    frase = obj.get("frase")
    if not isinstance(frase, str) or not frase.strip() or len(frase.split()) > 25 or not numbers_ok(frase, allowed):
        return {"opcion": pick.id, "frase": pick.phrase, "source": "llm",
                "reason": "frase del modelo descartada (cifras no verificables o demasiado larga)"}
    return {"opcion": pick.id, "frase": frase.strip(), "source": "llm", "reason": ""}


def answer(client, summary, question):
    """Responde una pregunta del piloto. Siempre devuelve texto; sin dato o con cifras inventadas -> NO_DATA."""
    if client is None:
        return NO_DATA
    payload = {"datos": summary, "pregunta": question}
    allowed = allowed_numbers(summary)
    try:
        raw = client.chat(SYS_ANSWER, json.dumps(payload, ensure_ascii=False))
    except LLMError:
        return NO_DATA
    raw = raw.strip().splitlines()[0] if raw.strip() else NO_DATA
    if len(raw.split()) > 30 or not numbers_ok(raw, allowed):
        return NO_DATA
    return raw
