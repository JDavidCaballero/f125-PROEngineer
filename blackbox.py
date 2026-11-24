#!/usr/bin/env python3
"""Caja negra: deja TODO lo que pasa en una sesión en una carpeta y lo junta en un solo archivo de texto (INFORME.txt).

Cada ejecución de voice.py crea runs/session-<fecha>/ con:
  console.log   todo lo que salió por pantalla, incluidos errores y tracebacks (también de los hilos)
  meta.json     fecha, comando, sistema, versión de Python y de cada paquete, y una copia de config.toml
  voice.jsonl   lo que dijiste / entendió / respondió / tiempos (sin audio)
  engine.jsonl  decisiones del ingeniero: propuestas, silencios, estrategia
  audio/        tus frases grabadas (si no usaste --no-audio): se quedan en TU Mac y NO van al informe
  INFORME.txt   resumen de auditoría + errores + consola + registros: es el único archivo que hace falta enviarme
"""
import importlib.metadata as md
import json, platform, sys, threading, traceback
from datetime import datetime
from pathlib import Path

PACKAGES = ["numpy", "sounddevice", "mlx", "mlx-whisper", "faster-whisper", "ctranslate2", "onnxruntime", "av", "huggingface-hub"]
KEEP_ENGINE = {"say", "proposal", "silence", "skip", "heard", "ask", "strategy", "lap_trace", "lap_rejected", "lap_report",
               "lap_report_skipped", "new_session", "proposal_blocked", "alt_offered", "debrief", "practice_summary",
               "llm_bad_json", "llm_error", "llm_compose_rejected", "memory", "memory_error", "preplan", "note_skipped",
               "summary_error", "coach", "cover", "budget"}


class Tee:
    """Escribe en pantalla y en el archivo a la vez."""

    def __init__(self, stream, f):
        self.stream, self.f = stream, f
        self.encoding = getattr(stream, "encoding", "utf-8")

    def write(self, s):
        try:
            self.stream.write(s)
        except Exception:                                       # noqa: BLE001 (la pantalla nunca debe romper el registro)
            pass
        self.f.write(s)
        return len(s)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:                                       # noqa: BLE001
            pass
        self.f.flush()

    def isatty(self):
        return getattr(self.stream, "isatty", lambda: False)()


def versions():
    out = {}
    for p in PACKAGES:
        try:
            out[p] = md.version(p)
        except md.PackageNotFoundError:
            out[p] = None
    return out


def system_info():
    return {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
            "machine": platform.machine(), "mac_ver": platform.mac_ver()[0], "packages": versions()}


class Session:
    def __init__(self, folder, argv, config_text=""):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.f = open(self.folder / "console.log", "a", buffering=1, encoding="utf-8")
        self._out, self._err = sys.stdout, sys.stderr
        self._hook, self._thook = sys.excepthook, threading.excepthook
        sys.stdout, sys.stderr = Tee(self._out, self.f), Tee(self._err, self.f)
        sys.excepthook = lambda t, v, tb: traceback.print_exception(t, v, tb)
        threading.excepthook = lambda a: (print(f"\n[excepción en el hilo {a.thread.name if a.thread else '?'}]", file=sys.stderr),
                                          traceback.print_exception(a.exc_type, a.exc_value, a.exc_traceback))
        meta = {"started": datetime.now().isoformat(timespec="seconds"), "argv": argv, **system_info(), "config_toml": config_text}
        (self.folder / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")

    def stop(self):
        sys.stdout, sys.stderr = self._out, self._err
        sys.excepthook, threading.excepthook = self._hook, self._thook
        self.f.close()


def _read(p):
    try:
        return Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def quiet(console):
    """Quita el ruido de las barras de progreso (descargas de modelos) que tapa lo importante."""
    out, last = [], None
    for line in console.replace("\r", "\n").splitlines():
        if not line.strip():
            continue
        if "Downloading bytes" in line or "Reconstruct" in line or "Fetching" in line or ("%|" in line and "it/s" in line):
            last = line.strip()
            continue
        if last:
            out.append("(descarga de modelo: última línea) " + last)
            last = None
        out.append(line)
    if last:
        out.append("(descarga de modelo: última línea) " + last)
    return "\n".join(out)


def errors_in(console):
    """Bloques de traceback y líneas con Error/FALLA/excepción de la consola."""
    out, block = [], []
    for line in console.splitlines():
        if line.startswith("Traceback") or line.startswith("[excepción"):
            block = [line]
        elif block and (line.startswith(" ") or not line.strip() or line.split(":")[0].endswith(("Error", "Exception"))):
            block.append(line)
            if not line.startswith(" ") and line.strip():
                out.append("\n".join(block))
                block = []
        elif "FALLA" in line or "Error" in line:
            out.append(line)
    if block:
        out.append("\n".join(block))
    return out


def _compact_voice(text):
    """Quita rutas de audio y recorta textos larguísimos (alucinaciones del reconocedor) para que el informe quede legible."""
    out = []
    for line in text.strip().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            out.append(line[:400])
            continue
        if isinstance(r.get("audio"), str):
            r["audio"] = Path(r["audio"]).name
        for k in ("raw", "clean", "reply"):
            if isinstance(r.get(k), str) and len(r[k]) > 300:
                r[k] = r[k][:300] + "…"
        out.append(json.dumps(r, ensure_ascii=False, default=str))
    return "\n".join(out)


def pack(folder, render_report=None, max_console=160):
    """Escribe folder/INFORME.txt (un solo archivo de texto con todo) y devuelve su ruta."""
    folder = Path(folder)
    parts = []
    meta = _read(folder / "meta.json")
    parts.append("=== SISTEMA Y COMANDO ===\n" + meta.split('"config_toml"')[0].rstrip().rstrip(",") + "\n}")
    if render_report is not None:
        try:
            parts.append("=== AUDITORÍA ===\n" + render_report(folder))
        except Exception as e:                                  # noqa: BLE001
            parts.append(f"=== AUDITORÍA ===\n(no se pudo generar: {type(e).__name__}: {e})")
    console = _read(folder / "console.log")
    results = [l for l in console.splitlines() if l.startswith(("OK   ", "FALLA", "OK ")) and "s  " in l]
    if results:
        parts.append("=== RESULTADO DE LAS COMPROBACIONES ===\n" + "\n".join(results))
    console = quiet(console)
    errs = errors_in(console)
    parts.append("=== ERRORES ===\n" + ("\n---\n".join(errs) if errs else "ninguno registrado"))
    parts.append(f"=== CONSOLA (últimas {max_console} líneas) ===\n" + "\n".join(console.splitlines()[-max_console:]))
    voice = _read(folder / "voice.jsonl")
    parts.append("=== REGISTRO DE VOZ (voice.jsonl; texto, sin audio) ===\n" + (_compact_voice(voice) or "(vacío)"))
    eng = []
    for line in _read(folder / "engine.jsonl").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") in KEEP_ENGINE:
            r.pop("report", None)                               # la tabla completa de opciones es enorme
            r.pop("summary", None)
            eng.append(json.dumps(r, ensure_ascii=False, default=str))
    parts.append("=== DECISIONES DEL INGENIERO (engine.jsonl resumido) ===\n" + ("\n".join(eng) or "(vacío)"))
    raw = folder / "udp.f1raw"
    if raw.exists():
        parts.append(f"=== GRABACIÓN DE PAQUETES ===\n{raw} ({raw.stat().st_size / 1e6:.1f} MB): todos los paquetes del juego, reproducible con "
                     "`python voice.py lab --file <ruta>`; no va dentro de este informe.")
    path = folder / "INFORME.txt"
    path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return path
