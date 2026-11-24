#!/usr/bin/env python3
"""Voz del ingeniero: cola con prioridades y corte al hablar tú. Motor por sistema: macOS `say`, Windows SAPI (PowerShell +
System.Speech), Linux `espeak-ng` o `spd-say`; se elige solo o con `[voice] tts` de config.toml (ver choose_backend).
Windows y Linux: implementado y probado con procesos simulados, SIN probar en máquinas reales.

- prioridad 1 (crítico / respuesta a tu pregunta): corta lo que esté sonando y habla ya
- prioridad 2: espera su turno (cola corta; si se llena, descarta lo más viejo)
- prioridad 3 (informativo): se descarta si el ingeniero ya está hablando
- stop(): lo llama la radio cuando pulsas para hablar, para no hablar encima de ti
`popen` se inyecta, así se prueba sin sonido.
"""
import shutil, subprocess, sys, threading, time

from speech import to_speech
from collections import deque

PREFERRED = ["Mónica", "Monica", "Paulina", "Jorge", "Juan", "Diego", "Angélica", "Angelica"]


def find_spanish_voice(run=subprocess.run):
    """Nombre de una voz en español instalada en macOS, o None. Salida de `say -v ?`: 'Nombre   es_ES   # texto'."""
    try:
        out = run(["say", "-v", "?"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    voices = {}
    for line in out.splitlines():
        head = line.split("#")[0].split()
        if len(head) >= 2 and head[-1].lower().startswith("es"):
            voices[" ".join(head[:-1])] = head[-1]
    for p in PREFERRED:
        if p in voices:
            return p
    return next(iter(voices), None)


def _clamp(x, lo, hi):
    return max(lo, min(hi, int(round(x))))


class SayTTS:
    """macOS: el comando `say` (texto como último argumento; es lo que había siempre, sin cambios)."""
    name, available, can_synth, needs_voice = "say", True, True, True
    hint = "no hay voz es_*: Ajustes del Sistema > Accesibilidad > Contenido hablado > Voz del sistema"

    def command(self, text, voice, rate):
        return ["say", "-r", str(rate)] + (["-v", voice] if voice else []) + [text], None

    def synth_command(self, text, voice, path):
        return ["say"] + (["-v", voice] if voice else []) + ["-o", str(path), "--file-format=WAVE",
                                                          "--data-format=LEI16@16000", text], None

    def find_voice(self, run=subprocess.run):
        return find_spanish_voice(run)


def _ps(*statements):
    """PowerShell con un script FIJO de una línea (sentencias unidas por '; ', sin comillas dobles: nada que escapar).
    El texto a decir NUNCA va en la línea de comandos: va por stdin (UTF-8), así no hay inyección ni problemas de comillas."""
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", "; ".join(statements)]


# stdin: línea 1 = voz ("" = la primera española instalada), línea 2 = velocidad SAPI (-10..10), [SYNTH: línea 3 = WAV], resto = texto
_SAPI_COMMON = (
    "$ErrorActionPreference = 'Stop'",
    "Add-Type -AssemblyName System.Speech",
    "$in = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), (New-Object System.Text.UTF8Encoding($false)))",
    "$v = $in.ReadLine()",
    "$r = $in.ReadLine()",
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
    "$es = @($s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'es-*' })",
    "if ($v) { try { $s.SelectVoice($v) } catch { } } elseif ($es.Count -gt 0) { $s.SelectVoice($es[0].VoiceInfo.Name) }",
    "try { $s.Rate = [int]$r } catch { }",
)
SAPI_SPEAK = _SAPI_COMMON + ("$t = $in.ReadToEnd()", "$s.Speak($t)")
SAPI_SYNTH = _SAPI_COMMON + (
    "$o = $in.ReadLine()",
    "$t = $in.ReadToEnd()",
    "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
    "[System.Speech.AudioFormat.AudioChannel]::Mono)",
    "$s.SetOutputToWaveFile($o, $fmt)",
    "$s.Speak($t)",
    "$s.Dispose()",
)
SAPI_LIST = (
    "try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }",
    "Add-Type -AssemblyName System.Speech",
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
    "foreach ($x in $s.GetInstalledVoices()) { if ($x.Enabled) { $x.VoiceInfo.Name + '|' + $x.VoiceInfo.Culture.Name } }",
)


def sapi_rate(wpm):
    """Palabras por minuto (las de `say`, 190 por defecto) -> Rate de SAPI (-10..10; 0 ≈ 180 ppm, cada punto ≈ 18 ppm, aproximado)."""
    return _clamp((wpm - 180) / 18.0, -10, 10)


def parse_sapi_voices(out):
    """'Nombre|cultura' por línea -> la voz española preferida (es-ES, luego es-MX, luego cualquier es-*) o None."""
    voices = []
    for line in (out or "").splitlines():
        name, _, culture = line.strip().rpartition("|")
        if name and culture.lower().startswith("es"):
            voices.append((name, culture.lower()))
    for want in ("es-es", "es-mx"):
        for name, c in voices:
            if c == want:
                return name
    return voices[0][0] if voices else None


class SapiTTS:
    """Windows: voces SAPI con PowerShell + System.Speech (vienen con Windows; no hay que instalar nada).
    Cortar = terminate() = TerminateProcess de powershell.exe (en Windows es un cierre inmediato, sin señal que atrapar): la
    síntesis corre dentro de ese proceso, así que se calla al momento. Cada frase arranca PowerShell (~0,3-1 s de retraso)."""
    name, available, can_synth, needs_voice = "sapi", True, True, True
    hint = ("no hay voz en español en Windows: Configuración > Hora e idioma > Voz > Agregar voces (Español); "
            "si aun así no habla, comprueba que `powershell` abre en una terminal")

    def command(self, text, voice, rate):
        return _ps(*SAPI_SPEAK), f"{voice or ''}\n{sapi_rate(rate)}\n{text}"

    def synth_command(self, text, voice, path):
        return _ps(*SAPI_SYNTH), f"{voice or ''}\n0\n{path}\n{text}"

    def find_voice(self, run=subprocess.run):
        try:
            out = run(_ps(*SAPI_LIST), capture_output=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        return parse_sapi_voices(out.decode("utf-8", "replace") if isinstance(out, bytes) else out)


def parse_espeak_voices(out):
    """Salida de `espeak-ng --voices=es` (cabecera y columnas: Pty Language Age/Gender VoiceName File ...) -> 'es', 'es-419'... o None."""
    langs = []
    for line in (out or "").splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 2 and cols[1].lower().startswith("es"):
            langs.append(cols[1])
    for want in ("es", "es-419"):
        if want in langs:
            return want
    return langs[0] if langs else None


class EspeakTTS:
    """Linux: espeak-ng (o espeak). Texto por stdin (`--stdin`), velocidad en palabras por minuto (-s), como `say -r`."""
    name, available, can_synth, needs_voice = "espeak", True, True, False
    hint = ""

    def __init__(self, exe="espeak-ng"):
        self.exe = exe

    def command(self, text, voice, rate):
        return [self.exe, "-v", voice or "es", "-s", str(_clamp(rate, 80, 450)), "--stdin"], text

    def synth_command(self, text, voice, path):
        return [self.exe, "-v", voice or "es", "-w", str(path), "--stdin"], text

    def find_voice(self, run=subprocess.run):
        try:
            return parse_espeak_voices(run([self.exe, "--voices=es"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout)
        except (OSError, subprocess.SubprocessError):
            return None


class SpdTTS:
    """Linux: spd-say (speech-dispatcher). `-w` espera a terminar; el texto va tras `--` (no se confunde con una opción).
    Al matar spd-say el demonio podría seguir hablando: al cortar se manda además `spd-say -C` (cancelar)."""
    name, available, can_synth, needs_voice = "spd", True, False, False
    hint = ""

    def __init__(self, exe="spd-say"):
        self.exe = exe

    def command(self, text, voice, rate):
        cmd = [self.exe, "-w", "-l", "es", "-r", str(_clamp((rate - 180) / 2.0, -100, 100))]
        return cmd + (["-y", voice] if voice else []) + ["--", text], None

    def cancel_command(self):
        return [self.exe, "-C"]

    def find_voice(self, run=subprocess.run):
        return None


class NoTTS:
    """Sin motor de voz: no se dice nada (como --no-voice) y quien lo crea explica una vez cómo instalar uno (hint)."""
    name, available, can_synth, needs_voice = "none", False, False, False

    def __init__(self, hint=""):
        self.hint = hint

    def command(self, text, voice, rate):
        return None, None

    def find_voice(self, run=subprocess.run):
        return None


def _no_tts_hint(platform):
    if platform.startswith("win"):
        return "No encuentro PowerShell para la voz del ingeniero (SAPI viene con Windows). Sigo sin voz: los avisos salen por pantalla."
    if platform.startswith("linux"):
        return ("No hay motor de voz para el ingeniero: instala espeak-ng (sudo apt install espeak-ng | sudo dnf install espeak-ng | "
                "sudo pacman -S espeak-ng). Sigo sin voz: los avisos salen por pantalla.")
    return "No hay motor de voz para el ingeniero en este sistema. Sigo sin voz: los avisos salen por pantalla."


def choose_backend(name="auto", platform=None, which=shutil.which):
    """Motor de voz: 'auto' (por sistema) | 'say' | 'sapi' | 'espeak' | 'spd' | 'none'. Nunca lanza: si no hay, NoTTS con la explicación.
    Linux (auto): espeak-ng, luego espeak, luego spd-say."""
    platform = platform or sys.platform
    name = (name or "auto").strip().lower()
    if name == "none":
        return NoTTS('Voz del ingeniero desactivada ([voice] tts = "none"): los avisos salen por pantalla.')
    if name == "say" or (name == "auto" and platform == "darwin"):
        return SayTTS()                                  # en Mac, sin comprobar nada: el comportamiento de siempre
    if name == "sapi" or (name == "auto" and platform.startswith("win")):
        return SapiTTS() if which("powershell") else NoTTS(_no_tts_hint("win"))
    for b in {"espeak": ("espeak",), "spd": ("spd",)}.get(name, ("espeak", "spd")):
        exe = (which("espeak-ng") or which("espeak")) if b == "espeak" else which("spd-say")
        if exe:
            return EspeakTTS(exe) if b == "espeak" else SpdTTS(exe)
    return NoTTS(_no_tts_hint("linux" if name in ("espeak", "spd") else platform))


def tts_name(cfg):
    """[voice] tts de la configuración ('auto' si no está)."""
    return ((cfg or {}).get("voice", {}) or {}).get("tts", "auto")


class _Item(tuple):
    """(prio, texto) con la hora en que se pidió: así un aviso que esperó demasiado se descarta en vez de sonar tarde."""
    t = 0.0


class Speaker:
    def __init__(self, voice=None, rate=190, popen=subprocess.Popen, enabled=True, on_event=None, backend=None,
                 run=subprocess.run, max_age_s=8.0):
        self.voice, self.rate, self.popen, self.enabled = voice, rate, popen, enabled
        self.max_age_s = max_age_s                       # un aviso informativo que espera más que esto ya no sirve: se descarta (0 = nunca)
        self.backend = backend or SayTTS()               # por defecto `say`, exactamente como antes
        self.run = run                                   # solo para `spd-say -C` al cortar
        if not self.backend.available:                   # sin motor: como --no-voice (no se dice nada)
            self.enabled = False
        self.on_event = on_event or (lambda *a: None)
        self.q = deque()
        self.cv = threading.Condition()
        self.proc = None
        self.killed = False
        self.held = False                      # el piloto está hablando: no se dice nada hasta soltar
        self.hold_q = []
        self.busy = False
        self.spoken = []                       # lo dicho (para auditoría y pruebas)
        self.dropped = []
        self._stop = False
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def say(self, text, prio=2, expires=True):
        if not self.enabled or not text:
            return False
        with self.cv:
            if self.held:
                if prio >= 3:
                    self.dropped.append((text, "piloto hablando"))
                    return False
                self.hold_q.append(self._item(prio, text, expires))
                return True
            if prio >= 3 and (self.busy or self.q):
                self.dropped.append((text, "ocupado"))
                return False
            if prio <= 1:
                self.q = deque(x for x in self.q if x[0] <= 1)
                self._kill()
                self.q.appendleft(self._item(prio, text, expires))
            else:
                if len(self.q) >= 3:
                    self.dropped.append((self.q.popleft()[1], "cola llena"))
                self.q.append(self._item(prio, text, expires))
            self.cv.notify()
        return True

    @staticmethod
    def _item(prio, text, expires=True):
        it = _Item((prio, text))
        it.t = time.monotonic()                          # monótono: un cambio de hora del sistema no descarta la cola
        it.expires = expires
        return it

    def hold(self):
        """El piloto empieza a hablar: corta lo que suena y retiene los avisos nuevos (el micrófono no debe oírlos)."""
        with self.cv:
            self.held = True
            for _, t in self.q:
                self.dropped.append((t, "interrumpido"))
            self.q.clear()
            self._kill()

    def release(self):
        """El piloto terminó: se dicen los avisos retenidos (máx. 3, los más recientes; lo informativo se descarta)."""
        with self.cv:
            self.held = False
            keep = [x for x in self.hold_q if x[0] <= 2][-3:]
            for _, t in self.hold_q:
                if (_, t) not in keep:
                    self.dropped.append((t, "retenido y viejo"))
            self.hold_q = []
            for it in keep:
                it.t = time.monotonic()                  # la espera mientras hablabas el piloto no cuenta como «viejo»
            self.q.extend(sorted(keep, key=lambda x: x[0]))
            self.cv.notify()

    def stop(self):
        """Corta la voz y vacía la cola (barge-in: vas a hablar tú)."""
        with self.cv:
            for _, t in self.q:
                self.dropped.append((t, "interrumpido"))
            self.q.clear()
            self._kill()

    def _kill(self):
        p = self.proc
        if p is not None and p.poll() is None:
            self.killed = True
            try:
                p.terminate()
            except OSError:
                pass
            cancel = getattr(self.backend, "cancel_command", None)
            if cancel:                                   # spd-say: el demonio seguiría hablando aunque muera el cliente
                try:                                     # sin esperar: se llama con el candado puesto (al pulsar para hablar)
                    self.popen(cancel(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except (OSError, subprocess.SubprocessError, TypeError):
                    pass

    def _run(self):
        while True:
            with self.cv:
                while not self.q and not self._stop:
                    self.busy = False
                    self.cv.notify_all()
                    self.cv.wait()
                if self._stop:
                    return
                item = self.q.popleft()
                prio, text = item
                stale = (prio >= 2 and self.max_age_s and getattr(item, "expires", True) and getattr(item, "t", 0)
                         and time.monotonic() - item.t > self.max_age_s)
                if stale:                                # sonaría tarde (sprint del 11 nov: «Contacto con X» 14 s después, detrás de un plan de 19 s)
                    self.dropped.append((text, "caducado"))
                    continue
                self.busy = True
            cmd, data = self.backend.command(to_speech(text), self.voice, self.rate)
            t0 = time.time()
            self.killed = False
            try:
                if data is None:
                    self.proc = self.popen(cmd)
                else:                                    # el texto por stdin (SAPI, espeak): nunca en la línea de comandos
                    self.proc = self.popen(cmd, stdin=subprocess.PIPE)
                    feed_stdin(self.proc, data)
                self.on_event("start", text, t0)
                self.proc.wait()
            except OSError as e:
                self.on_event("error", str(e), t0)
            (self.dropped if self.killed else self.spoken).append((text, "cortado") if self.killed else text)

    def wait(self, timeout=5.0):
        """Espera a que termine de hablar (pruebas / fin de sesión)."""
        end = time.time() + timeout
        with self.cv:
            while (self.q or self.busy) and time.time() < end:
                self.cv.wait(0.05)
        return not (self.q or self.busy)

    def close(self):
        with self.cv:
            self._stop = True
            self._kill()
            self.cv.notify_all()


def feed_stdin(proc, data):
    """Escribe el texto (UTF-8) y cierra stdin: el motor lee hasta el final. Si el proceso ya murió (cortado), no pasa nada."""
    try:
        proc.stdin.write(data.encode("utf-8"))
        proc.stdin.close()
    except (OSError, ValueError, AttributeError):
        pass
