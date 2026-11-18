#!/usr/bin/env python3
"""Del texto de pantalla al texto que se LEE en voz alta.

Problema real (informe del 28 oct): la voz española de macOS lee «1.452» como «mil cuatrocientos cincuenta y dos» (el punto es el
separador de miles) y «1:42.993» a trompicones. Aquí todo número con decimales se convierte en palabras de radio:
  0.5 s -> «5 décimas» · 0.19 s -> «19 centésimas» · 0.442 s -> «442 milésimas» · 1.4 s -> «1 segundo y 4 décimas»
  1:43.450 -> «1 minuto, 43 segundos y 450 milésimas» · 49% -> «49 por ciento» · 136°C -> «136 grados»
La pantalla y el informe conservan los números tal cual; solo cambia lo que se pronuncia.
"""
import re

UNITS = {1: "décima", 2: "centésima", 3: "milésima"}
SIGN = {"+": "más ", "-": "menos "}


def _frac(digits):
    """'5' -> '5 décimas'; '19' -> '19 centésimas'; '442' -> '442 milésimas'; ceros a la izquierda se quitan; todo ceros -> ''."""
    digits = digits[:3]
    n = int(digits)
    if n == 0:
        return ""
    return f"{n} {UNITS[len(digits)]}{'' if n == 1 else 's'}"


def seconds_words(value_str):
    """'1.452' -> '1 segundo y 452 milésimas'; '0.4' -> '4 décimas'; '2.0' -> '2 segundos'; '0.0' -> 'cero segundos'."""
    ip, _, dp = value_str.partition(".")
    i = int(ip or 0)
    f = _frac(dp) if dp else ""
    if i == 0:
        return f or "cero segundos"
    base = f"{i} segundo{'' if i == 1 else 's'}"
    return base + (f" y {f}" if f else "")


def lap_time_words(m, s, frac):
    f = _frac(frac)
    out = f"{int(m)} minuto{'' if int(m) == 1 else 's'}, {int(s)} segundo{'' if int(s) == 1 else 's'}"
    return out + (f" y {f}" if f else "")


_LAP = re.compile(r"\b(\d{1,2}):(\d{2})\.(\d{1,3})\b")
_SECS = re.compile(r"(?<![\w.])([+−-]?)(\d+\.\d+|\d+)\s?s\b(?!\w)")
_KM = re.compile(r"\bkm (\d+)\.(\d+)\b")
_POS = re.compile(r"\bP(\d{1,2})\.(\d)\b")
_PCT = re.compile(r"(\d)\s?%")
_DEC = re.compile(r"(?<![\w:])(\d+)\.(\d+)(?![\w:])")


def to_speech(text):
    """Texto listo para el sintetizador: sin separadores de miles falsos, con las unidades en palabras."""
    if not text:
        return text
    t = text.replace("−", "-")
    t = _LAP.sub(lambda m: lap_time_words(m.group(1), m.group(2), m.group(3)), t)
    t = _KM.sub(lambda m: f"kilómetro {m.group(1)} coma {m.group(2)}", t)
    t = _POS.sub(lambda m: f"posición {m.group(1)} coma {m.group(2)}", t)
    t = _SECS.sub(lambda m: SIGN.get(m.group(1), "") + seconds_words(m.group(2)), t)   # el signo dice si vas mejor o peor: no se pierde
    t = _PCT.sub(r"\1 por ciento", t)
    t = t.replace("°C", " grados").replace("km/h", "kilómetros por hora").replace(" kg", " kilos")
    t = _DEC.sub(lambda m: f"{m.group(1)} coma {m.group(2)}", t)           # cualquier decimal que quede: «3 coma 5»
    t = re.sub(r"(?<![\w)])-(?=\d)", "menos ", t)
    t = re.sub(r"(?<![\w)])\+(?=\d)", "más ", t)
    t = re.sub(r"\b1 vueltas\b", "1 vuelta", t)
    t = re.sub(r"\bMJ\b", "megajulios", t)
    t = re.sub(r"\bkJ\b", "kilojulios", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()
