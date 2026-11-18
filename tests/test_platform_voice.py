"""Voz por plataforma (macOS say, Windows SAPI, Linux espeak-ng/spd-say) con procesos simulados: no suena nada ni hace falta
ningún motor instalado. Lo que se comprueba es el comando exacto, que el texto vaya por stdin cuando toca y el orden de respaldo."""
import argparse, contextlib, io, subprocess, time, unittest

import helpers  # noqa: F401  (añade la carpeta del proyecto al path)

import speaker as SP
import stt as STT
import voice as V
from speech import to_speech

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


class FakeStdin:
    def __init__(self, broken=False):
        self.data, self.closed, self.broken = b"", False, broken

    def write(self, b):
        if self.broken:
            raise BrokenPipeError()
        self.data += b

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, cmd, kw, dur):
        self.cmd, self.kw, self.dur, self.t0, self.dead = cmd, kw, dur, time.time(), False
        self.stdin = FakeStdin() if kw.get("stdin") == subprocess.PIPE else None

    def poll(self):
        return 0 if (self.dead or time.time() - self.t0 >= self.dur) else None

    def wait(self):
        while self.poll() is None:
            time.sleep(0.005)

    def terminate(self):
        self.dead = True


class Recorder:
    """popen falso que guarda cada proceso lanzado."""
    def __init__(self, dur=0.01, first=None):
        self.procs, self.dur, self.first = [], dur, first

    def __call__(self, cmd, **kw):
        p = FakeProc(cmd, kw, self.first if (self.first and not self.procs) else self.dur)
        self.procs.append(p)
        return p


def which_of(*present):
    table = {"powershell": PS, "espeak-ng": "/usr/bin/espeak-ng", "espeak": "/usr/bin/espeak", "spd-say": "/usr/bin/spd-say",
             "xdg-open": "/usr/bin/xdg-open"}
    return lambda name: table[name] if name in present else None


def speak(backend, text, voice=None, dur=0.01, **kw):
    rec = Recorder(dur)
    s = SP.Speaker(voice=voice, popen=rec, backend=backend, **kw)
    s.say(text, 2)
    s.wait(2)
    s.close()
    return rec.procs


class Choice(unittest.TestCase):
    def test_mac_is_always_say_even_without_looking_for_it(self):
        self.assertIsInstance(SP.choose_backend("auto", "darwin", which_of()), SP.SayTTS)

    def test_windows_uses_sapi_when_powershell_exists(self):
        self.assertIsInstance(SP.choose_backend("auto", "win32", which_of("powershell")), SP.SapiTTS)
        b = SP.choose_backend("auto", "win32", which_of())
        self.assertFalse(b.available)
        self.assertIn("PowerShell", b.hint)

    def test_linux_order_espeak_ng_then_espeak_then_spd_then_none(self):
        b = SP.choose_backend("auto", "linux", which_of("espeak-ng", "espeak", "spd-say"))
        self.assertEqual((b.name, b.exe), ("espeak", "/usr/bin/espeak-ng"))
        b = SP.choose_backend("auto", "linux", which_of("espeak", "spd-say"))
        self.assertEqual((b.name, b.exe), ("espeak", "/usr/bin/espeak"))
        b = SP.choose_backend("auto", "linux", which_of("spd-say"))
        self.assertEqual((b.name, b.exe), ("spd", "/usr/bin/spd-say"))
        b = SP.choose_backend("auto", "linux", which_of())
        self.assertEqual(b.name, "none")
        self.assertIn("sudo apt install espeak-ng", b.hint)

    def test_config_forces_a_backend(self):
        self.assertEqual(SP.choose_backend("spd", "linux", which_of("espeak-ng", "spd-say")).name, "spd")
        self.assertEqual(SP.choose_backend("say", "linux", which_of()).name, "say")
        self.assertEqual(SP.choose_backend("none", "darwin", which_of()).name, "none")
        self.assertEqual(SP.choose_backend("sapi", "linux", which_of()).name, "none")
        self.assertEqual(SP.choose_backend("espeak", "win32", which_of("powershell")).name, "none")
        self.assertEqual(SP.tts_name({"voice": {"tts": "espeak"}}), "espeak")
        self.assertEqual(SP.tts_name({}), "auto")


class MacUnchanged(unittest.TestCase):
    def test_default_speaker_runs_say_exactly_as_before(self):
        text = "Box en 2 vueltas, P3"
        procs = speak(None, text, voice="Mónica")
        self.assertEqual(procs[0].cmd, ["say", "-r", "190", "-v", "Mónica", to_speech(text)])
        self.assertEqual(procs[0].kw, {})                       # sin stdin: popen(cmd), como siempre
        procs = speak(SP.SayTTS(), "hola")
        self.assertEqual(procs[0].cmd, ["say", "-r", "190", "hola"])

    def test_say_synth_command_is_unchanged(self):
        calls = []
        V.synth_wav("hola", "Mónica", "/tmp/x.wav", run=lambda cmd, **kw: calls.append((cmd, kw)))
        self.assertEqual(calls, [(["say", "-v", "Mónica", "-o", "/tmp/x.wav", "--file-format=WAVE", "--data-format=LEI16@16000",
                                   "hola"], {"check": True, "timeout": 30})])

    def test_make_speaker_on_mac_finds_the_spanish_voice_with_say(self):
        class Out:
            stdout = "Mónica              es_ES    # Hola\n"
        seen = []
        args = argparse.Namespace(no_voice=False, voice=None)
        s = V.make_speaker(args, {}, platform="darwin", run=lambda cmd, **kw: (seen.append(cmd), Out)[1])
        self.assertEqual((s.backend.name, s.voice, s.enabled), ("say", "Mónica", True))
        self.assertEqual(seen, [["say", "-v", "?"]])
        s.close()


class Windows(unittest.TestCase):
    def test_text_goes_by_stdin_never_in_the_command_line(self):
        text = 'Para en 3 vueltas"; Remove-Item C:\\ -Recurse; "'
        procs = speak(SP.SapiTTS(), text, voice="Microsoft Helena Desktop")
        p = procs[0]
        self.assertEqual(p.cmd[:4], ["powershell", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertEqual(len(p.cmd), 5)
        self.assertNotIn("Remove-Item", " ".join(p.cmd))
        self.assertNotIn("vueltas", " ".join(p.cmd))
        self.assertEqual(p.kw, {"stdin": subprocess.PIPE})
        self.assertTrue(p.stdin.closed)
        self.assertEqual(p.stdin.data.decode("utf-8"), f"Microsoft Helena Desktop\n1\n{to_speech(text)}")

    def test_script_is_one_line_without_double_quotes(self):
        for stmts in (SP.SAPI_SPEAK, SP.SAPI_SYNTH, SP.SAPI_LIST):
            script = SP._ps(*stmts)[-1]
            self.assertNotIn('"', script)
            self.assertNotIn("\n", script)
        self.assertIn("ReadToEnd", SP._ps(*SP.SAPI_SPEAK)[-1])
        self.assertIn("UTF8Encoding", SP._ps(*SP.SAPI_SPEAK)[-1])

    def test_without_voice_the_first_line_is_empty_and_accents_survive(self):
        p = speak(SP.SapiTTS(), "¿Cómo van las gomas?")[0]
        self.assertEqual(p.stdin.data.decode("utf-8"), "\n1\n¿Cómo van las gomas?")

    def test_rate_maps_words_per_minute_to_sapi_range(self):
        self.assertEqual(SP.sapi_rate(180), 0)
        self.assertEqual(SP.sapi_rate(190), 1)
        self.assertEqual(SP.sapi_rate(250), 4)
        self.assertEqual(SP.sapi_rate(0), -10)
        self.assertEqual(SP.sapi_rate(2000), 10)

    def test_spanish_voice_selection(self):
        out = ("Microsoft David Desktop|en-US\nMicrosoft Sabina Desktop|es-MX\nMicrosoft Helena Desktop|es-ES\n"
               "Microsoft Zira Desktop|en-US\n")
        self.assertEqual(SP.parse_sapi_voices(out), "Microsoft Helena Desktop")
        self.assertEqual(SP.parse_sapi_voices("Microsoft Sabina Desktop|es-MX\nX|en-US"), "Microsoft Sabina Desktop")
        self.assertEqual(SP.parse_sapi_voices("Voz Rara|es-AR\n"), "Voz Rara")
        self.assertIsNone(SP.parse_sapi_voices("Microsoft David Desktop|en-US\n"))
        self.assertIsNone(SP.parse_sapi_voices(""))

    def test_find_voice_runs_powershell_and_decodes_utf8(self):
        class Out:
            stdout = "Voz Ñandú|es-ES\r\n".encode("utf-8")
        seen = []
        v = SP.SapiTTS().find_voice(lambda cmd, **kw: (seen.append((cmd, kw)), Out)[1])
        self.assertEqual(v, "Voz Ñandú")
        self.assertEqual(seen[0][0][0], "powershell")
        self.assertIn("GetInstalledVoices", seen[0][0][-1])
        self.assertIsNone(SP.SapiTTS().find_voice(lambda *a, **k: (_ for _ in ()).throw(OSError())))
        self.assertIsNone(SP.SapiTTS().find_voice(lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 1))))

    def test_synth_passes_path_and_text_by_stdin(self):
        calls = []
        V.synth_wav("Sí", "Helena", r"C:\t\t.wav", SP.SapiTTS(), run=lambda cmd, **kw: calls.append((cmd, kw)))
        cmd, kw = calls[0]
        self.assertNotIn("t.wav", " ".join(cmd))
        self.assertIn("SetOutputToWaveFile", cmd[-1])
        self.assertEqual(kw["input"].decode("utf-8"), "Helena\n0\nC:\\t\\t.wav\nSí")

    def test_interrupting_terminates_the_powershell_process(self):
        rec = Recorder(first=5.0)
        s = SP.Speaker(popen=rec, backend=SP.SapiTTS())
        s.say("una explicación muy larga", 2)
        for _ in range(200):
            if rec.procs:
                break
            time.sleep(0.005)
        s.say("¡Bandera amarilla!", 1)
        s.wait(2)
        s.close()
        self.assertTrue(rec.procs[0].dead)
        self.assertIn(("una explicación muy larga", "cortado"), s.dropped)
        self.assertIn("¡Bandera amarilla!", s.spoken)
        self.assertTrue(rec.procs[1].stdin.data.decode("utf-8").endswith(to_speech("¡Bandera amarilla!")))

    def test_a_dead_process_while_writing_stdin_does_not_break_the_speaker(self):
        class Dead:
            stdin = FakeStdin(broken=True)
        SP.feed_stdin(Dead(), "hola")                            # no lanza
        SP.feed_stdin(object(), "hola")                           # sin stdin: tampoco


class Linux(unittest.TestCase):
    def test_espeak_command_with_text_by_stdin(self):
        p = speak(SP.EspeakTTS("/usr/bin/espeak-ng"), "Quedan 12 vueltas")[0]
        self.assertEqual(p.cmd, ["/usr/bin/espeak-ng", "-v", "es", "-s", "190", "--stdin"])
        self.assertEqual(p.stdin.data.decode("utf-8"), to_speech("Quedan 12 vueltas"))
        p = speak(SP.EspeakTTS("/usr/bin/espeak-ng"), "hola", voice="es-419")[0]
        self.assertEqual(p.cmd[1:3], ["-v", "es-419"])

    def test_espeak_voice_list_parsing(self):
        out = ("Pty Language       Age/Gender VoiceName          File                 Other Languages\n"
               " 5  es-419          --/M      Spanish_(Latin_America) roa/es-419    (es-mx 6)\n"
               " 5  es              --/M      Spanish_(Spain)    roa/es               \n")
        self.assertEqual(SP.parse_espeak_voices(out), "es")
        self.assertEqual(SP.parse_espeak_voices(out.splitlines()[0] + "\n" + out.splitlines()[1]), "es-419")
        self.assertIsNone(SP.parse_espeak_voices("Pty Language Age/Gender VoiceName File\n"))
        self.assertIsNone(SP.EspeakTTS().find_voice(lambda *a, **k: (_ for _ in ()).throw(OSError())))

    def test_espeak_synth(self):
        calls = []
        V.synth_wav("hola", None, "/tmp/a.wav", SP.EspeakTTS("/usr/bin/espeak-ng"), run=lambda cmd, **kw: calls.append((cmd, kw)))
        self.assertEqual(calls[0][0], ["/usr/bin/espeak-ng", "-v", "es", "-w", "/tmp/a.wav", "--stdin"])
        self.assertEqual(calls[0][1]["input"], b"hola")

    def test_spd_say_waits_and_separates_text_from_options(self):
        p = speak(SP.SpdTTS("/usr/bin/spd-say"), "-3 segundos")[0]
        self.assertEqual(p.cmd, ["/usr/bin/spd-say", "-w", "-l", "es", "-r", "5", "--", to_speech("-3 segundos")])
        self.assertEqual(p.kw, {})

    def test_spd_say_cancel_on_interrupt(self):
        rec, runs = Recorder(dur=5.0), []
        s = SP.Speaker(popen=rec, backend=SP.SpdTTS("/usr/bin/spd-say"), run=lambda cmd, **kw: runs.append(cmd))
        s.say("larga", 2)
        for _ in range(200):
            if rec.procs:
                break
            time.sleep(0.005)
        s.stop()
        s.wait(2)
        s.close()
        self.assertTrue(rec.procs[0].dead)
        self.assertIn(["/usr/bin/spd-say", "-C"], [p.cmd for p in rec.procs])   # se lanza sin esperar (no bloquea el corte)
        self.assertEqual(runs, [])

    def test_spd_cannot_synth_test_audio(self):
        with self.assertRaises(RuntimeError):
            V.synth_wav("hola", None, "/tmp/a.wav", SP.SpdTTS(), run=lambda *a, **k: None)


class NoBackend(unittest.TestCase):
    def test_speaks_nothing_and_never_launches_a_process(self):
        rec = Recorder()
        s = SP.Speaker(popen=rec, backend=SP.NoTTS("instala algo"))
        self.assertFalse(s.enabled)
        self.assertFalse(s.say("hola", 1))
        s.wait(1)
        s.close()
        self.assertEqual(rec.procs, [])

    def test_make_speaker_explains_once_and_keeps_running(self):
        args = argparse.Namespace(no_voice=False, voice=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            s = V.make_speaker(args, {}, which=which_of(), platform="linux", popen=Recorder())
        self.assertFalse(s.enabled)
        self.assertIn("sudo apt install espeak-ng", out.getvalue())
        self.assertEqual(out.getvalue().count("Aviso:"), 1)
        s.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):                    # con --no-voice no hace falta avisar
            V.make_speaker(argparse.Namespace(no_voice=True, voice=None), {}, which=which_of(), platform="linux").close()
        self.assertEqual(out.getvalue(), "")

    def test_make_speaker_on_linux_picks_espeak_and_its_spanish_voice(self):
        class Out:
            stdout = "Pty Language Age/Gender VoiceName File\n 5  es  --/M  Spanish roa/es\n"
        args = argparse.Namespace(no_voice=False, voice=None)
        s = V.make_speaker(args, {"voice": {"tts": "auto"}}, which=which_of("espeak-ng"), platform="linux",
                           popen=Recorder(), run=lambda *a, **k: Out)
        self.assertEqual((s.backend.name, s.voice, s.enabled), ("espeak", "es", True))
        s.close()


class OpenFolder(unittest.TestCase):
    def test_each_platform_opens_with_its_tool_and_never_fails(self):
        calls = []
        pop = lambda cmd, **kw: calls.append(cmd)                 # noqa: E731
        self.assertTrue(V.open_folder("/x", "darwin", popen=pop))
        self.assertTrue(V.open_folder("/x", "win32", popen=pop, startfile=lambda p: calls.append(("startfile", p))))
        self.assertTrue(V.open_folder("/x", "linux", popen=pop, which=which_of("xdg-open")))
        self.assertEqual(calls, [["open", "/x"], ("startfile", "/x"), ["xdg-open", "/x"]])
        self.assertFalse(V.open_folder("/x", "linux", popen=pop, which=which_of()))

        def boom(*a, **k):
            raise OSError("no")
        self.assertFalse(V.open_folder("/x", "darwin", popen=boom))
        self.assertFalse(V.open_folder("/x", "win32", startfile=boom))


class STTChoice(unittest.TestCase):
    def setUp(self):
        self.orig = STT.MlxWhisperSTT, STT.FasterWhisperSTT
        self.tried = []
        test = self

        class Mlx:
            name = "mlx"

            def __init__(self, model):
                test.tried.append(("mlx", model))
                if test.mlx_fails:
                    raise ImportError("no mlx")

        class Faster:
            name = "faster"

            def __init__(self, model):
                test.tried.append(("faster", model))
                if test.faster_fails:
                    raise ImportError("no faster_whisper")
        STT.MlxWhisperSTT, STT.FasterWhisperSTT = Mlx, Faster
        self.mlx_fails = self.faster_fails = False

    def tearDown(self):
        STT.MlxWhisperSTT, STT.FasterWhisperSTT = self.orig

    def test_apple_silicon_tries_mlx_first(self):
        self.assertEqual(STT.make_stt("auto", "small", "darwin", "arm64").name, "mlx")
        self.mlx_fails = True
        self.assertEqual(STT.make_stt("auto", "small", "darwin", "arm64").name, "faster")
        self.assertEqual(self.tried, [("mlx", "small"), ("mlx", "small"), ("faster", "small")])

    def test_elsewhere_auto_is_faster_only(self):
        for plat, mach in (("linux", "x86_64"), ("win32", "AMD64"), ("darwin", "x86_64")):
            self.tried = []
            self.assertEqual(STT.make_stt("auto", "small", plat, mach).name, "faster")
            self.assertEqual(self.tried, [("faster", "small")])

    def test_clear_error_names_the_installer_of_each_platform(self):
        self.mlx_fails = self.faster_fails = True
        for plat, script in (("linux", "setup_linux.sh"), ("win32", "setup_windows.ps1"), ("darwin", "setup_mac.sh")):
            with self.assertRaises(RuntimeError) as e:
                STT.make_stt("auto", "small", plat, "x86_64")
            self.assertIn(script, str(e.exception))
            self.assertIn("faster", str(e.exception))

    def test_default_model_and_helpers(self):
        self.assertEqual(STT.default_model("darwin"), "medium")
        self.assertEqual(STT.default_model("linux"), "small")
        self.assertEqual(STT.default_model("win32"), "small")
        self.assertTrue(STT.is_apple_silicon("darwin", "arm64"))
        self.assertFalse(STT.is_apple_silicon("linux", "arm64"))


if __name__ == "__main__":
    unittest.main()
