#!/usr/bin/env python3
"""Protocolo de la radio: qué hacer cuando no te entiende (lógica pura, sin audio, fácil de probar).

Flujo que pediste:
  1. Si no te entiende: "No he entendido. Repite, por favor." (tantas veces como haga falta, sin asumir nada).
  2. Si hay una propuesta pendiente y falla `max_misses` veces seguidas, pasa al canal de respaldo:
     "No te entiendo. Si guardas silencio entenderé que no; si pulsas la radio otra vez, confirmo."
  3. En el respaldo (ventana de `fallback_window_s` segundos, que EMPIEZA cuando el ingeniero termina de explicarlo):
     un toque corto del botón = confirma;
     silencio hasta que acabe la ventana = rechaza; si hablas y te entiende, se atiende normal.
Un "toque" es pulsar y soltar el botón en menos de `tap_max_s` segundos sin hablar.
El reloj se inyecta (`now`) para poder probarlo sin esperar.
"""
from dataclasses import dataclass

NOT_UNDERSTOOD = "No he entendido. Repite, por favor."
NOT_HEARD = "No te he oído. Repite, por favor."
FALLBACK = "No te entiendo. Si callas, la descarto. Si pulsas la radio, la confirmo."
FALLBACK_AGAIN = "Sigo sin entenderte. Callas: descarto. Pulsas: confirmo."
FALLBACK_DECLINED = "Sin respuesta: descarto la propuesta."
FALLBACK_ACCEPTED = "Confirmado por radio."


@dataclass
class Action:
    kind: str                 # "say" | "accept" | "decline"
    text: str = ""
    prio: int = 1


class Radio:
    def __init__(self, cfg=None):
        r = (cfg or {}).get("radio", {})
        self.max_misses = r.get("max_misses", 2)
        self.window_s = r.get("fallback_window_s", 7.0)
        self.tap_max_s = r.get("tap_max_s", 0.5)
        self.arm_timeout_s = r.get("arm_timeout_s", 15.0)
        self.reset()

    def reset(self):
        self.misses = 0
        self.state = "idle"            # idle | fallback
        self.deadline = None           # None mientras se está diciendo la explicación; arm() lo fija al terminar de hablar
        self.entered = None

    def new_proposal(self):
        """Una propuesta nueva empieza de cero (no arrastra fallos anteriores)."""
        self.reset()

    def heard(self, now, understood, empty=False, pending=False):
        """Se procesó una frase. understood: el asistente reconoció una intención. empty: no se oyó nada útil.
        Devuelve acciones; la respuesta normal del asistente la dice quien llama cuando understood es True."""
        if understood:
            self.misses = 0
            self.state, self.deadline = "idle", None
            return []
        self.misses += 1
        if pending and self.state == "fallback":                  # ya se explicó: repite corto y reinicia la ventana
            self.deadline, self.entered = None, now
            return [Action("say", FALLBACK_AGAIN, 1)]
        if pending and self.misses >= self.max_misses:
            self.state, self.deadline, self.entered = "fallback", None, now
            return [Action("say", FALLBACK, 1)]
        return [Action("say", NOT_HEARD if empty else NOT_UNDERSTOOD, 1)]

    def tap(self, now, pending):
        """Toque corto del botón. En el respaldo y con propuesta pendiente: confirma."""
        if self.state == "fallback" and pending:
            self.reset()
            return [Action("accept"), Action("say", FALLBACK_ACCEPTED, 1)]
        return []

    def arm(self, now):
        """La explicación del respaldo ya se dijo: desde ahora corren los `window_s` segundos de silencio."""
        if self.state == "fallback" and self.deadline is None:
            self.deadline = now + self.window_s

    def tick(self, now, pending):
        """Llamar a menudo. Si vence la ventana de respaldo sin respuesta: rechaza."""
        if self.state != "fallback":
            return []
        if not pending:
            self.reset()
            return []
        if self.deadline is None:                       # por si nadie avisó de que terminó de hablar (sin voz, error)
            if now - self.entered > self.arm_timeout_s:
                self.arm(now)
            return []
        if now >= self.deadline:
            self.reset()
            return [Action("decline"), Action("say", FALLBACK_DECLINED, 1)]
        return []

    def is_tap(self, press_s, spoke):
        return press_s < self.tap_max_s and not spoke
