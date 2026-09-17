"""ui_check.py — does the dashboard's JavaScript actually parse?

Written because it did not, once, and nothing noticed: a single misplaced brace
in an added function (an object literal starting with `(`) turned the whole
<script> block into a parse error, so every handler silently disappeared while
the server kept answering 200 and the Python self-check kept passing 94/94.
The browser console said `SyntaxError` and nothing else did.

This does not need a server, a browser, or a token: it imports the page out of
pool_server.py, extracts the script block, and hands it to a real parser. Node
is used when present because that is the same engine the agent runs on; if
node is missing the check says so and exits 2 rather than pretending to pass.

    python ui_check.py            # check the dashboard as it stands in the file
    python ui_check.py --url http://localhost:8390/   # check what is actually served
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

DIR = os.path.dirname(os.path.abspath(__file__))


def extract_script(html):
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    if not blocks:
        sys.exit("ui_check: no <script> block found — did the dashboard move?")
    return "\n".join(blocks)


def page_html(url=None):
    if url:
        import urllib.request
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read().decode("utf-8")
    sys.path.insert(0, DIR)
    import pool_server                      # importing does NOT start the server
    return pool_server.DASHBOARD


def main(argv):
    url = argv[argv.index("--url") + 1] if "--url" in argv else None
    js = extract_script(page_html(url))
    node = shutil.which("node")
    if not node:
        print("ui_check: SKIPPED — node is not installed, so the JavaScript in "
              "%d bytes of dashboard was NOT parsed" % len(js))
        return 2
    tmp = os.path.join(tempfile.mkdtemp(prefix="ui-check-"), "dashboard.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(js)
    try:
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
    finally:
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
    if r.returncode == 0:
        print("ui_check: OK — %d bytes of dashboard JavaScript parsed" % len(js))
        return 0
    print("ui_check: FAILED — the dashboard will not run as served")
    print((r.stdout + r.stderr).strip()[-1500:])
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
