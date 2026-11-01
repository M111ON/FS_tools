import importlib.util, os, sys, traceback
spec = importlib.util.spec_from_file_location("bz", r"I:\tools\ann\bench_zone_ab.py")
bz = importlib.util.module_from_spec(spec); spec.loader.exec_module(bz)
print("REPO", bz.REPO)
print("WORK", bz.WORK)
print("EXES", bz.EXES, {k: os.path.exists(v) for k, v in bz.EXES.items()})
bz.setup()
for d in ("old", "new"):
    print(d, "dir?", os.path.isdir(os.path.join(bz.WORK, d)),
          "index?", os.path.exists(os.path.join(bz.WORK, d, "ann_index.jsonl")))
try:
    p, h = bz.start("old")
    print("started", h.get("entries"))
    bz.stop(p)
except Exception:
    traceback.print_exc()
