"""fetch_cqadupstack_qrels.py — the 12 CQADupStack test qrels, from HuggingFace.

Where the qrels live: they ship inside the 5.3 GB BEIR dataset zip
(public.ukp.informatik.tu-darmstadt.de/thakur/.../cqadupstack.zip, as
<sub>/qrels/test.tsv), which is the only place in the dataset distribution.
The BEIR authors also mirror exactly those twelve files on the Hub as
BeIR/cqadupstack-qrels (1.3 MB total), which is what this uses - no 5.3 GB
download, no disk churn.

Files land as data/beir/qrels/<sub>__test.tsv and keep the original header
line, matching what score_cqadupstack_package_2026-11-02.py expects.

Run: python experiments/fetch_cqadupstack_qrels.py
"""
import os
import urllib.request
from pathlib import Path

BASE = "https://huggingface.co/datasets/BeIR/cqadupstack-qrels/resolve/main"
SUBS = ["android", "english", "gaming", "gis", "mathematica", "physics",
        "programmers", "stats", "tex", "unix", "webmasters", "wordpress"]
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "beir" / "qrels"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for sub in SUBS:
        dst = OUT / ("%s__test.tsv" % sub)
        if dst.exists() and dst.stat().st_size > 0:
            continue
        with urllib.request.urlopen("%s/%s/test.tsv" % (BASE, sub), timeout=120) as r:
            body = r.read()
        if len(body) < 1000:
            raise RuntimeError("%s: only %d bytes back - not a qrels file" % (sub, len(body)))
        dst.write_bytes(body)
        first = body.split(b"\n", 1)[0][:60].decode("utf-8", "replace")
        print("%-12s %7d bytes   header: %s" % (sub, len(body), first))
    print("\nwrote %d files into %s" % (len(os.listdir(OUT)), OUT))


if __name__ == "__main__":
    main()
