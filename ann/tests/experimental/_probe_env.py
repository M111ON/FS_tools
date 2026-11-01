import os, subprocess, sys, json
d = r"I:\tools\ann\build\_zone_ab\old"
env = {**os.environ,
       "ANN_INDEX_PATH": os.path.join(d, "ann_index.jsonl"),
       "ANN_TAGS_PATH": os.path.join(d, "ann_tags.tsv"),
       "ANN_ANCH_PATH": os.path.join(d, "ann.bin"),
       "ANN_ORDER_PATH": os.path.join(d, "order.bin"),
       "ANN_ROSTER_PATH": os.path.join(d, "roster.bin"),
       "ANN_ROUTE_LOG": os.path.join(d, "route.log"),
       "ANN_PORT": "8103", "ANN_TOTEM_WARM": "0"}
bad = [k for k in env if not isinstance(k, str) or not isinstance(env[k], str)]
print("non-str:", bad)
print("weird keys:", [k for k in env if k.startswith("=") or "\x00" in k])
exe = r"I:\tools\ann\build\ann_search_nozr.exe"
print("exe exists:", os.path.exists(exe), "workdir exists:", os.path.isdir(d))
try:
    p = subprocess.Popen([exe, "--serve", "8103"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import time; time.sleep(2); print("alive", p.poll()); p.kill()
except Exception as e:
    print("FAIL", type(e).__name__, e)
    e2 = dict(env)
    for k in list(e2):
        if k.startswith("="):
            del e2[k]
    print("retry without '=' keys, removed:", [k for k in env if k.startswith("=")])
    try:
        p = subprocess.Popen([exe, "--serve", "8103"], env=e2,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import time; time.sleep(2); print("alive2", p.poll()); p.kill()
    except Exception as e3:
        print("FAIL2", type(e3).__name__, e3)
