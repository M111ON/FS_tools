"""Derive the NFKC dropbitmap from the e5 sidecar's OWN vocab — exact, not guessed.

Why this exists (2026-10-23): the first bitmap came from an unspecified "vocab
dump" and disagreed with reality — it marked all 2682 NFKC-delta codepoints
"in vocab" while every one of them 500s the tokenizer raw. Guessing cost a 503
class; this script reads the GGUF token list instead.

    representable(cp)  <=>  cp appears in the decoded text of some vocab token

That is the same lookup llama-server's tokenizer does ("invalid
unordered_map<K,T> key" = the cp has no entry), so the derived bitmap is the
same verdict the sidecar gives, measured once instead of 65k probes.

Writes scripts/nfkc_vocab.json:
    {"model": "<path>", "n_tokens": N, "n_cps": M, "dropbits_hex": "<16384 hex>"}

    python scripts/probe_vocab.py --model I:/model/multilingual-e5-small-q8_0-fixed.gguf
    python scripts/probe_vocab.py --verify http://127.0.0.1:18095 --sample 120
"""
import argparse
import json
import os
import random
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "nfkc_vocab.json")

_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def read_tokens(path):
    """tokenizer.ggml.tokens as str — a minimal GGUF reader (the local gguf
    module hands back raw ints for STRING arrays, so it cannot be used)."""
    import struct
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise SystemExit("%s is not a GGUF" % path)
        _, n_kv = struct.unpack("<I", f.read(4))[0], struct.unpack(
            "<Q", f.read(8))[0]
        f.read(8)  # n_tensors

        def rs():
            n = struct.unpack("<Q", f.read(8))[0]
            return f.read(n).decode("utf-8", "replace")

        def rv(t):
            if t == 8:
                return rs()
            if t == 9:
                et = struct.unpack("<I", f.read(4))[0]
                n = struct.unpack("<Q", f.read(8))[0]
                return [rv(et) for _ in range(n)]
            return int.from_bytes(f.read(_SIZES[t]), "little")

        for _ in range(n_kv):
            k = rs()
            t = struct.unpack("<I", f.read(4))[0]
            v = rv(t)
            if k == "tokenizer.ggml.tokens":
                return v
    raise SystemExit("no tokenizer.ggml.tokens in %s" % path)


def probe(url, text, to=60):
    """200 if the sidecar can embed `text`, else its HTTP status."""
    req = urllib.request.Request(
        url.rstrip("/") + "/embedding",
        data=json.dumps({"content": text}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=to) as r:
            r.read()
        return 200
    except Exception as ex:
        return getattr(ex, "code", "ERR")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="I:/model/multilingual-e5-small-q8_0-fixed.gguf")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--from-json", default="", help="reuse an earlier bitmap")
    ap.add_argument("--verify", default="", help="sidecar url: spot-check the bitmap")
    ap.add_argument("--sample", type=int, default=120)
    a = ap.parse_args()

    if a.from_json:
        dropbits = bytes.fromhex(json.load(open(a.from_json))["dropbits_hex"])
        model = "(from %s)" % a.from_json
        ntok = 0
    else:
        toks = read_tokens(a.model)
        cps = set()
        for t in toks:
            cps.update(ord(c) for c in t)
        # sentencepiece normalizes ' ' to '▁' before the vocab lookup, so a space
        # is representable whenever U+2581 is a token — reading the raw token
        # text alone would mark U+0020 droppable and silently delete every space
        # in every query (measured: that concatenated words and changed every
        # embedding).
        if ord("\u2581") in cps:
            cps.add(ord(" "))
        dropbits = bytearray(8192)
        for cp in range(0x10000):
            if cp not in cps:
                dropbits[cp >> 3] |= 1 << (cp & 7)
        model, ntok = a.model, len(toks)
        json.dump({"model": model, "n_tokens": ntok, "n_cps": len(cps),
                   "dropbits_hex": bytes(dropbits).hex()},
                  open(a.out, "w"), indent=1)
        print("wrote %s: %d tokens, %d cps representable, %d dropped"
              % (os.path.abspath(a.out), ntok, len(cps),
                 sum(1 for cp in range(0x10000) if dropbits[cp >> 3] & (1 << (cp & 7)))))

    if a.verify:
        rng = random.Random(22726)
        bmp = [cp for cp in range(0x20, 0x10000) if not (0xD800 <= cp <= 0xDFFF)]
        picked = rng.sample(bmp, min(a.sample, len(bmp)))
        bad = 0
        for cp in picked:
            want = 0 if (dropbits[cp >> 3] & (1 << (cp & 7))) else 200
            rc = probe(a.verify, "x" + chr(cp) + "y")
            if (rc == 200) != (want == 200):
                bad += 1
                print("  MISMATCH U+%04X bitmap=%s probe=%s" % (cp, want, rc))
        print("verify: %d/%d agree with the live sidecar" % (len(picked) - bad, len(picked)))
        return 0 if bad == 0 else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
