import os, shutil, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from prune_check import COPIES, local_imports, free_port, wait_health
import ui_browser_check as ubc
box = tempfile.mkdtemp(prefix="pv-shot-")
missing = local_imports()
if missing: raise SystemExit("cannot boot: %s" % missing)
for name in COPIES:
    src = os.path.join(HERE, name)
    if os.path.exists(src): shutil.copy2(src, os.path.join(box, name))
ubc.build_log(box)
port = 8477
env = dict(os.environ, POOL_PORT=str(port), PYTHONIOENCODING="utf-8")
out = open(os.path.join(box, "server.out"), "w", encoding="utf-8")
srv = subprocess.Popen([sys.executable, os.path.join(box, "pool_server.py")],
                       cwd=box, env=env, stdout=out, stderr=subprocess.STDOUT)
out.close()
if not wait_health(port, srv):
    print(open(os.path.join(box, "server.out"), encoding="utf-8").read()[-600:]); raise SystemExit(1)
open(os.path.join(HERE, "shot.ok"), "w").write("http://127.0.0.1:%d/" % port)
time.sleep(7200)
