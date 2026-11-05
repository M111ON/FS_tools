"""Self-check for the precomputed order asset (ico_precompute_order.py).

Verifies:
  - asset exists and loads
  - shapes/dtypes/version
  - vf/vt match icosphere() (geometry not drifted)
  - dv matches recomputed chord distances
  - m matches recomputed min
  - transition using asset is EXACTLY equal to full argmin (small N)
  - regenerating the asset is byte-identical (idempotent)
"""
import os, sys, subprocess, hashlib, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ico_cost_quality import icosphere
from ico_precompute_order import asset_path, build
from ico_transition_precomputed import transition_precomputed

def test_asset_loads():
    p = asset_path(2, 3)
    assert os.path.exists(p), p
    a = np.load(p)
    assert int(a['from_level']) == 2 and int(a['to_level']) == 3
    assert int(a['version']) == 1
    assert a['dv'].shape == (162, 642) and a['dv'].dtype == np.float64
    assert a['m'].shape == (162,) and a['m'].dtype == np.float64
    assert a['vf'].shape == (162, 3) and a['vt'].shape == (642, 3)

def test_geometry_matches():
    a = np.load(asset_path(2, 3))
    vf, _ = icosphere(2); vt, _ = icosphere(3)
    assert np.allclose(a['vf'], vf.astype(np.float64), atol=1e-12)
    assert np.allclose(a['vt'], vt.astype(np.float64), atol=1e-12)
    dv, m, _, _ = build(2, 3)
    assert np.allclose(a['dv'], dv, atol=1e-12)
    assert np.allclose(a['m'], m, atol=1e-12)

def test_transition_exact():
    import numpy as np
    from ico_lifecycle import Ico, _fvecs
    BASE = os.path.join(HERE, 'sift', 'sift_base.fvecs')
    X_all = _fvecs(BASE)
    rng = np.random.default_rng(7)
    X = X_all[rng.choice(len(X_all), size=5000, replace=False)]
    asset = np.load(asset_path(2, 3))
    inc, t, sk, ex, ev, P, vt = transition_precomputed(X, 2, 3, asset)
    # full argmin
    full = np.empty(X.shape[0], dtype=np.int32)
    for s in range(0, X.shape[0], 5000):
        e = min(s+5000, X.shape[0])
        d = np.linalg.norm(P[s:e, None, :] - vt[None, :, :], axis=2)
        full[s:e] = np.argmin(d, axis=1)
    assert np.array_equal(inc, full), "transition != full argmin"

def test_idempotent():
    p = asset_path(2, 3)
    h0 = hashlib.sha256(open(p,'rb').read()).hexdigest()
    subprocess.run([sys.executable, os.path.join(HERE,'ico_precompute_order.py')],
                   check=True, cwd=HERE)
    h1 = hashlib.sha256(open(p,'rb').read()).hexdigest()
    assert h0 == h1, "regeneration changed bytes"

if __name__ == "__main__":
    t0 = time.perf_counter()
    test_asset_loads(); print("asset_loads OK")
    test_geometry_matches(); print("geometry_matches OK")
    test_transition_exact(); print("transition_exact OK")
    test_idempotent(); print("idempotent OK")
    print(f"ALL PASS in {time.perf_counter()-t0:.2f}s")
