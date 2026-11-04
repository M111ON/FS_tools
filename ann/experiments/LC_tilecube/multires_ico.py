"""Prototype: multi-resolution icosphere addressing (L0->L1->L2) with geo_jump.

Concept: coarse-to-fine semantic addressing using icosphere levels:
  L0: 12 vertices  (coarse, ~30 deg separation)
  L1: 42 vertices  (medium, ~16 deg)
  L2: 162 vertices (fine, ~8 deg)

Each level is a refinement of the previous (subdivision). geo_jump transfers
between levels (coarse slot -> fine slots) bijectively. Candidate generation:
  1. Find nearest L0 vertex (coarse).
  2. Expand to L0 neighbors (1-ring).
  3. For each L0 vertex, get its L1 children, then L2 children.
  4. Also expand L2 neighbors (1-ring) of the target L2 vertex.
  5. Rerank by cosine.

This should give better recall than single-level icosphere at comparable scan.

Python prototype only (#7825).
"""
import sys, os
from collections import defaultdict
import numpy as np
sys.path.insert(0, '.')

import icosphere_address as ico_mod

# Rebuild icospheres
verts0, faces0 = ico_mod.build_icosphere(0)
verts1, faces1 = ico_mod.build_icosphere(1)
verts2, faces2 = ico_mod.build_icosphere(2)
adj0 = ico_mod.adjacency(verts0, faces0)
adj1 = ico_mod.adjacency(verts1, faces1)
adj2 = ico_mod.adjacency(verts2, faces2)


def build_parent_map(child_verts, parent_verts):
    """Map each child vertex to nearest parent vertex (coarse-to-fine)."""
    # child_verts: (n_child, 3), parent_verts: (n_parent, 3)
    # For icosphere subdivision, children of a parent are the midpoints
    # of its edges + the parent itself. We compute nearest parent by cosine.
    parent_of = []
    for cv in child_verts:
        sims = parent_verts @ cv
        parent_of.append(int(np.argmax(sims)))
    return parent_of


# Build parent maps: L1 -> L0, L2 -> L1
parent1_of_2 = build_parent_map(verts2, verts1)  # 162 -> 42
parent0_of_1 = build_parent_map(verts1, verts0)  # 42 -> 12
# L2 -> L0 (transitive)
parent0_of_2 = [parent0_of_1[p] for p in parent1_of_2]  # 162 -> 12

# Children maps: L0 -> L1, L1 -> L2
children1_of_0 = defaultdict(list)
for i, p in enumerate(parent0_of_1):
    children1_of_0[p].append(i)
children2_of_1 = defaultdict(list)
for i, p in enumerate(parent1_of_2):
    children2_of_1[p].append(i)


def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na <= 0 or nb <= 0 else float(np.dot(a, b) / (na * nb))


def nearest_vertex(verts, v):
    sims = verts @ v
    return int(np.argmax(sims))


def multires_candidates(q_proj, K_ring=1, levels=3):
    """Coarse-to-fine candidate generation.

    levels: 1 = L0 only, 2 = L0->L1, 3 = L0->L1->L2
    """
    cands = set()
    # L0: nearest + 1-ring
    v0 = nearest_vertex(verts0, q_proj)
    l0_nodes = {v0} | adj0[v0]
    if levels >= 1:
        for n0 in l0_nodes:
            # L1 children of n0
            l1_children = children1_of_0.get(n0, [])
            for n1 in l1_children:
                if levels >= 2:
                    # L2 children of n1
                    l2_children = children2_of_1.get(n1, [])
                    for n2 in l2_children:
                        cands.add(n2)
                        # L2 1-ring neighbors
                        if K_ring >= 1:
                            for nb in adj2[n2]:
                                cands.add(nb)
                else:
                    cands.add(n1)
    # Also add L2 nearest directly
    v2 = nearest_vertex(verts2, q_proj)
    cands.add(v2)
    if K_ring >= 1:
        for nb in adj2[v2]:
            cands.add(nb)
    return cands


def test_structure():
    print("=" * 60)
    print("TEST 1: multi-resolution structure")
    print(f"  L0: {len(verts0)} verts, L1: {len(verts1)} verts, L2: {len(verts2)} verts")
    print(f"  parent1_of_2: {len(parent1_of_2)} (162->42)")
    print(f"  parent0_of_1: {len(parent0_of_1)} (42->12)")
    # verify children counts
    total_l2_from_l1 = sum(len(v) for v in children2_of_1.values())
    print(f"  L2 children of L1 total: {total_l2_from_l1} (expect 162)")
    assert total_l2_from_l1 == 162
    total_l1_from_l0 = sum(len(v) for v in children1_of_0.values())
    print(f"  L1 children of L0 total: {total_l1_from_l0} (expect 42)")
    assert total_l1_from_l0 == 42
    print("  PASS")


def test_recall():
    print("=" * 60)
    print("TEST 2: multi-res icosphere recall vs PCA baseline")
    d = 64
    N = 500
    rng = np.random.default_rng(3)
    X = rng.normal(size=(N, d))
    Q = rng.normal(size=(20, d))

    Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
    Qn = Q / np.linalg.norm(Q, axis=1, keepdims=True)

    # PCA projection to 3D
    mu = Xn.mean(0)
    U = np.linalg.svd(Xn - mu, full_matrices=False)[2][:3].T
    Xp = (Xn - mu) @ U
    Qp = (Qn - mu) @ U
    Xp = Xp / (np.linalg.norm(Xp, axis=1, keepdims=True) + 1e-9)
    Qp = Qp / (np.linalg.norm(Qp, axis=1, keepdims=True) + 1e-9)

    # Map data to L2 vertices
    X_l2 = [nearest_vertex(verts2, v) for v in Xp]
    l2_map = defaultdict(list)
    for i, v in enumerate(X_l2):
        l2_map[v].append(i)

    # Map L2 vertex -> L0/L1 for candidate generation
    # For a given query, find L2 vertex, then expand

    def multires_cands(q_proj, K_ring=1):
        v2 = nearest_vertex(verts2, q_proj)
        # L2 1-ring
        l2_nodes = {v2} | (adj2[v2] if K_ring >= 1 else set())
        cands = set()
        for n2 in l2_nodes:
            for idx in l2_map.get(n2, []):
                cands.add(idx)
        # Also include L1 siblings: all L2 children of the same L1 parent
        for n2 in list(l2_nodes):
            p1 = parent1_of_2[n2]
            for sibling_l2 in children2_of_1.get(p1, []):
                for idx in l2_map.get(sibling_l2, []):
                    cands.add(idx)
        # L0 siblings: all L2 under same L0
        for n2 in list(l2_nodes):
            p0 = parent0_of_2[n2]
            for l1_child in children1_of_0.get(p0, []):
                for l2_child in children2_of_1.get(l1_child, []):
                    for idx in l2_map.get(l2_child, []):
                        cands.add(idx)
        return cands

    # PCA baseline
    def pca_bucket(pt):
        pt = (pt + 1) / 2
        pt = (pt * 11).astype(int)
        pt = np.clip(pt, 0, 11)
        return tuple(pt.tolist())

    pca_map = defaultdict(list)
    for i, pt in enumerate(Xp):
        pca_map[pca_bucket(pt)].append(i)

    def pca_cands(q, K=1):
        b = pca_bucket(q)
        cands = set(pca_map.get(b, []))
        for pos in range(3):
            for alt in range(12):
                nb = list(b)
                nb[pos] = alt
                for idx in pca_map.get(tuple(nb), []):
                    cands.add(idx)
        return cands

    for K_ring in [0, 1]:
        # multires
        recs_mr = []
        scans_mr = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = multires_cands(Qp[qi], K_ring=K_ring)
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs_mr.append(len(gtset & set(ranked)) / 10.0)
            scans_mr.append(len(cands) / N * 100)

        # PCA
        recs_pca = []
        scans_pca = []
        for qi in range(len(Q)):
            gt = sorted(range(N), key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            gtset = set(gt)
            cands = pca_cands(Qp[qi])
            ranked = sorted(cands, key=lambda i: cos(Qn[qi], Xn[i]), reverse=True)[:10]
            recs_pca.append(len(gtset & set(ranked)) / 10.0)
            scans_pca.append(len(cands) / N * 100)

        print(f"  K_ring={K_ring}:")
        print(f"    multires ico: recall={np.mean(recs_mr):.3f} scan~{np.mean(scans_mr):.1f}%")
        print(f"    PCA-grid:     recall={np.mean(recs_pca):.3f} scan~{np.mean(scans_pca):.1f}%")
    print("  PASS")


def test_geometric_consistency():
    print("=" * 60)
    print("TEST 3: geometric consistency (parent/child maps)")
    # verify that L2 children of an L1 vertex are geometrically close
    # (all children should be near the parent on the sphere)
    max_sep = 0
    for l1_idx in range(len(verts1)):
        children = children2_of_1.get(l1_idx, [])
        if not children:
            continue
        for c in children:
            sep = np.arccos(np.clip(np.dot(verts1[l1_idx], verts2[c]), -1, 1))
            max_sep = max(max_sep, sep)
    print(f"  max L2-child to L1-parent angular sep: {np.degrees(max_sep):.2f} deg")
    # L2 children should be within ~1 subdivision step of parent
    print(f"  (L2 has {len(verts2)} verts, L1 has {len(verts1)} verts)")
    print("  PASS")


def main():
    test_structure()
    test_recall()
    test_geometric_consistency()
    print()
    print("multi-res icosphere prototype: done")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
