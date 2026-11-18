import json, subprocess, sys, tempfile, threading, unittest
from pathlib import Path

from helpers import ROOT, demo
import blackbox as BB
import voice_audit as VA


class Capture(unittest.TestCase):
    def test_console_errors_threads_and_meta_are_recorded(self):
        folder = Path(tempfile.mkdtemp()) / "s"
        out0, err0 = sys.stdout, sys.stderr
        bb = BB.Session(folder, ["voice.py", "lab"], "[llm]\nenabled = false\n")
        print("hola consola")
        t = threading.Thread(target=lambda: 1 / 0, name="hilo-x")
        t.start()
        t.join()
        bb.stop()
        self.assertIs(sys.stdout, out0)
        self.assertIs(sys.stderr, err0)
        log = (folder / "console.log").read_text()
        self.assertIn("hola consola", log)
        self.assertIn("ZeroDivisionError", log)
        self.assertIn("hilo-x", log)
        meta = json.loads((folder / "meta.json").read_text())
        self.assertEqual(meta["argv"], ["voice.py", "lab"])
        self.assertIn("python", meta)
        self.assertIn("sounddevice", meta["packages"])
        self.assertIn("enabled = false", meta["config_toml"])

    def test_errors_in_extracts_tracebacks_and_failures(self):
        c = ("normal\nTraceback (most recent call last):\n  File \"x.py\", line 1\n    boom()\nValueError: mala\notra línea\n"
             "FALLA micrófono (1 s)  sin permiso\n")
        e = BB.errors_in(c)
        self.assertEqual(len(e), 2)
        self.assertIn("ValueError: mala", e[0])
        self.assertIn("FALLA micrófono", e[1])

    def test_pack_is_one_text_file_without_audio_and_without_huge_tables(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "audio").mkdir()
        (folder / "audio" / "utt_001.wav").write_bytes(b"RIFF" + b"\0" * 1000)
        (folder / "console.log").write_text("línea 1\nTraceback (most recent call last):\n  x\nRuntimeError: caída\n")
        (folder / "meta.json").write_text(json.dumps({"argv": ["voice.py"], "python": "3.13", "config_toml": "[x]"}, indent=1))
        (folder / "voice.jsonl").write_text(json.dumps({"type": "utt", "n": 1, "understood": True, "intent": "si", "source": "reglas",
                                                       "raw": "sí", "stt": {"latency_s": 0.4}, "t_nlu_s": 0.0, "t_total_s": 0.5}) + "\n")
        (folder / "engine.jsonl").write_text(json.dumps({"type": "strategy", "lap": 3, "report": {"options": ["x" * 10000]},
                                                         "summary": {"a": 1}, "decision": {"opcion": "A"}}) + "\n" +
                                             json.dumps({"type": "otro"}) + "\n")
        p = BB.pack(folder, lambda f: VA.render(VA.summarize([f])))
        txt = p.read_text()
        for sec in ("SISTEMA Y COMANDO", "AUDITORÍA", "ERRORES", "CONSOLA", "REGISTRO DE VOZ", "DECISIONES DEL INGENIERO"):
            self.assertIn(sec, txt)
        self.assertIn("RuntimeError: caída", txt)
        self.assertNotIn("RIFF", txt)
        self.assertNotIn("x" * 100, txt)               # la tabla de opciones no se vuelca
        self.assertNotIn('"otro"', txt)
        self.assertLess(len(txt), 20000)

    def test_progress_bars_do_not_bury_the_results(self):
        folder = Path(tempfile.mkdtemp())
        bars = "\n".join(f"Downloading bytes: ██▍       |  {k}MB, 22.4MB/s" for k in range(300))
        (folder / "console.log").write_text("OK    voz española instalada           0.6s  Mónica\n" + bars +
                                            "\nFALLA viaje voz a texto            29.6s  FileNotFoundError: ffmpeg\n")
        (folder / "meta.json").write_text("{}")
        txt = BB.pack(folder).read_text()
        self.assertIn("RESULTADO DE LAS COMPROBACIONES", txt)
        self.assertIn("voz española instalada", txt.split("=== ERRORES")[0])      # visible aunque la consola se llenó de barras
        self.assertLess(txt.count("Downloading bytes"), 3)
        self.assertIn("ffmpeg", txt)

    def test_audit_accepts_a_session_folder(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "voice.jsonl").write_text(json.dumps({"type": "utt", "n": 1, "understood": True, "intent": "si", "source": "reglas",
                                                       "raw": "sí", "reject": None, "stt": {"latency_s": 0.2}, "t_nlu_s": 0.0,
                                                       "t_total_s": 0.3}) + "\n")
        self.assertEqual(VA.summarize([folder])["utterances"], 1)
        self.assertEqual(VA.summarize([Path(tempfile.mkdtemp())])["utterances"], 0)


class EndToEnd(unittest.TestCase):
    def test_voice_lab_in_a_subprocess_leaves_everything_in_one_folder(self):
        runs = Path(tempfile.mkdtemp())
        cfgp = Path(tempfile.mkdtemp()) / "c.toml"
        cfgp.write_text((ROOT / "config.toml").read_text().replace('runs = "runs"', f'runs = "{runs}"'))
        r = subprocess.run([sys.executable, str(ROOT / "voice.py"), "lab", "--text", "--no-voice", "--config", str(cfgp), "--file",
                            str(demo("corta")[0]), "--speed", "500", "--start-at", "320"],
                           input="qué hacemos\nqwer asdf\n/quit\n", capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        folders = list(runs.glob("session-*-lab"))
        self.assertEqual(len(folders), 1)
        f = folders[0]
        for name in ("console.log", "meta.json", "voice.jsonl", "engine.jsonl", "INFORME.txt"):
            self.assertTrue((f / name).exists(), name)
        self.assertIn("INFORME.txt", r.stdout)
        txt = (f / "INFORME.txt").read_text()
        self.assertIn("REGISTRO DE VOZ", txt)
        self.assertIn("qwer asdf", txt)

    def test_a_crash_is_recorded_in_the_report(self):
        runs = Path(tempfile.mkdtemp())
        cfgp = Path(tempfile.mkdtemp()) / "c.toml"
        cfgp.write_text((ROOT / "config.toml").read_text().replace('runs = "runs"', f'runs = "{runs}"'))
        r = subprocess.run([sys.executable, str(ROOT / "voice.py"), "script", "--text", "--no-voice", "--config", str(cfgp),
                            "--file", "/no/existe.f1raw"], capture_output=True, text=True, timeout=60, cwd=str(ROOT))
        self.assertNotEqual(r.returncode, 0)
        f = next(runs.glob("session-*-script"))
        txt = (f / "INFORME.txt").read_text()
        self.assertIn("ERRORES", txt)
        self.assertTrue("FileNotFoundError" in txt or "No such file" in txt)


if __name__ == "__main__":
    unittest.main()
