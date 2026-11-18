"""Configuración: config.local.toml se mezcla sobre config.toml sin tocarlo, y `config.py` ayuda a comprobar el servidor del modelo."""
import contextlib, io, shutil, tempfile, unittest
from pathlib import Path

import helpers  # noqa: F401  (pone la raíz del proyecto en sys.path)
import config as C
import llm


class LocalOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(C.ROOT / "config.toml", self.tmp / "config.toml")
        self._root = C.ROOT
        C.ROOT = self.tmp

    def tearDown(self):
        C.ROOT = self._root

    def write(self, text):
        (self.tmp / C.LOCAL).write_text(text, encoding="utf-8")

    def test_without_a_local_file_the_defaults_are_used(self):
        cfg = C.load()
        self.assertEqual(cfg["llm"]["url"], "http://127.0.0.1:1234/v1")

    def test_local_values_override_only_what_they_name(self):
        self.write('[llm]\nurl = "http://192.168.1.50:1234/v1"\nmodel = "otro/modelo"\n')
        cfg = C.load()
        self.assertEqual((cfg["llm"]["url"], cfg["llm"]["model"]), ("http://192.168.1.50:1234/v1", "otro/modelo"))
        self.assertEqual(cfg["llm"]["timeout_s"], 12)                     # lo demás de [llm] sigue siendo el de serie
        self.assertEqual(cfg["network"]["port"], 20777)                   # y las demás secciones, intactas

    def test_a_new_section_or_key_is_added(self):
        self.write('[paths]\nexport_dir = ""\n[engineer]\nmode = "conservador"\n')
        cfg = C.load()
        self.assertEqual(cfg["paths"]["export_dir"], "")
        self.assertEqual(cfg["paths"]["record_raw"], True)
        self.assertEqual(cfg["engineer"]["mode"], "conservador")

    def test_an_explicit_config_path_ignores_the_local_file(self):
        self.write('[llm]\nurl = "http://x/v1"\n')
        self.assertEqual(C.load(self.tmp / "config.toml")["llm"]["url"], "http://127.0.0.1:1234/v1")

    def test_use_local_false_ignores_it_too(self):
        self.write('[llm]\nurl = "http://x/v1"\n')
        self.assertEqual(C.load(use_local=False)["llm"]["url"], "http://127.0.0.1:1234/v1")

    def test_a_broken_local_file_gives_a_clear_message_not_a_traceback(self):
        self.write("[llm\nurl = ")
        with self.assertRaises(SystemExit) as cm:
            C.load()
        self.assertIn("config.local.toml no es un TOML válido", str(cm.exception))

    def test_the_example_file_is_valid_and_overrides_nothing_surprising(self):
        import tomllib
        ex = tomllib.loads((self._root / "config.local.example.toml").read_text(encoding="utf-8"))
        base = C.load()
        self.assertEqual(ex["llm"]["url"], base["llm"]["url"])
        self.assertEqual(ex["llm"]["model"], base["llm"]["model"])

    def test_local_file_is_not_tracked_by_git(self):
        self.assertIn("config.local.toml", (self._root / ".gitignore").read_text(encoding="utf-8"))


class ShowCommand(unittest.TestCase):
    def run_show(self, models=None, error=None, model="qwen/qwen3.5-9b", enabled=True):
        def fake_models(self_):
            if error:
                raise llm.LLMError(error)
            return models
        old = llm.LLMClient.models, C.load
        llm.LLMClient.models = fake_models
        C.load = lambda *a, **k: {"llm": {"enabled": enabled, "url": "http://h:1/v1", "model": model, "timeout_s": 3},
                                  "network": {"port": 20777}, "engineer": {"mode": "normal", "game_year": 2025}}
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = C.show()
        finally:
            llm.LLMClient.models, C.load = old
        return code, out.getvalue()

    def test_it_lists_the_models_the_server_offers_and_marks_the_chosen_one(self):
        code, out = self.run_show(models=["qwen/qwen3.5-9b", "otro/modelo"])
        self.assertEqual(code, 0)
        self.assertIn("* qwen/qwen3.5-9b", out)
        self.assertIn("  otro/modelo", out)

    def test_a_model_name_not_offered_is_flagged(self):
        code, out = self.run_show(models=["otro/modelo"])
        self.assertEqual(code, 1)
        self.assertIn("no está en la lista", out)

    def test_an_empty_model_name_is_fine(self):
        self.assertEqual(self.run_show(models=["a"], model="")[0], 0)

    def test_a_server_that_does_not_answer_says_what_to_check(self):
        code, out = self.run_show(error="se acabó el tiempo")
        self.assertEqual(code, 1)
        self.assertIn("NO responde", out)
        self.assertIn("Serve on Local Network", out)

    def test_disabled_model_is_explained(self):
        code, out = self.run_show(enabled=False)
        self.assertEqual(code, 0)
        self.assertIn("solo con reglas", out)


class ClientModels(unittest.TestCase):
    def test_models_lists_ids(self):
        c = llm.LLMClient(transport=lambda p: {"data": [{"id": "a"}, {"id": "b"}, {"x": 1}]})
        c._post = lambda path, payload=None: {"data": [{"id": "a"}, {"id": "b"}, {"x": 1}]}
        self.assertEqual(c.models(), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
