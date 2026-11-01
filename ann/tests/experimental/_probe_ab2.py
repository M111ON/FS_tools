import os, subprocess, sys, time, json, shutil
REPO = r"I:\tools\ann"
WORK = os.path.join(os.environ.get("TEMP", REPO), "ann_zone_ab")
d = os.path.join(WORK, "old")
print("TEMP", os.environ.get("TEMP"))
print("WORK", WORK, os.path.isdir(WORK))
print("d", d, os.path.isdir(d))
exe = os.path.join(REPO, "build", "ann_search_nozr.exe")
print("exe", exe, os.path.exists(exe))
errpath = os.path.join(d, "srv.log")
print("errpath dir exists", os.path.isdir(os.path.dirname(errpath)))
env = {**os.environ,
       "ANN_INDEX_PATH": os.path.join(d, "ann_index.jsonl"),
       "ANN_TAGS_PATH": os.path.join(d, "ann_tags.tsv"),
       "ANN_ANCH_PATH": os.path.join(d, "ann.bin"),
       "ANN_ORDER_PATH": os.path.join(d, "order.bin"),
       "ANN_ROSTER_PATH": os.path.join(d, "roster.bin"),
       "ANN_ROUTE_LOG": os.path.join(d, "route.log"),
       "ANN_PORT": "8103", "ANN_TOTEM_WARM": "0"}
try:
    err = open(errpath, "a", encoding="utf-8")
    print("opened err ok")
except Exception as e:
    print("err open FAILED:", e)
    raise SystemExit(1)
try:
    p = subprocess.Popen([exe, "--serve", "8103"], env=env,
                         stdout=subprocess.DEVNULL, stderr=err)
    print("popen ok", p.pid)
    time.sleep(3)
    print("poll", p.poll())
    p.kill()
except Exception as e:
    print("popen FAILED:", type(e).__name__, e)
