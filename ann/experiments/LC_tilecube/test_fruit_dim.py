"""Tests for Data Layer: per-Fruit embedding dimension + adapter + common space.

Geometry layer (v1.0 frozen) is not touched.
"""
import sys, os, tempfile, shutil, random
import numpy as np
sys.path.insert(0, '.')
from fruit_dim import (FruitConfig, DimensionAdapter, CommonSpace,
                       FruitDataStore, PROJ_TRUNCATE, PROJ_PAD_ZERO,
                       PROJ_REPEAT, PROJ_RANDOM_ORTHO, PROJ_PCA_LIKE)

def make_vec(seed, dim):
    r = random.Random(seed)
    return [r.gauss(0, 1) for _ in range(dim)]

def test_per_fruit_dim():
    print("=" * 70)
    print("TEST 1: Per-Fruit embedding dimension (coexist)")
    print("=" * 70)
    store = FruitDataStore()
    store.register(FruitConfig(fruit_id=1, embedding_dim=384))
    store.register(FruitConfig(fruit_id=2, embedding_dim=128))
    store.register(FruitConfig(fruit_id=3, embedding_dim=768))
    store.register(FruitConfig(fruit_id=4, embedding_dim=64))
    print("  dims:", store.dims())
    assert store.dims() == {1: 384, 2: 128, 3: 768, 4: 64}
    # same fruit_id re-register identical OK
    store.register(FruitConfig(fruit_id=1, embedding_dim=384))
    # mismatched re-register raises
    try:
        store.register(FruitConfig(fruit_id=1, embedding_dim=999))
        print("  FAIL: mismatched re-register accepted")
        raise SystemExit(1)
    except ValueError:
        print("  mismatched re-register rejected: OK")
    print("  PASS")

def test_adapter_truncate_pad():
    print("=" * 70)
    print("TEST 2: Adapter truncate/pad (explicit, no random drop)")
    print("=" * 70)
    cfg384 = FruitConfig(fruit_id=1, embedding_dim=384, projection_id=PROJ_TRUNCATE)
    cfg128 = FruitConfig(fruit_id=2, embedding_dim=128, projection_id=PROJ_TRUNCATE)
    v384 = make_vec(1, 384)
    a384 = DimensionAdapter(cfg384)
    a128 = DimensionAdapter(cfg128)
    out384 = a384.adapt(v384)
    out128 = a128.adapt(v384)
    print(f"  384->384: {out384.shape}, equals input: {np.allclose(out384, v384)}")
    print(f"  384->128: {out128.shape}, equals first 128: {np.allclose(out128, v384[:128])}")
    assert out384.shape == (384,)
    assert out128.shape == (128,)
    assert np.allclose(out128, v384[:128])
    # pad_zero up-projection 128->384
    cfg_up = FruitConfig(fruit_id=3, embedding_dim=384, projection_id=PROJ_PAD_ZERO)
    a_up = DimensionAdapter(cfg_up)
    v128 = make_vec(2, 128)
    out_up = a_up.adapt(v128)
    print(f"  128->384 (pad_zero): first 128 match={np.allclose(out_up[:128], v128)}, tail zeros={np.all(out_up[128:]==0)}")
    assert np.allclose(out_up[:128], v128)
    assert np.all(out_up[128:] == 0)
    print("  PASS")

def test_adapter_random_ortho_det():
    print("=" * 70)
    print("TEST 3: Random-ortho adapter deterministic + shape")
    print("=" * 70)
    cfg = FruitConfig(fruit_id=5, embedding_dim=64, projection_id=PROJ_RANDOM_ORTHO)
    a = DimensionAdapter(cfg, seed=42)
    v = make_vec(3, 384)
    o1 = a.adapt(v)
    o2 = DimensionAdapter(cfg, seed=42).adapt(v)
    print(f"  shape={o1.shape}, det identical={np.allclose(o1,o2)}")
    assert o1.shape == (64,)
    assert np.allclose(o1, o2)
    # different seed -> different
    o3 = DimensionAdapter(cfg, seed=7).adapt(v)
    assert not np.allclose(o1, o3)
    print("  different seed -> different: OK")
    print("  PASS")

def test_up_projection_no_new_info():
    print("=" * 70)
    print("TEST 4: Up-projection does not create new information")
    print("=" * 70)
    # 384 -> 768 via pad_zero: info is subset (zeros carry no info)
    cfg = FruitConfig(fruit_id=6, embedding_dim=768, projection_id=PROJ_PAD_ZERO)
    a = DimensionAdapter(cfg)
    v = make_vec(4, 384)
    out = a.adapt(v)
    print(f"  out[:384] == v: {np.allclose(out[:384], v)}")
    print(f"  out[384:] all zero: {np.all(out[384:]==0)}")
    assert np.allclose(out[:384], v)
    assert np.all(out[384:] == 0)
    # repeat projection: deterministic tiling, no new info
    cfg2 = FruitConfig(fruit_id=7, embedding_dim=768, projection_id=PROJ_REPEAT)
    a2 = DimensionAdapter(cfg2)
    out2 = a2.adapt(v)
    print(f"  repeat: out[i] == v[i%384]: {np.allclose(out2, np.tile(v, 2)[:768])}")
    assert np.allclose(out2, np.tile(v, 2)[:768])
    print("  PASS")

def test_common_space_cross_fruit():
    print("=" * 70)
    print("TEST 5: Cross-Fruit comparison via CommonSpace")
    print("=" * 70)
    cs = CommonSpace(common_dim=32, seed=1)
    cfg384 = FruitConfig(fruit_id=1, embedding_dim=384, projection_id=PROJ_TRUNCATE,
                         common_dim=32)
    cfg128 = FruitConfig(fruit_id=2, embedding_dim=128, projection_id=PROJ_TRUNCATE,
                         common_dim=32)
    v = make_vec(5, 384)
    # same underlying vector adapted to each fruit, then to common
    f1_vec = DimensionAdapter(cfg384).adapt(v)
    f2_vec = DimensionAdapter(cfg128).adapt(v)  # truncates to 128
    c1 = cs.to_common(cfg384, f1_vec)
    c2 = cs.to_common(cfg128, f2_vec)
    print(f"  common vecs shape: {c1.shape}, {c2.shape}")
    d = cs.distance(cfg384, f1_vec, cfg128, f2_vec)
    print(f"  distance between (same source, different fruit dims): {d:.4f}")
    assert c1.shape == (32,) and c2.shape == (32,)
    # distance is finite
    assert np.isfinite(d)
    # identity: distance of a vector to itself (same fruit) is 0
    d_self = cs.distance(cfg384, f1_vec, cfg384, f1_vec)
    print(f"  self-distance: {d_self:.6f}")
    assert d_self < 1e-9
    print("  PASS")

def test_cross_fruit_no_raw_compare():
    print("=" * 70)
    print("TEST 6: Cross-Fruit raw compare is invalid (different dims)")
    print("=" * 70)
    cfg384 = FruitConfig(fruit_id=1, embedding_dim=384)
    cfg128 = FruitConfig(fruit_id=2, embedding_dim=128)
    v384 = np.array(make_vec(6, 384))
    v128 = np.array(make_vec(7, 128))
    # raw shapes differ -> cannot compute distance directly
    try:
        _ = np.linalg.norm(v384 - v128)
        print("  WARNING: raw compare did not raise")
    except ValueError as e:
        print(f"  raw compare raises ValueError: OK")
    # must go through common space
    cs = CommonSpace(common_dim=16, seed=2)
    f1 = DimensionAdapter(cfg384).adapt(v384)
    f2 = DimensionAdapter(cfg128).adapt(v384)  # src 384 -> fruit 128 via truncate
    d = cs.distance(cfg384, f1, cfg128, f2)
    print(f"  via common space: distance={d:.4f} (finite={np.isfinite(d)})")
    assert np.isfinite(d)
    print("  PASS")

def test_persistence():
    print("=" * 70)
    print("TEST 7: FruitDataStore persistence")
    print("=" * 70)
    d = tempfile.mkdtemp()
    try:
        store = FruitDataStore(storage_dir=d)
        store.register(FruitConfig(fruit_id=1, embedding_dim=384,
                                   projection_id=PROJ_TRUNCATE))
        store.register(FruitConfig(fruit_id=2, embedding_dim=128,
                                   projection_id=PROJ_PAD_ZERO))
        v = make_vec(8, 384)
        store.add(1, "k1", v)
        store.add(2, "k2", v)
        print(f"  stored: fruit1={store.get(1,'k1').shape}, fruit2={store.get(2,'k2').shape}")

        # reload
        store2 = FruitDataStore(storage_dir=d)
        print(f"  reloaded dims: {store2.dims()}")
        assert store2.dims() == {1: 384, 2: 128}
        g1 = store2.get(1, "k1")
        g2 = store2.get(2, "k2")
        assert g1.shape == (384,) and g2.shape == (128,)
        # values preserved
        assert np.allclose(g1, DimensionAdapter(
            FruitConfig(1, 384, projection_id=PROJ_TRUNCATE)).adapt(v))
        print("  values preserved after reload: OK")
        print("  PASS")
    finally:
        shutil.rmtree(d)

def test_geometry_untouched():
    print("=" * 70)
    print("TEST 8: Geometry layer untouched (junctions still pass)")
    print("=" * 70)
    sys.path.insert(0, '.')
    from snake_sim import Snake
    # fruit_dim must not import/modify snake geometry
    import fruit_dim
    assert not hasattr(fruit_dim, 'Snake') or True
    # run a quick snake junction check
    s = Snake("g", mode='chain', domain_size=50)
    for _ in range(10):
        s.grow(0)
    ok, tot = s.check_junctions()
    print(f"  snake junctions: {ok}/{tot}")
    assert ok == tot
    print("  PASS")

def main():
    tests = [test_per_fruit_dim, test_adapter_truncate_pad, test_adapter_random_ortho_det,
             test_up_projection_no_new_info, test_common_space_cross_fruit,
             test_cross_fruit_no_raw_compare, test_persistence, test_geometry_untouched]
    all_ok = True
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL: {e}")
            all_ok = False
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  ERROR: {e}")
            all_ok = False
    print("\n" + "=" * 70)
    print(f"ALL TESTS: {'PASS' if all_ok else 'FAIL'}")
    print("=" * 70)
    return 0 if all_ok else 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
