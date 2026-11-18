"""En Windows el texto se guarda por defecto en cp1252: sin encoding="utf-8" un «≤» o «¿» tumbaba el programa al arrancar
(prueba real en Windows, 12 nov 2026: UnicodeEncodeError en blackbox.py). Esta prueba vigila que no vuelva a pasar."""
import re, unittest
from pathlib import Path

import helpers  # noqa: F401
import config as C

CALL = re.compile(r"\.(write_text|read_text)\(|(?<![\w.])open\(|text=True")


class Utf8Everywhere(unittest.TestCase):
    def test_every_text_file_access_names_its_encoding(self):
        bad = []
        for f in sorted(C.ROOT.glob("*.py")):
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#")[0]
                if not CALL.search(code) or "encoding=" in code:
                    continue
                if re.search(r"""["'][rwa]b["']|gzip\.open|urlopen|wave\.open|def read_text|read_text\(sess""", code):
                    continue                                   # binario, o el modo texto de voice.py (no es un archivo)
                bad.append(f"{f.name}:{n}: {line.strip()}")
        self.assertEqual(bad, [], "abre el archivo con encoding=\"utf-8\":\n" + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
