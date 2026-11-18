import sys, re, collections, time
sys.path.insert(0, "tests"); sys.path.insert(0, ".")
import helpers
from helpers import demo, cfg
import engineer as E
from voice import read_capture
def norm(t): return re.sub(r"\d+([.,]\d+)?", "#", t)
for name in sys.argv[1:]:
    t0 = time.time(); path, info = demo(name); out = []
    eng = E.Engineer(cfg(False), "normal", None, out=out.append, sync=True)
    msgs = []
    eng.on_say.append(lambda text, prio: msgs.append((eng.tr.cur_lap or 0, prio, text)))
    for t, d in read_capture(path): eng.on_packet(t, d)
    laps = max([m[0] for m in msgs] or [1])
    c = collections.Counter(norm(m[2]) for m in msgs)
    byl = collections.Counter(m[0] for m in msgs)
    print(f"== {name}: {len(msgs)} avisos en {laps} vueltas ({len(msgs)/max(laps,1):.1f}/vuelta, máx {max(byl.values() or [0])} en una) [{time.time()-t0:.0f}s]")
    for k, n in c.most_common(6):
        if n > 2: print(f"   x{n}  {k[:90]}")
