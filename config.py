#!/usr/bin/env python3
"""Carga config.toml (+ config.local.toml personal) con tomllib (Python 3.11+) y el perfil aprendido data/profile.json."""
import json, tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent


LOCAL = "config.local.toml"


def _merge(base, over):
    """Mezcla recursiva: lo de `over` gana, y las secciones ([llm], [paths]...) se combinan clave a clave en vez de sustituirse."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def utf8_console():
    """En Windows la consola y los archivos usan por defecto cp1252: un «≤» o «¿» al imprimir podía tumbar el programa (prueba
    real en Windows, 12 nov 2026). La salida pasa a UTF-8 y lo que la consola no pueda mostrar se sustituye en vez de fallar."""
    import sys
    for s in (sys.stdin, sys.stdout, sys.stderr):           # también lo que escribes (modo texto): «¿» no debe llegar roto
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def load(path=None, use_local=True):
    """config.toml, más tus cambios personales de config.local.toml (junto al anterior, fuera de git) si existe.
    Con una ruta explícita (--config) se usa ese archivo tal cual, sin mezclar nada."""
    p = Path(path) if path else ROOT / "config.toml"
    with open(p, "rb") as f:
        cfg = tomllib.load(f)
    local = ROOT / LOCAL
    if not path and use_local and local.exists():
        try:
            with open(local, "rb") as f:
                cfg = _merge(cfg, tomllib.load(f))
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"{LOCAL} no es un TOML válido ({e}). Corrígelo o bórralo; el resto de la configuración no se toca.")
    return cfg


def memory_path(cfg=None):
    """Archivo de la memoria de entrenos/clasificación (solo se usa con el juego en vivo: las repeticiones no la tocan)."""
    rel = (cfg or {}).get("paths", {}).get("memory", "data/memory.json")
    if not rel:
        return None
    p = Path(rel).expanduser()
    return p if p.is_absolute() else ROOT / p


def load_profile(cfg=None):
    rel = (cfg or {}).get("paths", {}).get("profile", "data/profile.json")
    p = Path(rel)
    p = p if p.is_absolute() else ROOT / p
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"tracks": {}}


def save_profile(profile, cfg=None):
    rel = (cfg or {}).get("paths", {}).get("profile", "data/profile.json")
    p = Path(rel)
    p = p if p.is_absolute() else ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profile, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def show():
    """`python3 config.py`: qué configuración se usa de verdad y si el servidor del modelo responde (con los nombres de modelo disponibles)."""
    import llm
    cfg = load()
    local = (ROOT / LOCAL).exists()
    print(f"Configuración: config.toml{' + ' + LOCAL if local else ' (no hay ' + LOCAL + ': se usan los valores de serie)'}")
    c = cfg.get("llm", {})
    print(f"  [network] puerto UDP del juego : {cfg.get('network', {}).get('port')}")
    print(f"  [llm] enabled / url / model    : {c.get('enabled')} / {c.get('url')} / {c.get('model') or '(el cargado en el servidor)'}")
    print(f"  [engineer] mode / game_year    : {cfg.get('engineer', {}).get('mode')} / {cfg.get('engineer', {}).get('game_year')}")
    if not c.get("enabled"):
        print("El modelo está desactivado (enabled = false): el programa funciona solo con reglas.")
        return 0
    client = llm.LLMClient(c.get("url", ""), c.get("model", ""), timeout=min(c.get("timeout_s", 12), 5))
    try:
        ids = client.models()
    except llm.LLMError as e:
        print(f"\nEl servidor NO responde en {c.get('url')}: {e}\n"
              "  - ¿está encendido y con un modelo cargado? - ¿la URL termina en /v1? - si es otro equipo: ¿\"Serve on Local Network\" y el cortafuegos?")
        return 1
    print(f"\nEl servidor responde. Modelos disponibles ({len(ids)}):")
    for i in ids:
        print(f"  {'*' if i == c.get('model') else ' '} {i}")
    if c.get("model") and c["model"] not in ids:
        print(f"\nOJO: tu `model` ({c['model']!r}) no está en la lista; copia uno de arriba tal cual (o déjalo vacío para usar el cargado).")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(show())
