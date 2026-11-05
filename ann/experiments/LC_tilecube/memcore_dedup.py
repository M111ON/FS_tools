"""MemCore dedup: exact + near-duplicate detection on LC fruit store (tf73942fc).

Reports duplicate statistics on the LC memcore store and writes a dedup
mapping (original item id -> canonical id) so downstream NDCG can be
re-run with/without duplicates. Near-duplicate threshold = 0.99 cosine.
"""
import os, sys, json, hashlib
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

STORE = os.path.join(HERE, 'memcore_lc_store')

def load_vectors():
    fd = json.load(open(os.path.join(STORE, 'fruit_data.json')))
    items = {}  # id -> (branch, vector)
    for br, b in fd.items():
        for iid, vec in b['vectors'].items():
            items[iid] = (int(br), np.asarray(vec, dtype=np.float64))
    return items

def norm(v):
    n = np.linalg.norm(v)
    return v / (n + 1e-12)

def main():
    items = load_vectors()
    ids = list(items.keys())
    V = np.stack([norm(items[i][1]) for i in ids])  # (N, D) unit
    N = len(ids)
    print(f"items: {N}  dim: {V.shape[1]}")

    # exact duplicates: identical vectors (cosine == 1.0 within tolerance)
    # use rounding to 6 decimals for exact-text-equality proxy
    hashes = {}
    for i, iid in enumerate(ids):
        key = hashlib.sha1(V[i].tobytes()).hexdigest()
        hashes.setdefault(key, []).append(iid)
    exact_groups = [g for g in hashes.values() if len(g) > 1]
    exact_dup_count = sum(len(g)-1 for g in exact_groups)
    print(f"exact dup groups: {len(exact_groups)}  extra rows: {exact_dup_count}")

    # near-duplicates: cosine > 0.99 (excluding exact)
    # compute pairwise in chunks to avoid O(N^2) memory blow (N small here)
    near_pairs = []
    thr = 0.99
    for i in range(N):
        # cosine with all j>i
        sims = V[i+1:] @ V[i]
        js = np.where(sims > thr)[0] + (i+1)
        for j in js:
            if ids[j] not in [p[1] for p in near_pairs if p[0]==ids[i]]:
                near_pairs.append((ids[i], ids[j], float(sims[j-i-1])))
    # dedupe pair list by first id
    print(f"near-dup pairs (cos>0.99): {len(near_pairs)}")

    # build canonical mapping: union-find over near-dup pairs
    parent = {iid: iid for iid in ids}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    for a, b, _ in near_pairs:
        union(a, b)
    # also union exact dup groups (already included if cos>0.99; exact cos=1.0 >0.99 so included)
    canons = {}
    for iid in ids:
        canons[iid] = find(iid)
    n_canonical = len(set(canons.values()))
    print(f"canonical (deduped) count: {n_canonical}  (removed {N - n_canonical})")

    # save mapping
    out = os.path.join(HERE, 'memcore_dedup_map.json')
    with open(out, 'w') as f:
        json.dump({'threshold': thr, 'n_original': N, 'n_canonical': n_canonical,
                   'mapping': canons}, f, indent=1)
    print(f"mapping saved: {out}")

    # summary stats
    print("\nDuplicate rate:", round((N - n_canonical) / N * 100, 2), "%")
    print("Largest dup group size:", max(len([x for x in canons.values() if x==c]) for c in set(canons.values())))

if __name__ == "__main__":
    main()
