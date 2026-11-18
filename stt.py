#!/usr/bin/env python3
"""Voz a texto (intercambiable) y grabación del micrófono.

Backends: 'mlx' (mlx-whisper, pensado para Apple Silicon), 'faster' (faster-whisper, CPU) y 'fake' (pruebas).
Nada se importa hasta que se usa: este archivo se puede cargar sin instalar nada. Las llamadas a mlx-whisper y
faster-whisper están escritas de memoria y NO se han ejecutado aquí: `python voice.py selftest` las comprueba en tu Mac.
Whisper alucina con silencio o ruido ("Subtítulos por la comunidad de Amara.org"), así que cada resultado pasa por
clean(): se descarta si hay poca voz, poca confianza o una frase típica de alucinación.
"""
import re, time, wave

MLX_MODELS = {"tiny": "mlx-community/whisper-tiny-mlx", "base": "mlx-community/whisper-base-mlx",
              "small": "mlx-community/whisper-small-mlx", "medium": "mlx-community/whisper-medium-mlx",
              "large": "mlx-community/whisper-large-v3-mlx", "turbo": "mlx-community/whisper-large-v3-turbo"}
HALLUCINATIONS = ("amara.org", "subtítulos", "subtitulos", "gracias por ver", "suscríbete", "suscribete",
                  "música", "musica", "www.", ".com", "thanks for watching")
SR = 16000


def build_prompt(names=()):
    """Pista para Whisper: vocabulario de la radio y pilotos de la parrilla (mejora mucho los nombres propios)."""
    base = ("Radio de equipo de Fórmula 1 en español. Palabras: neumáticos, gomas, boxes, undercut, overcut, combustible, "
            "ERS, DRS, Safety Car, VSC, estrategia, hueco, ritmo, sí, no, repite, explícame, silencio, objetivo, pasar, "
            "pole, P1, P2, Q1, Q2, Q3, sprint, libres, pits, clasificación, entrenamiento, curva, vuelta rápida, desgaste, batería, daños, adelantar, defender.")
    names = list(names)
    ej = (f" Ejemplos: ¿Qué neumáticos lleva {names[0]}? ¿Cómo voy contra {names[1] if len(names) > 1 else names[0]}?" if names else "")
    return base + (" Pilotos: " + ", ".join(names) + "." if names else "") + ej


def looks_like_loop(text):
    """Whisper a veces entra en bucle ('Blablabla blablabla ...'): muchas palabras y casi todas repetidas, o una palabra enorme."""
    words = re.findall(r"\w+", text.lower())
    if any(len(w) > 40 for w in words):
        return True
    return len(words) >= 12 and len(set(words)) / len(words) < 0.35


def clean(res, min_logprob=-1.2, max_no_speech=0.6, min_chars=2, max_chars_per_s=25):
    """(texto, motivo_de_descarte). El texto es None si el resultado no es de fiar."""
    text = (res.get("text") or "").strip()
    if len(text) < min_chars:
        return None, "vacío"
    if looks_like_loop(text):
        return None, "bucle de repetición de Whisper"
    dur = res.get("duration_s")
    if dur and len(text) > max_chars_per_s * max(dur, 1.0):
        return None, "texto demasiado largo para el audio"
    low = text.lower()
    if any(h in low for h in HALLUCINATIONS):
        return None, "alucinación típica de Whisper"
    if res.get("no_speech_prob") is not None and res["no_speech_prob"] > max_no_speech:
        return None, f"poca voz (no_speech={res['no_speech_prob']:.2f})"
    if res.get("avg_logprob") is not None and res["avg_logprob"] < min_logprob:
        return None, f"poca confianza (logprob={res['avg_logprob']:.2f})"
    return fix_jargon(text), None


# errores de oído repetidos de Whisper con la jerga de F1 (vistos en informes reales) -> lo que se dijo
JARGON = [
    (r"\b(?:quema|kema|cuema|kiu) ?([123])\b", r"Q\1"),                 # «Quema 1»
    (r"\b(?:bits|pitts?)\b", "pits"),                                           # «Entran los bits»
    (r"\b(?:qu?ale|k?ale|cali|kali|quale)\b", "quali"),                         # «mi vuelta de QALE»
    (r"\bteledimetr[ií]a\b", "telemetría"),
    (r"(?i)\ben qu[eé] secci[oó]n estamos\b", "en qué sesión estamos"),
    (r"(?i)\bsprint\s+shoot\s?out\b", "sprint shootout"),
]


def fix_jargon(text):
    import re
    for pat, rep in JARGON:
        text = re.sub(pat, rep, text, flags=re.I)
    return text


def similarity(a, b):
    """1.0 = idénticos, tras quitar acentos, mayúsculas y signos. Distancia de edición por palabras."""
    import unicodedata

    def norm(x):
        x = "".join(c for c in unicodedata.normalize("NFD", x.lower()) if unicodedata.category(c) != "Mn")
        return re.sub(r"[^\w\s]", " ", x).split()
    A, B = norm(a), norm(b)
    if not A and not B:
        return 1.0
    prev = list(range(len(B) + 1))
    for i, wa in enumerate(A, 1):
        cur = [i]
        for j, wb in enumerate(B, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (wa != wb)))
        prev = cur
    return max(0.0, 1 - prev[-1] / max(len(A), len(B)))


def read_wav_pcm(path):
    """(bytes PCM int16, frecuencia, canales) de un WAV, solo con la librería estándar."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"WAV de {8 * w.getsampwidth()} bits: se esperaba 16")
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()


def load_wav(path):
    """WAV -> matriz float32 mono a 16 kHz. Así los motores no necesitan ffmpeg para leer archivos."""
    import numpy as np
    pcm, rate, ch = read_wav_pcm(path)
    a = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch)[:, 0]
    if rate != SR and len(a):
        a = np.interp(np.linspace(0, len(a) - 1, int(len(a) * SR / rate)), np.arange(len(a)), a).astype("float32")
    return a


class FakeSTT:
    """Devuelve textos preparados (pruebas) o el que se le pase en audio si es str (modo texto)."""
    name, model = "fake", "-"

    def __init__(self, texts=()):
        self.texts = list(texts)

    def transcribe(self, audio, prompt=""):
        t0 = time.time()
        item = audio if isinstance(audio, str) else (self.texts.pop(0) if self.texts else "")
        res = dict(item) if isinstance(item, dict) else {"text": item, "avg_logprob": -0.2, "no_speech_prob": 0.01}
        res.update(backend="fake", model="-", latency_s=time.time() - t0)
        return res


class FasterWhisperSTT:
    name = "faster"

    def __init__(self, model="small"):
        from faster_whisper import WhisperModel          # pip install faster-whisper
        self.model = model
        self.m = WhisperModel(model, device="cpu", compute_type="int8")

    def transcribe(self, audio, prompt=""):
        t0 = time.time()
        audio = load_wav(audio) if isinstance(audio, str) else audio
        segs, _ = self.m.transcribe(audio, language="es", initial_prompt=prompt, beam_size=1,
                                    condition_on_previous_text=False, temperature=0.0, vad_filter=True)
        segs = list(segs)
        return {"text": " ".join(s.text.strip() for s in segs).strip(),
                "avg_logprob": (sum(s.avg_logprob for s in segs) / len(segs)) if segs else None,
                "no_speech_prob": max((s.no_speech_prob for s in segs), default=None),
                "backend": "faster", "model": self.model, "latency_s": time.time() - t0}


class MlxWhisperSTT:
    name = "mlx"

    def __init__(self, model="small"):
        import mlx_whisper                                # pip install mlx-whisper
        self.mod = mlx_whisper
        self.model = MLX_MODELS.get(model, model)

    def transcribe(self, audio, prompt=""):
        t0 = time.time()
        audio = load_wav(audio) if isinstance(audio, str) else audio
        r = self.mod.transcribe(audio, path_or_hf_repo=self.model, language="es", initial_prompt=prompt,
                                condition_on_previous_text=False, temperature=0.0)
        segs = r.get("segments") or []
        return {"text": (r.get("text") or "").strip(),
                "avg_logprob": (sum(s.get("avg_logprob", 0) for s in segs) / len(segs)) if segs else None,
                "no_speech_prob": max((s.get("no_speech_prob", 0) for s in segs), default=None),
                "backend": "mlx", "model": self.model, "latency_s": time.time() - t0}


def is_apple_silicon(platform=None, machine=None):
    import platform as pf, sys
    return (platform or sys.platform) == "darwin" and (machine or pf.machine()) == "arm64"


def setup_hint(platform=None):
    """Cómo instalar la voz en este sistema (los instaladores de la carpeta del proyecto)."""
    import sys
    platform = platform or sys.platform
    if platform == "darwin":
        return "./setup_mac.sh"
    if platform.startswith("win"):
        return r".\setup_windows.ps1 (PowerShell; instala faster-whisper, que se usa solo)"
    return "./setup_linux.sh (instala faster-whisper, que se usa solo)"


def default_model(platform=None):
    """Modelo de Whisper si no se pasa --model: 'medium' en Mac (lo de siempre), 'small' en el resto (faster-whisper en CPU, int8)."""
    import sys
    return "medium" if (platform or sys.platform) == "darwin" else "small"


def make_stt(backend="auto", model="small", platform=None, machine=None):
    """Crea el backend pedido. 'auto' = mlx y luego faster en Mac con Apple Silicon; solo faster en el resto (mlx no existe ahí).
    Lanza RuntimeError con instrucciones si no hay ninguno."""
    errors = []
    auto = ("mlx", "faster") if is_apple_silicon(platform, machine) else ("faster",)
    for name, cls in (("mlx", MlxWhisperSTT), ("faster", FasterWhisperSTT)):
        if backend == name or (backend == "auto" and name in auto):
            try:
                return cls(model)
            except Exception as e:                        # noqa: BLE001 (ImportError, fallo de modelo, etc.)
                errors.append(f"{name}: {type(e).__name__}: {e}")
    if backend == "fake":
        return FakeSTT()
    raise RuntimeError(f"No hay motor de voz a texto disponible. Ejecuta {setup_hint(platform)}. Detalle: " + " | ".join(errors))


class Recorder:
    """Grabación del micrófono (sounddevice + numpy, se importan al usarla)."""

    def __init__(self, device=None):
        import numpy as np
        import sounddevice as sd
        self.np, self.sd, self.device = np, sd, device
        self.chunks, self.stream = [], None

    @staticmethod
    def devices():
        import sounddevice as sd
        return str(sd.query_devices())

    def start(self):
        self.chunks = []
        self.stream = self.sd.InputStream(samplerate=SR, channels=1, dtype="float32", device=self.device,
                                          callback=lambda data, frames, t, status: self.chunks.append(data.copy()))
        self.stream.start()

    def stop(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        return self.np.concatenate(self.chunks)[:, 0] if self.chunks else self.np.zeros(0, dtype="float32")

    def rms(self, audio):
        return float(self.np.sqrt(self.np.mean(audio ** 2))) if len(audio) else 0.0

    def peak(self, audio):
        return float(self.np.max(self.np.abs(audio))) if len(audio) else 0.0

    def save_wav(self, audio, path):
        pcm = (self.np.clip(audio, -1, 1) * 32767).astype("int16")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
