"""fetch_cqadupstack_qrels.py — the 12 qrels/test.tsv files, without the 5.3 GB.

CQADupStack ships its qrels only inside the dataset zip, and the extracted tree
was deleted after the failed local embedded run (disk pressure). Scoring any
CQADupStack embedding package needs the qrels, so this downloads the zip,
extracts the twelve tiny test.tsv files into data/beir/qrels/<sub>__test.tsv and
deletes the zip again. About 7 minutes on this link, ~1 MB kept.

Run: python experiments/fetch_cqadupstack_qrels.py
"""
import os
import urllib.request
import zipfile
from pathlib import Path

URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/cqadupstack.zip"
ROOT = Path(__file__).resolve().parent.parent
ZIP = ROOT / "data" / "beir" / "cqadupstack.zip"
OUT = ROOT / "data" / "beir" / "qrels"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    have = {f for f in os.listdir(OUT) if f.endswith("__test.tsv")}
    if len(have) == 12:
        print("already have 12 qrels files")
        return
    if not ZIP.exists():
        print("downloading (5.3 GB, ~7 min)...")
        urllib.request.urlretrieve(URL, ZIP)
    with zipfile.ZipFile(ZIP) as z:
        n = 0
        for name in z.namelist():
            if name.endswith("qrels/test.tsv"):
                sub = name.split("/")[1]
                (OUT / ("%s__test.tsv" % sub)).write_bytes(z.read(name))
                n += 1
    print("extracted %d qrels files into %s" % (n, OUT))
    ZIP.unlink()
    print("zip removed")


if __name__ == "__main__":
    main()
