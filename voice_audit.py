#!/usr/bin/env python3
"""Resumen de auditoría de las sesiones de voz (texto corto para pegármelo; no incluye audio).

  python voice.py report runs/voice-*.jsonl
Lee los registros de `voice.py lab` y `voice.py script` y calcula: cuánto entiende, cuánto tarda, qué falla y por qué.
"""
import json, re, statistics
from collections import Counter
from pathlib import Path


def load(paths):
    recs = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            p = p / "voice.jsonl"
            if not p.exists():
                continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except ValueError:
                        pass
    return recs


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[min(int(q * len(xs)), len(xs) - 1)]


def lat(xs):
    xs = [x for x in xs if x is not None]
    return {"n": len(xs), "p50": pct(xs, 0.5), "p95": pct(xs, 0.95), "max": max(xs) if xs else None}


_DIG = {"1": "uno", "2": "dos", "3": "tres", "4": "cuatro", "5": "cinco", "6": "seis", "7": "siete", "8": "ocho", "9": "nueve",
        "10": "diez"}


def words(s):
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFD", (s or "").lower()) if unicodedata.category(c) != "Mn")
    return [_DIG.get(w, w) for w in re.findall(r"\w+", s)]


def summarize(paths):
    recs = load(paths)
    utts = [r for r in recs if r.get("type") == "utt"]
    scripts = [r for r in recs if r.get("type") == "script"]
    out = {"files": len(paths), "utterances": len(utts), "script_phrases": len(scripts)}
    if utts:
        out["understood_pct"] = round(100 * sum(u["understood"] for u in utts) / len(utts))
        out["rejected_by_stt"] = dict(Counter(u["reject"] for u in utts if u.get("reject")))
        out["intents"] = dict(Counter(u["intent"] for u in utts if u.get("intent")).most_common())
        out["intent_source"] = dict(Counter(u["source"] for u in utts if u.get("source")))
        out["latency_stt_s"] = lat([(u.get("stt") or {}).get("latency_s") for u in utts])
        out["latency_nlu_s"] = lat([u.get("t_nlu_s") for u in utts])
        out["latency_total_s"] = lat([u.get("t_total_s") for u in utts])
        weak = sorted((u for u in utts if (u.get("stt") or {}).get("avg_logprob") is not None),
                      key=lambda u: u["stt"]["avg_logprob"])[:8]
        out["lowest_confidence"] = [{"n": u["n"], "heard": u.get("raw"), "logprob": round(u["stt"]["avg_logprob"], 2),
                                     "understood": u["understood"]} for u in weak]
        run = longest = 0
        for u in utts:
            run = 0 if u["understood"] else run + 1
            longest = max(longest, run)
        out["longest_run_not_understood"] = longest
        out["followed_by_repeat"] = sum(1 for a, b in zip(utts, utts[1:]) if not a["understood"] and b["understood"])
    radio = [r for r in recs if r.get("type") == "radio"]
    taps = [r for r in recs if r.get("type") == "tap"]
    out["fallback"] = {
        "entered": sum(1 for r in radio if any((a.get("text") or "").startswith("No te entiendo") for a in r["actions"])),
        "accepted_by_tap": sum(1 for r in radio if r.get("source") == "toque" and any(a["kind"] == "accept" for a in r["actions"])),
        "declined_by_silence": sum(1 for r in radio if r.get("source") == "silencio" and any(a["kind"] == "decline" for a in r["actions"])),
        "taps_total": len(taps), "taps_ignored": sum(1 for t in taps if not (t["state"] == "fallback" and t["pending"]))}
    if scripts:
        ok = [s for s in scripts if s["ok"]]
        out["script"] = {"n": len(scripts), "ok": len(ok), "ok_pct": round(100 * len(ok) / len(scripts)),
                         "mean_similarity": round(statistics.mean(s["sim"] for s in scripts), 2),
                         "rejected_by_stt": sum(1 for s in scripts if s.get("reject")),
                         "latency_stt_s": lat([s.get("stt_latency_s") for s in scripts])}
        by = {}
        for s in scripts:
            d = by.setdefault(s["expected_intent"], [0, 0])
            d[0] += 1
            d[1] += s["ok"]
        out["script"]["by_intent"] = {k: f"{v[1]}/{v[0]}" for k, v in sorted(by.items(), key=lambda kv: kv[1][1] / kv[1][0])}
        out["script"]["failures"] = [{"expected": s["expected"], "heard": s.get("heard") or s.get("raw"), "got": s.get("got"),
                                      "wanted": s["expected_intent"], "reject": s.get("reject")} for s in scripts if not s["ok"]][:20]
        mis = Counter()
        for s in scripts:
            exp, got = words(s["expected"]), set(words(s.get("raw")))
            for w in exp:
                if len(w) >= 4 and w not in got:
                    mis[w] += 1
        out["script"]["most_missed_words"] = dict(mis.most_common(10))
        drv = [s for s in scripts if s.get("expected_driver")]
        if drv:
            out["script"]["driver_names"] = f"{sum(1 for s in drv if s['ok'])}/{len(drv)} bien"
    return out


def render(s):
    L = [f"== AUDITORÍA DE VOZ == archivos {s['files']} | frases libres {s['utterances']} | frases de guion {s['script_phrases']}"]
    if s["utterances"]:
        L.append(f"Entendidas (intención reconocida): {s['understood_pct']}%   descartadas por el motor de voz: {s['rejected_by_stt'] or 'ninguna'}")
        L.append(f"Intenciones: {s['intents']}   origen: {s['intent_source']}")
        for k, n in (("latency_stt_s", "voz->texto"), ("latency_nlu_s", "intención+consulta"), ("latency_total_s", "TOTAL soltar botón->respuesta")):
            v = s[k]
            if v["n"]:
                L.append(f"Latencia {n}: mediana {v['p50']:.2f}s, p95 {v['p95']:.2f}s, peor {v['max']:.2f}s (n={v['n']})")
        L.append(f"Racha más larga sin entender: {s['longest_run_not_understood']}   repetidas con éxito: {s['followed_by_repeat']}")
        L.append("Menor confianza del motor de voz:")
        for w in s["lowest_confidence"][:6]:
            L.append(f"   #{w['n']} logprob {w['logprob']}  oyó «{w['heard']}»  entendida={w['understood']}")
    f = s["fallback"]
    L.append(f"Canal de respaldo: entradas {f['entered']}, confirmadas con toque {f['accepted_by_tap']}, "
             f"descartadas por silencio {f['declined_by_silence']}; toques {f['taps_total']} (ignorados {f['taps_ignored']})")
    if "script" in s:
        sc = s["script"]
        L.append(f"GUION: {sc['ok']}/{sc['n']} bien ({sc['ok_pct']}%), similitud media {sc['mean_similarity']}, "
                 f"descartadas por el motor {sc['rejected_by_stt']}, voz->texto mediana {sc['latency_stt_s']['p50'] or 0:.2f}s")
        L.append(f"  por intención (peor primero): {sc['by_intent']}")
        if sc.get("driver_names"):
            L.append(f"  nombres de pilotos: {sc['driver_names']}")
        L.append(f"  palabras que más se pierde: {sc['most_missed_words']}")
        for x in sc["failures"][:12]:
            L.append(f"  FALLO esperaba «{x['expected']}» ({x['wanted']}) | oyó «{x['heard']}» -> {x['got']}" +
                     (f" [{x['reject']}]" if x["reject"] else ""))
    return "\n".join(L)
