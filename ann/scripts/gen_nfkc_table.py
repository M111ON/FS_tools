"""Generate src/nfkc_table.h — the query-side NFKC twin of build_index.sanitize.

TWO inputs, TWO different sources of truth:

  deltas   = every BMP codepoint where Python's unicodedata NFKC changes it
             (2972 cps measured 2026-10-23).  Source of truth: unicodedata,
             the same normalize scripts/build_index.py runs on every corpus row.
             A table entry means "expanding this cp is what lands the query in
             the corpus's space" — so lz_sanitize expands EVERY cp that has one,
             before consulting the vocab bitmap.

  dropbits = cps with NO delta that the e5 sidecar tokenizer 500s on
             (unassigned U+0378, U+FFFD, ZWSP U+200B, Thai holes ...).
             Source of truth: a real probe of the sidecar, NOT a vocab dump —
             the earlier dump claimed the whole delta class was "in vocab"
             (2682/2682) while every one of them 500s raw.  Preserved verbatim
             from the header being regenerated; re-derive with
             scripts/probe_vocab.py if the sidecar or its vocab changes.

Regenerate (in place, preserving dropbits):

    python scripts/gen_nfkc_table.py

The old file's dropbits block is REQUIRED for a bare regeneration — pass
--dropbits-from <file> if you are starting from a header without one.
"""
import argparse
import os
import re
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src", "nfkc_table.h")
PROV = "Auto-generated from e5 sidecar vocab dump (hash fbz58cd-df85-4c07-9bd6-f0a00b1b1a27)"


def load_dropbits(path):
    """(bytes, in_vocab_comment_counts) — the 8192-byte bitmap verbatim."""
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return None
    m = re.search(r"nfkc_dropbits\[8192\] = \{(.*?)\};", src, re.S)
    if not m:
        return None
    vals = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", m.group(1))]
    if len(vals) != 8192:
        raise SystemExit("dropbits block parsed %d bytes, want 8192" % len(vals))
    return bytes(vals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dropbits-from", default=SRC,
                    help="header to lift the dropbits block from")
    ap.add_argument("--dropbits-json", default="",
                    help="scripts/nfkc_vocab.json from probe_vocab.py (wins)")
    ap.add_argument("--out", default=SRC)
    a = ap.parse_args()

    if a.dropbits_json:
        import json
        dropbits = bytes.fromhex(json.load(open(a.dropbits_json))["dropbits_hex"])
        if len(dropbits) != 8192:
            raise SystemExit("dropbits_hex is %d bytes, want 8192" % len(dropbits))
    else:
        dropbits = load_dropbits(a.dropbits_from)
    if dropbits is None:
        raise SystemExit("no dropbits block in %s — refusing to write a header "
                         "with an all-zero bitmap" % a.dropbits_from)
    def in_vocab(cp):
        return cp < 0x10000 and not (dropbits[cp >> 3] & (1 << (cp & 7)))

    deltas, pool, seen, off = [], bytearray(), {}, 0
    dropped_out = 0
    for cp in range(0x10000):
        d = unicodedata.normalize("NFKC", chr(cp))
        if d == chr(cp):
            continue
        b = d.encode("utf-8")
        if len(b) > 255:            # len fits unsigned char; measured max 33
            continue
        if b not in seen:           # dedupe the pool
            seen[b] = off
            pool.extend(b)
            off += len(b)
        deltas.append((cp, seen[b], len(b)))
        if all(not in_vocab(ord(oc)) for oc in d):
            dropped_out += 1

    missing_ascii = [cp for cp in range(0x20, 0x7f) if not in_vocab(cp)]
    if missing_ascii:
        raise SystemExit("bitmap drops printable ASCII %s - refusing to write"
                         % [hex(c) for c in missing_ascii])

    L = []
    L.append("// %s" % PROV)
    L.append("// deltas: every BMP cp NFKC changes (%d) — unicodedata, same"
             " normalize as build_index.py" % len(deltas))
    L.append("// dropbits: cps with NO delta that the sidecar 500s on (%d)"
             " — probe-derived, preserved on regen" % sum(1 for cp in range(0x10000)
                                                           if not in_vocab(cp)
                                                           and unicodedata.normalize("NFKC", chr(cp)) == chr(cp)))
    L.append("// (>0 = the sidecar cannot represent it: drop the cp)")
    L.append("#ifndef NFKC_TABLE_H")
    L.append("#define NFKC_TABLE_H")
    L.append("")
    L.append("static const unsigned char nfkc_pool[%d] = {" % len(pool))
    row = []
    for i, b in enumerate(pool):
        row.append("0x%02x" % b)
        if len(row) == 20 or i == len(pool) - 1:
            L.append("    " + ",".join(row) + ",")
            row = []
    L.append("};")
    L.append("")
    L.append("static const struct { unsigned short cp; unsigned short off;"
             " unsigned char len; } nfkc_tbl[] = {")
    for cp, o, n in deltas:
        L.append("    {0x%04x,%d,%d}," % (cp, o, n))
    L.append("};")
    L.append("#define NFKC_TBL_N %d" % len(deltas))
    L.append("")
    L.append("static const unsigned char nfkc_dropbits[8192] = {")
    for i in range(0, 8192, 16):
        L.append("    " + ",".join("0x%02x" % b for b in dropbits[i:i + 16]) + ",")
    L.append("};")
    L.append("")
    L.append("static inline int nfkc_in_vocab(unsigned cp) { return cp < 0x10000 ?"
             " !(nfkc_dropbits[cp >> 3] & (1u << (cp & 7))) : 0; }")
    L.append("")
    L.append("/* replacement bytes for cp where NFKC changes it, else 0 */")
    L.append("static const char *nfkc_lookup(unsigned cp, unsigned *off,"
             " unsigned char *len) {")
    L.append("    int lo = 0, hi = NFKC_TBL_N - 1;")
    L.append("    while (lo <= hi) {")
    L.append("        int mid = (lo + hi) >> 1;")
    L.append("        unsigned c = nfkc_tbl[mid].cp;")
    L.append("        if (c == cp) { *off = nfkc_tbl[mid].off;"
             " *len = nfkc_tbl[mid].len; return (const char *)nfkc_pool; }")
    L.append("        if (c < cp) lo = mid + 1; else hi = mid - 1;")
    L.append("    }")
    L.append("    return 0;")
    L.append("}")
    L.append("")
    L.append("#endif /* NFKC_TABLE_H */")

    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    print("wrote %s: %d deltas (pool %d bytes, %d dedup), dropbits %d cps,"
          " %d delta-cps expand to wholly-unrepresentable text"
          % (os.path.abspath(a.out), len(deltas), len(pool), len(seen),
             sum(1 for cp in range(0x10000) if not in_vocab(cp)), dropped_out))


if __name__ == "__main__":
    main()
