#!/usr/bin/env python3
"""F1 25 UDP: captura y reproduce telemetría (solo librería estándar).

  python3 f1_udp.py capture --port 20777 --out sesion.f1raw
  python3 f1_udp.py replay  sesion.f1raw --host 127.0.0.1 --port 20777 --speed 1.0

capture: escucha los paquetes del juego, los guarda tal cual y muestra un resumen.
replay : reenvía lo guardado con los tiempos originales, para desarrollar sin el juego.
"""
import argparse, socket, struct, sys, time

# Cabecera de 29 bytes de F1 23-25 (little endian). Verifícala contra la especificación oficial de EA.
HEADER = struct.Struct("<HBBBBBQfIIBB")
NAMES = {0: "Motion", 1: "Session", 2: "LapData", 3: "Event", 4: "Participants",
         5: "CarSetups", 6: "CarTelemetry", 7: "CarStatus", 8: "FinalClassification",
         9: "LobbyInfo", 10: "CarDamage", 11: "SessionHistory", 12: "TyreSets",
         13: "MotionEx", 14: "TimeTrial", 15: "LapPositions"}
MAGIC = b"F1RAW1\n"
REC = struct.Struct("<dH")  # segundos desde el inicio, longitud del paquete


def parse_header(data):
    if len(data) < HEADER.size:
        return None
    (fmt, year, major, minor, pver, pid, uid, stime, frame, oframe, player, second) = HEADER.unpack_from(data)
    return {"format": fmt, "year": year, "version": f"{major}.{minor}", "packet_version": pver,
            "id": pid, "session_uid": uid, "session_time": stime, "frame": frame,
            "player": player}


def summary(counts, last):
    parts = [f"{NAMES.get(k, k)}={v}" for k, v in sorted(counts.items())]
    extra = ""
    if last:
        extra = f" | formato {last['format']} v{last['version']} | jugador idx {last['player']} | t={last['session_time']:.1f}s"
    return ", ".join(parts) + extra


def capture(a):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((a.host, a.port))
    sock.settimeout(1.0)
    print(f"Escuchando UDP en {a.host}:{a.port} -> {a.out}  (Ctrl+C para parar)")
    counts, last, bad, t0, last_print, n = {}, None, 0, None, time.time(), 0
    with open(a.out, "wb") as f:
        f.write(MAGIC)
        try:
            while True:
                if a.seconds and t0 and time.time() - t0 > a.seconds:
                    break
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    if time.time() - last_print > 5:
                        print("Sin paquetes... revisa IP/puerto en el juego y el firewall.")
                        last_print = time.time()
                    continue
                now = time.time()
                if t0 is None:
                    t0 = now
                f.write(REC.pack(now - t0, len(data)) + data)
                n += 1
                h = parse_header(data)
                if h is None:
                    bad += 1
                    continue
                counts[h["id"]] = counts.get(h["id"], 0) + 1
                last = h
                if h["format"] != 2025 and counts[h["id"]] == 1:
                    print(f"Aviso: packetFormat={h['format']} (esperaba 2025). Revisa 'UDP Format' en el juego.")
                if now - last_print > 2:
                    print(summary(counts, last))
                    last_print = now
        except KeyboardInterrupt:
            pass
    print(f"\nTotal: {n} paquetes ({bad} ilegibles) guardados en {a.out}")
    print(summary(counts, last))


def replay(a):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with open(a.file, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            sys.exit("Archivo no válido (cabecera distinta).")
        start, n = time.time(), 0
        while True:
            raw = f.read(REC.size)
            if len(raw) < REC.size:
                break
            t, ln = REC.unpack(raw)
            data = f.read(ln)
            delay = start + t / a.speed - time.time()
            if delay > 0:
                time.sleep(delay)
            sock.sendto(data, (a.host, a.port))
            n += 1
    print(f"Reenviados {n} paquetes a {a.host}:{a.port}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--host", default="0.0.0.0")
    c.add_argument("--port", type=int, default=20777)
    c.add_argument("--out", default="sesion.f1raw")
    c.add_argument("--seconds", type=float, default=0, help="parar solo tras N segundos (0 = hasta Ctrl+C)")
    r = sub.add_parser("replay")
    r.add_argument("file")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=20777)
    r.add_argument("--speed", type=float, default=1.0)
    a = p.parse_args()
    capture(a) if a.cmd == "capture" else replay(a)
