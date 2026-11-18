"""Utilidades comunes de las pruebas: ruta del proyecto, demos temporales y captura real opcional."""
import os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import make_demo  # noqa: E402
import config as C  # noqa: E402

# captura real opcional (una carrera grabada con f1_udp.py o udp.f1raw): export F1_REAL_CAPTURE=/ruta/captura.f1raw; sin ella esas pruebas se omiten
REAL = Path(os.environ.get("F1_REAL_CAPTURE", str(ROOT / "captures" / "real.f1raw")))
_DEMOS = {}
_TMP = tempfile.TemporaryDirectory(prefix="f1demo-")


def demo(name):
    """Genera (una sola vez por ejecución) la captura sintética y devuelve (ruta, info)."""
    if name not in _DEMOS:
        path = Path(_TMP.name) / f"demo_{name}.f1raw"
        _DEMOS[name] = (path, make_demo.generate(name, path))
    return _DEMOS[name]


def cfg(primary=False):
    """Configuración de las pruebas. Las pruebas del LLM como cerebro piden cfg(primary=True); el resto, el modo clásico."""
    c = C.load(use_local=False)                     # las pruebas no dependen de tu config.local.toml
    c.setdefault("llm", {})["enabled"] = False
    c["llm"]["primary"] = primary
    return c


def run_engineer(path, mode="normal", client=None, profile=None):
    """Corre el ingeniero sobre una captura y devuelve (lista de mensajes, Engineer)."""
    import engineer as E
    from f1_parse import read_capture
    lines = []
    eng = E.Engineer(cfg(), mode, client, out=lines.append, sync=True, profile=profile)
    for t, d in read_capture(path):
        eng.on_packet(t, d)
    return lines, eng
