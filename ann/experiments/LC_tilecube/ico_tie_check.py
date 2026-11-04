"""Confirm the section-A NDCG penalty is a tie/duplicate artifact, not a retrieval loss."""
import os, sys
import numpy as np
sys.path.insert(0, '.')
from ico_lifecycle import _fvecs, gt_set, l2_topk

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')


def main():
    X = _fvecs(BASE)
    rng = np.random.default_rng(5)
    N = len(X)
    print(f"{'query':>8} {'d(rank1)':>9} {'d(gt1)':>9} {'same?':>6} "
          f"{'#rows at d(rank1)':>18} {'#rows at d(gt1)':>16}")
    for qi in rng.choice(N, 6, replace=False):
        q = X[qi].astype(np.int32)
        d = np.einsum('ij,ij->i', X.astype(np.int32) - q, X.astype(np.int32) - q).astype(np.float64)
        d[qi] = np.inf
        gt = sorted(gt_set(X, X[qi], int(qi)), key=lambda g: d[g])
        ranked = l2_topk(X, X[qi], np.arange(N), 10)
        d_rank1, d_gt1 = d[ranked[0]], d[gt[0]]
        n_at_r1 = int((d == d_rank1).sum())
        n_at_g1 = int((d == d_gt1).sum())
        print(f"{qi:>8} {d_rank1:>9.0f} {d_gt1:>9.0f} {str(d_rank1==d_gt1):>6} "
              f"{n_at_r1:>18} {n_at_g1:>16}")
    print("\nif d(rank1) == d(gt1) while the indices differ, the miss is a duplicate tie")


if __name__ == '__main__':
    main()