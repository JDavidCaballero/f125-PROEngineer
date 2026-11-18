#!/usr/bin/env python3
"""Respuestas sensibles a la sesión (entrenos, clasificación, carrera) y preguntas generales sobre sesión, vueltas y pilotos.

Informe real (26 oct): en entrenos «vas P1 en la vuelta 6 de 1», «tu compañero va a 15264 s» y «no es una carrera» eran respuestas
absurdas, porque posiciones y huecos de carrera no significan nada allí. Aquí: fuera de carrera todo se compara por mejor vuelta.
"""
import practice as PR
import quali as Q
import strategy as S

SESSIONS = {1: "Libres 1", 2: "Libres 2", 3: "Libres 3", 4: "Práctica corta", 5: "Clasificación 1", 6: "Clasificación 2",
            7: "Clasificación 3", 8: "Clasificación corta", 9: "Clasificación de una vuelta", 10: "Sprint shootout 1",
            11: "Sprint shootout 2", 12: "Sprint shootout 3", 13: "Sprint shootout corto", 14: "Sprint shootout de una vuelta",
            15: "Carrera", 16: "Carrera 2", 17: "Carrera 3", 18: "Contrarreloj"}
KIND_NAME = {"race": "carrera", "quali": "clasificación", "practice": "entrenamiento", "tt": "contrarreloj", "other": "una sesión"}
RACE_ONLY = {"undercut", "overcut", "alternativa", "parar_ya", "viabilidad", "sugerir_objetivo", "objetivo_pos",
             "objetivo_pasar", "objetivo_estado", "objetivo_borrar", "gomas_mal", "dejar_pasar", "por_que_pierdo", "como_adelantar"}
PRACTICE_ANSWERS = {"estrategia", "elegir_compuesto", "compuesto"}


def lap_str(ms):
    return Q.fmt(ms)


SESSION_OF = lambda st: "los libres" if 1 <= st <= 4 else ("la clasificación" if 5 <= st <= 14 else "la carrera")


def forecast_text(ses, now_only=False):
    """El tiempo según el PRONÓSTICO del juego (paquete de sesión: muestras a 0/5/10/15/30… min). Informe del 11 nov: con lluvia,
    «¿cuándo para de llover?» respondía «no tengo ese dato» aunque el pronóstico venía en los paquetes."""
    from f1_parse import WEATHER
    if not ses:
        return "No tengo datos del clima."
    cur, st = ses.get("weather", 0), ses.get("session_type")
    fc = sorted((f for f in ses.get("forecast", []) if f["session_type"] == st), key=lambda f: f["minutes"])
    out = [f"Ahora {WEATHER.get(cur, '?')}."]
    later = [f for f in fc if f["minutes"] > 0]
    horizon = max((f["minutes"] for f in fc), default=0)
    if cur >= 3:
        stop = next((f for f in later if f["weather"] < 3), None)
        if stop:
            out.append(f"Según el pronóstico del juego, deja de llover en unos {stop['minutes']} min ({WEATHER.get(stop['weather'], '?')}).")
        elif horizon:
            heavy = next((f for f in later if f["weather"] > cur), None)
            out.append(f"Según el pronóstico del juego, sigue lloviendo al menos {horizon} min"
                       + (f", más fuerte en unos {heavy['minutes']} min." if heavy else "."))
        out.append("El juego no manda cuánta agua hay en la pista: cuando pare, se irá secando poco a poco.")
    else:
        rain = next((f for f in later if f["weather"] >= 3), None)
        if rain:
            out.append(f"Según el pronóstico del juego, {WEATHER.get(rain['weather'], 'lluvia')} en unos {rain['minutes']} min "
                       f"({rain['rain_pct']}% de probabilidad).")
        elif horizon:
            out.append(f"Sin lluvia en el pronóstico de los próximos {horizon} min "
                       f"(probabilidad máxima {max((f['rain_pct'] for f in fc), default=0)}%).")
    if not now_only:
        nxt = {}
        for f in ses.get("forecast", []):
            if f["session_type"] != st and f["session_type"] not in nxt:
                nxt[f["session_type"]] = f
        names = {}
        for t_, f in sorted(nxt.items()):
            names.setdefault(SESSION_OF(t_), f)
        if names:
            out.append(" ".join(f"Para {n}: {WEATHER.get(f['weather'], '?')}." for n, f in names.items()))
    return " ".join(out)


def wet_tyres(ses):
    """Neumático para mojado según la lluvia AHORA (regla general: intermedio con lluvia ligera, de lluvia con fuerte), o None si seco."""
    if not ses or ses.get("weather", 0) < 3:
        return None
    tyre = "intermedios" if ses["weather"] == 3 else "de lluvia"
    return (f"Con {'lluvia ligera' if ses['weather'] == 3 else 'lluvia fuerte'}, {tyre} (regla general; los de seco no tienen agarre). "
            + forecast_text(ses, now_only=True).split(". ", 1)[-1].split(" El juego no manda")[0])


class SessionAnswers:
    """Mixin de Assistant."""

    def _kind(self):
        return self.eng.tr.session_kind()

    def non_race_reply(self, intent):
        """Respuesta para intenciones de carrera cuando la sesión no es carrera, o None si no aplica."""
        k = self._kind()
        if k == "race" or intent.name not in RACE_ONLY | PRACTICE_ANSWERS:
            return None
        tr = self.eng.tr
        if intent.name in PRACTICE_ANSWERS:
            wet = wet_tyres(tr.ses) if intent.name != "estrategia" else None
            if wet:
                return wet
            comps = tuple(c for c in ((intent.compound,) if intent.compound else intent.comps) if c)
            lines = PR.compound_advice(tr, comps or None)
            if k == "practice" and not PR.compound_table(tr) and intent.name != "estrategia":
                lines.append("Para el plan de carrera, haz una tanda de 6 o más vueltas seguidas con cada compuesto que pienses "
                             "usar; empieza por el que creas que irá en carrera.")
            if intent.name == "estrategia":
                import raceplan
                plan, why = raceplan.build(self.eng)             # «¿qué estrategia para la carrera?» = un plan con lo medido, no una tabla
                return plan if plan else " ".join(PR.summary(tr)[:2] + lines) + f" Aún no puedo armar un plan de carrera: {why}."
            if not comps:
                return " ".join(lines)
            return " ".join(lines)
        if intent.name == "parar_ya":                         # «entro a boxes» en entrenos: es una afirmación, no una orden
            return "Entendido."
        return f"Eso es de carrera; ahora estamos en {KIND_NAME.get(k, 'otra sesión')}."

    # ------------------------------------------------------------ posición y huecos fuera de carrera
    def nr_position(self):
        q = PR.quali_estimate(self.eng.tr)
        if not q:
            return "Aún no tienes un tiempo válido en esta sesión."
        txt = f"Por mejor vuelta vas P{q['rank']} de {q['n']} ({lap_str(q['best_ms'])})"
        return txt + ("." if q["leader_is_me"] else f", a {q['gap_s']:.3f} s de {q['leader']}.")

    def nr_gap(self, idx, name):
        tr = self.eng.tr
        tab = {r["car"]: r for r in PR.rank_table(tr)}
        a, b = tab.get(tr.player), tab.get(idx)
        if b is None:
            return f"{name} aún no tiene un tiempo válido."
        if a is None:
            return f"{name} tiene su mejor vuelta en {lap_str(b['best_ms'])} (P{b['rank']}); tú aún no tienes tiempo válido."
        d = (b["best_ms"] - a["best_ms"]) / 1000
        return (f"{name}: mejor vuelta {lap_str(b['best_ms'])} (P{b['rank']}), "
                f"{abs(d):.3f} s {'más lenta' if d > 0 else 'más rápida'} que la tuya.")

    def nr_neighbour(self, step):
        tr = self.eng.tr
        tab = PR.rank_table(tr)
        me = next((r for r in tab if r["car"] == tr.player), None)
        if me is None:
            return "Aún no tienes un tiempo válido en esta sesión."
        other = next((r for r in tab if r["rank"] == me["rank"] + step), None)
        if other is None:
            return "No hay nadie " + ("delante: eres el más rápido." if step < 0 else "detrás por tiempo.")
        return self.nr_gap(other["car"], other["name"])

    # ------------------------------------------------------------ nuevas intenciones
    def t_plan_carrera(self, i):
        import raceplan
        eng = self.eng
        n = int(i.topic) if i.topic and str(i.topic).isdigit() else None
        tid = (eng.tr.ses or {}).get("track_id")
        if n and not (raceplan.MIN_RACE_LAPS <= n <= raceplan.MAX_RACE_LAPS):
            return f"{n} vueltas no me cuadra como distancia de carrera; dime otra cifra."
        if n and tid is not None:
            eng.memory.set_race_laps(tid, n)
        plan, why = raceplan.build(eng, n)
        if plan:
            return (f"Anotado: carrera de {n} vueltas. " if n else "") + plan
        return (f"Anotado: carrera de {n} vueltas. " if n else "") + f"Aún no puedo armar el plan: {why}."

    def t_como_adelantar(self, i):
        import coach
        tr = self.eng.tr
        car = i.driver[0] if i.driver else coach.car_ahead(tr)
        if car is None:
            return "Vas primero: no hay nadie delante a quien adelantar."
        txt = coach.how_to_pass(self.eng, car)
        return txt or "No tengo datos de ese piloto para aconsejarte."

    ANIMOS = ("Tú puedes, concéntrate en tu línea.", "Tranquilo, tienes el ritmo. Hazlo limpio.", "Respira y confía en el coche.",
              "Todo está bajo control: una curva a la vez.", "Vas bien. Sin prisa y sin errores.", "Confío en ti, piloto.")

    def t_animo(self, i):
        self._animo = (getattr(self, "_animo", -1) + 1) % len(self.ANIMOS)
        return self.ANIMOS[self._animo]

    def t_cerrar(self, i):
        return "Para cerrar el programa pulsa Ctrl+C en la terminal. Mientras tanto sigo escuchando."

    def t_sesion(self, i):
        from f1_parse import TRACKS, WEATHER
        ses = self.eng.tr.ses or {}
        st = ses.get("session_type")
        if st is None:
            return "Todavía no sé en qué sesión estamos."
        track = TRACKS.get(ses.get("track_id"), "")
        return (f"Estamos en {SESSIONS.get(st, f'sesión {st}')}" + (f" en {track}" if track else "") +
                f"; tiempo: {WEATHER.get(ses.get('weather'), '?')}.")

    def t_mejor_vuelta(self, i):
        tr = self.eng.tr
        tab = PR.rank_table(tr)
        if not tab:
            return "Aún no hay vueltas válidas en esta sesión."
        mine = next((r for r in tab if r["car"] == tr.player), None)
        if i.driver:
            r = next((r for r in tab if r["car"] == i.driver[0]), None)
            return (f"La mejor vuelta de {i.driver[1]} es {lap_str(r['best_ms'])} (P{r['rank']})." if r
                    else f"{i.driver[1]} aún no tiene un tiempo válido.")
        h = tr.hist.get(tr.player)
        last = next((l["ms"] for l in reversed(h["laps"]) if l["ms"] > 0), None) if h else None
        parts = []
        if i.topic == "sesion":
            lead = tab[0]
            parts.append(f"La mejor vuelta de la sesión es de {lead['name']}: {lap_str(lead['best_ms'])}.")
            if mine and mine["car"] != lead["car"]:
                parts.append(f"La tuya es {lap_str(mine['best_ms'])} (P{mine['rank']}, a {(mine['best_ms'] - lead['best_ms']) / 1000:.3f} s).")
            elif mine:
                parts.append("Es la tuya.")
            return " ".join(parts)
        if mine:
            parts.append(f"Tu mejor vuelta es {lap_str(mine['best_ms'])} (P{mine['rank']} de {len(tab)}).")
        else:
            parts.append("Aún no tienes una vuelta válida.")
        if last:
            parts.append(f"La última fue {lap_str(last)}.")
        return " ".join(parts)

    def t_piloto(self, i):
        tr = self.eng.tr
        car, name = (i.driver if i.driver else (None, None))
        kind = self._kind()
        if car is None and i.pos:
            if kind == "race":
                car = next((k for k, c in enumerate(tr.lap_all) if c and c["position"] == i.pos and c["result_status"] == 2), None)
            else:
                r = next((r for r in PR.rank_table(tr) if r["rank"] == i.pos), None)
                car = r["car"] if r else None
            if car is None:
                return f"No tengo quién va P{i.pos} todavía."
            name = tr.name(car)
        if car is None:
            return "¿De qué piloto?"
        if car == tr.player:
            return self.t_mejor_vuelta(type(i)("mejor_vuelta"))
        parts = []
        if kind == "race":
            la = tr.lap_all[car]
            parts.append(f"{name} va P{la['position']}." if la else f"{name} no está en carrera.")
            g = tr.rel_gap(car)
            if g is not None:
                parts.append(f"Está a {abs(g):.1f} s {'delante' if g < 0 else 'detrás'} de ti.")
        else:
            r = next((r for r in PR.rank_table(tr) if r["car"] == car), None)
            parts.append(f"{name}: P{r['rank']} por mejor vuelta ({lap_str(r['best_ms'])})." if r else f"{name} aún no tiene un tiempo válido.")
        sa = tr.sta_all[car] if car < len(tr.sta_all) else None
        if sa:
            parts.append(f"Lleva {S.NAME.get(sa['visual_compound'], '?')} con {sa['tyre_age_laps']} vueltas.")
        return " ".join(parts)

    def t_actualizacion(self, i):
        tr = self.eng.tr
        if self._kind() == "race":
            parts = [self.t_posicion(i)]
            a, b = tr.neighbours()
            for n in (a, b):
                if n:
                    parts.append(self._hueco(n[0], tr.name(n[0])))
            return " ".join(parts)
        parts = [self.nr_position()]
        last, _ = self._my_laps()
        if last:
            parts.append(f"Tu última vuelta medida: {lap_str(last.time_ms)}.")
            parts += Q.ers_report(last)[:1]
        return " ".join(parts)

    def t_ack(self, i):
        return "Entendido."
