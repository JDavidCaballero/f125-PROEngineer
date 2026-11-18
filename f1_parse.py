#!/usr/bin/env python3
"""Parser de paquetes F1 25 (solo librería estándar) + visor de una captura .f1raw.

  python3 f1_parse.py captures/prueba1.f1raw

Los diseños se validaron contra una captura real de F1 25 v1.26 (tamaños de los 15 tipos de
paquete y valores de LapData/Session/Participants). Importable:
  from f1_parse import header, lap_data, car_telemetry, car_status, car_damage, session, event,
                       participants, tyre_sets, session_history, final_classification, read_capture

Orden de ruedas en todos los arreglos de 4: [trasero izq, trasero der, delantero izq, delantero der].
"""
import struct, sys

HS = 29  # tamaño de la cabecera
MAGIC = b"F1RAW1\n"
REC = struct.Struct("<dH")
HEADER = struct.Struct("<HBBBBBQfIIBB")
LAP = struct.Struct("<IIHBHBHBHBfffBBBBBBBBBBBBBBBHHBfB")          # 57 bytes por coche
TEL = struct.Struct("<HfffBbHBBH4H4B4BH4f4B")                        # 60
STA = struct.Struct("<BBBBBfffHHBBHBBBbfffBfffB")                    # 55
DMG = struct.Struct("<4f4B4B4B18B")                                  # 46
SES = struct.Struct("<BbbBHBbBHHBB")                                 # primeros 13 bytes de Session
PART = struct.Struct("<BBBBBBB32sBBHBB12s")                          # 57 bytes por coche
TSET = struct.Struct("<BBBBBBBhB")                                   # 10 bytes por juego de neumáticos
HIST_LAP = struct.Struct("<IHBHBHBB")                                # 14 bytes por vuelta
FINAL = struct.Struct("<BBBBBBBIdBBB8B8B8B")                         # 46 bytes por coche
NCARS = 22

PID = {0: "Motion", 1: "Session", 2: "LapData", 3: "Event", 4: "Participants", 5: "CarSetups",
       6: "CarTelemetry", 7: "CarStatus", 8: "FinalClassification", 9: "LobbyInfo", 10: "CarDamage",
       11: "SessionHistory", 12: "TyreSets", 13: "MotionEx", 14: "TimeTrial", 15: "LapPositions"}
# Neumático visible (el que ves en pantalla). Los ids reales 16-22 (C5..C0/C6) son 'actual_compound'.
VISUAL = {16: "Blando", 17: "Medio", 18: "Duro", 7: "Intermedio", 8: "Lluvia"}
ACTUAL = {16: "C5", 17: "C4", 18: "C3", 19: "C2", 20: "C1", 21: "C0", 22: "C6", 7: "Inter", 8: "Lluvia"}
RACE_TYPES = {15, 16, 17}
TIME_TRIAL = 18
WEATHER = {0: "despejado", 1: "poco nublado", 2: "nublado", 3: "lluvia ligera", 4: "lluvia fuerte", 5: "tormenta"}
TRACKS = {0: "Melbourne", 2: "Shanghái", 3: "Sakhir", 4: "Barcelona", 5: "Mónaco", 6: "Montreal",
          7: "Silverstone", 9: "Hungría", 10: "Spa", 11: "Monza", 12: "Singapur", 13: "Suzuka",
          14: "Abu Dabi", 15: "Texas", 16: "Brasil", 17: "Austria", 19: "México", 20: "Bakú",
          26: "Zandvoort", 27: "Imola", 29: "Jeddah", 30: "Miami", 31: "Las Vegas", 32: "Catar"}


def header(d):
    v = HEADER.unpack_from(d)
    return {"format": v[0], "year": v[1], "version": f"{v[2]}.{v[3]}", "id": v[5], "session_uid": v[6],
            "session_time": v[7], "frame": v[8], "player": v[10], "secondary": v[11]}


def _ms(ms_part, minutes):
    return minutes * 60000 + ms_part


def lap_data(d, i):
    v = LAP.unpack_from(d, HS + i * LAP.size)
    return {
        "last_ms": v[0], "current_ms": v[1],
        "sector1_ms": _ms(v[2], v[3]), "sector2_ms": _ms(v[4], v[5]),
        "delta_front_ms": _ms(v[6], v[7]), "delta_leader_ms": _ms(v[8], v[9]),
        "lap_distance": v[10], "distance": v[10], "total_distance": v[11], "sc_delta": v[12],
        "position": v[13], "lap": v[14], "pit_status": v[15], "pit_stops": v[16],
        "sector": v[17],                      # 0, 1, 2 = sector 1, 2, 3
        "lap_invalid": bool(v[18]), "penalties_s": v[19], "warnings": v[20], "corner_cuts": v[21],
        "unserved_dt": v[22], "unserved_sg": v[23], "grid": v[24],
        "driver_status": v[25],               # 0 garaje, 1 vuelta de vuelo, 2 in-lap, 3 out-lap, 4 en pista
        "result_status": v[26],               # 2 activo, 3 terminó, 4 DNF, 5 descalificado, 6 retirado
        "pit_lane_timer": bool(v[27]), "pit_lane_ms": v[28], "pit_stop_ms": v[29],
    }


def car_telemetry(d, i):
    v = TEL.unpack_from(d, HS + i * TEL.size)
    return {"speed": v[0], "throttle": v[1], "steer": v[2], "brake": v[3], "gear": v[5], "rpm": v[6],
            "drs": v[7], "rev_lights": v[8], "brake_temp": v[10:14], "tyre_surface_temp": v[14:18],
            "tyre_inner_temp": v[18:22], "engine_temp": v[22], "tyre_pressure": v[23:27]}


def car_status(d, i):
    v = STA.unpack_from(d, HS + i * STA.size)
    return {"fuel_mix": v[2], "brake_bias": v[3], "pit_limiter": bool(v[4]),
            "fuel_kg": v[5], "fuel_capacity": v[6], "fuel_laps": v[7],
            "drs_allowed": bool(v[11]), "drs_distance": v[12],
            "actual_compound": v[13], "visual_compound": v[14], "tyre_age_laps": v[15], "fia_flag": v[16],
            "ers_store_j": v[19], "ers_mode": v[20],       # 0 nada, 1 media, 2 hotlap, 3 adelantamiento
            "ers_harvest_k_j": v[21], "ers_harvest_h_j": v[22], "ers_deployed_j": v[23]}


def car_damage(d, i):
    v = DMG.unpack_from(d, HS + i * DMG.size)
    return {"tyre_wear_pct": v[0:4], "tyre_damage_pct": v[4:8], "brake_damage_pct": v[8:12],
            "tyre_blisters_pct": v[12:16], "front_left_wing": v[16], "front_right_wing": v[17],
            "rear_wing": v[18], "floor": v[19], "diffuser": v[20], "sidepod": v[21],
            "drs_fault": bool(v[22]), "ers_fault": bool(v[23]), "gearbox": v[24], "engine": v[25],
            "mguh_wear": v[26], "es_wear": v[27], "ce_wear": v[28], "ice_wear": v[29],
            "mguk_wear": v[30], "tc_wear": v[31], "engine_blown": bool(v[32]), "engine_seized": bool(v[33])}


def tyre_extra_damage(dm):
    """Daño de neumático que NO es desgaste. En la captura real (Singapur) tyre_damage_pct == tyre_wear_pct en todas las muestras:
    el juego lo repite, así que solo cuenta como daño lo que supera al desgaste (p. ej. un pinchazo o un golpe)."""
    return [max(0.0, d - w) for d, w in zip(dm["tyre_damage_pct"], dm["tyre_wear_pct"])]


def session(d):
    """Campos de Session. Offsets desde el inicio del cuerpo (tras la cabecera), según la especificación
    de F1 24/25; el total (753 bytes) y los valores de la captura real los confirman."""
    b = d[HS:]
    v = SES.unpack_from(b)
    n_fc = min(b[126], 64)
    forecast = []
    for k in range(n_fc):
        s = struct.unpack_from("<BBBbbbbB", b, 127 + 8 * k)
        forecast.append({"session_type": s[0], "minutes": s[1], "weather": s[2], "track_temp": s[3],
                         "air_temp": s[5], "rain_pct": s[7]})
    zones = []
    for k in range(min(b[18], 21)):
        start, flag = struct.unpack_from("<fb", b, 19 + 5 * k)
        zones.append((start, flag))
    return {"weather": v[0], "track_temp": v[1], "air_temp": v[2], "total_laps": v[3],
            "track_length_m": v[4], "session_type": v[5], "track_id": v[6], "formula": v[7],
            "time_left_s": v[8], "duration_s": v[9], "pit_speed_limit": v[10], "paused": bool(v[11]),
            "marshal_zones": zones,
            "safety_car_status": b[124],          # 0 ninguno, 1 SC, 2 VSC, 3 vuelta de formación
            "forecast": forecast,
            "pit_window_ideal": b[653], "pit_window_latest": b[654], "pit_rejoin_pos": b[655],
            "session_length": b[671], "num_sc": b[676], "num_vsc": b[677], "num_red": b[678],
            "safety_car_setting": b[696],         # 0 apagado, 1 reducido, 2 normal, 3 aumentado
            "sector2_start_m": struct.unpack_from("<f", b, 716)[0],
            "sector3_start_m": struct.unpack_from("<f", b, 720)[0]}


def event(d):
    code = d[HS:HS + 4].decode("ascii", "replace")
    b = d[HS + 4:]
    out = {"code": code}
    try:
        if code == "FTLP":
            out["car"], out["time_s"] = struct.unpack_from("<Bf", b)
        elif code in ("RTMT", "TMPT", "RCWN", "DTSV", "SGSV"):
            out["car"] = b[0]
        elif code == "PENA":
            (out["penalty_type"], out["infringement"], out["car"], out["other_car"], out["time_s"],
             out["lap"], out["places_gained"]) = struct.unpack_from("<7B", b)
        elif code == "OVTK":
            out["car"], out["overtaken"] = b[0], b[1]
        elif code == "SCAR":
            out["sc_type"], out["sc_event"] = b[0], b[1]   # evento: 0 sale, 1 vuelve, 2 entra, 3 reanuda carrera
        elif code == "COLL":
            out["car"], out["other_car"] = b[0], b[1]
        elif code == "SPTP":
            out["car"], out["speed"] = struct.unpack_from("<Bf", b)
        elif code == "STLG":
            out["lights"] = b[0]
        elif code == "BUTN":
            out["buttons"] = struct.unpack_from("<I", b)[0]          # botones pulsados del mando (máscara de bits)
    except struct.error:
        pass
    return out


def participants(d):
    n = d[HS]
    cars = []
    for i in range(NCARS):
        v = PART.unpack_from(d, HS + 1 + i * PART.size)
        name = v[7].split(b"\0", 1)[0].decode("utf-8", "replace")
        cars.append({"idx": i, "ai": bool(v[0]), "driver_id": v[1], "team_id": v[3], "number": v[5],
                     "name": name or f"coche {i}"})
    return {"active": n, "cars": cars}


def tyre_sets(d):
    """Juegos de neumáticos de UN coche (el paquete rota entre coches). `usable_life` en vueltas."""
    car = d[HS]
    sets = []
    for k in range(20):
        v = TSET.unpack_from(d, HS + 1 + k * TSET.size)
        sets.append({"actual": v[0], "visual": v[1], "wear": v[2], "available": bool(v[3]),
                     "life_span": v[5], "usable_life": v[6], "lap_delta_ms": v[7], "fitted": bool(v[8])})
    return {"car": car, "sets": sets, "fitted_idx": d[HS + 1 + 20 * TSET.size]}


def session_history(d):
    """Historial de vueltas de UN coche (rota entre coches). Tiempos en ms; 0 = vuelta sin tiempo."""
    car, n_laps, n_stints, best = d[HS], d[HS + 1], d[HS + 2], d[HS + 3]
    laps = []
    for k in range(min(n_laps, 100)):
        v = HIST_LAP.unpack_from(d, HS + 7 + k * HIST_LAP.size)
        laps.append({"ms": v[0], "s1": _ms(v[1], v[2]), "s2": _ms(v[3], v[4]), "s3": _ms(v[5], v[6]),
                     "valid": bool(v[7] & 1)})
    stints = []
    base = HS + 7 + 100 * HIST_LAP.size
    for k in range(min(n_stints, 8)):
        end_lap, actual, visual = struct.unpack_from("<BBB", d, base + 3 * k)
        stints.append({"end_lap": end_lap, "actual": actual, "visual": visual})
    return {"car": car, "laps": laps, "stints": stints, "best_lap": best}


def final_classification(d):
    n = d[HS]
    cars = []
    for i in range(min(n, NCARS)):
        v = FINAL.unpack_from(d, HS + 1 + i * FINAL.size)
        cars.append({"idx": i, "position": v[0], "laps": v[1], "grid": v[2], "points": v[3], "pit_stops": v[4],
                     "result_status": v[5], "best_lap_ms": v[7], "total_time_s": v[8],
                     "penalty_s": v[9]})
    return {"cars": cars}


def read_capture(path):
    """Lee una captura .f1raw (o .f1raw.gz, la versión comprimida que deja `lab --live`)."""
    import gzip
    with (gzip.open(path, "rb") if str(path).endswith(".gz") else open(path, "rb")) as f:
        if f.read(len(MAGIC)) != MAGIC:
            sys.exit("Archivo no válido.")
        while True:
            raw = f.read(REC.size)
            if len(raw) < REC.size:
                return
            t, n = REC.unpack(raw)
            data = f.read(n)
            if len(data) == n and n >= HS:
                yield t, data


def mmss(ms):
    return "-:--.---" if not ms else f"{ms // 60000}:{ms % 60000 / 1000:06.3f}"


def main():
    if len(sys.argv) < 2:
        sys.exit("Uso: python3 f1_parse.py ruta/captura.f1raw")
    st, last_print = {}, -99.0
    print(f"{'t(s)':>6} {'vta':>3} {'pos':>3} {'km/h':>4} {'M':>2} {'rpm':>5} {'comb kg(vueltas)':>17} "
          f"{'neum':<10}{'edad':>4} {'desg%':>5} {'ERS%':>4}  vuelta actual / última")
    for t, d in read_capture(sys.argv[1]):
        h = header(d)
        i = h["player"]
        if h["id"] == 2:
            st["lap"] = lap_data(d, i)
        elif h["id"] == 6:
            st["tel"] = car_telemetry(d, i)
        elif h["id"] == 7:
            st["sta"] = car_status(d, i)
        elif h["id"] == 10:
            st["dmg"] = car_damage(d, i)
        if t - last_print >= 1.0 and {"lap", "tel", "sta"} <= st.keys():
            lp, te, sa = st["lap"], st["tel"], st["sta"]
            wear = max(st["dmg"]["tyre_wear_pct"]) if "dmg" in st else 0
            comp = VISUAL.get(sa["visual_compound"], str(sa["visual_compound"]))
            fuel = f"{sa['fuel_kg']:.1f} ({sa['fuel_laps']:.1f})"
            print(f"{t:6.1f} {lp['lap']:>3} {lp['position']:>3} {te['speed']:>4} {te['gear']:>2} {te['rpm']:>5} "
                  f"{fuel:>17} {comp:<10}{sa['tyre_age_laps']:>4} {wear:>5.1f} "
                  f"{sa['ers_store_j'] / 4_000_000 * 100:>4.0f}  {mmss(lp['current_ms'])} / {mmss(lp['last_ms'])}")
            last_print = t


if __name__ == "__main__":
    main()
