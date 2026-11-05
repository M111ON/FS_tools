# LC_tilecube — standalone usage

LC_tilecube is a geometric retrieval layer: icosphere-based addressing,
RCU/epoch reclamation, fast rerank (CPU + GPU), and dedup. Usable on its own
without tesspack.

## Install / import

```python
import sys
sys.path.insert(0, "I:/tools/ann/experiments")   # or add to PYTHONPATH
import LC_tilecube as lc
```

Or run scripts directly from `I:/tools/ann/experiments/LC_tilecube/`.

## Quick start

### 1. Build a store (write path)
```python
from LC_tilecube import IngestionPipeline
pipe = IngestionPipeline(branch_count=3, steps_per_item=2, domain_size=100,
                        storage_dir="./store")
pipe.ingest("item1", vector=[...], branches=[0, 1])
```

### 2. Read / search (read path)
```python
from LC_tilecube import ReadPath
rp = ReadPath("./store")
hits = rp.search(branch=0, query=[...], top_k=5)
# -> [{"key":..., "item_id":..., "step":..., "score":...}, ...]
```

### 3. Rerank (CPU)
```python
from LC_tilecube import rerank_topk, rerank_euclid
idx, dist = rerank_topk(P, V, k=10)   # P: (nq,d), V: (nv,d)
```

### 4. Rerank (GPU, resident V)
```python
from LC_tilecube import ResidentReranker
rr = ResidentReranker(V)              # V transferred once
i = rr.query(P)                      # argmax cosine, fast
idx, dist = rr.query_topk(P, k=10)    # two-stage exact
```

### 5. Geometry
```python
from LC_tilecube import icosphere, Ico
v, f = icosphere(2)   # 162 vertices
```

### 6. Precompute order asset
```bash
python ico_precompute_order.py        # builds order_L2_L3.npz
python ico_transition_precomputed.py  # uses asset, exact match
```

### 7. Dedup
```bash
python memcore_dedup.py               # builds memcore_dedup_map.json
```

## Tests
```bash
cd I:/tools/ann/experiments/LC_tilecube
python -m pytest test_read_path.py test_read_path_e2e.py test_rerank_fast.py \
    test_rerank_gpu.py test_precompute_order.py test_memcore_dedup.py -q
```

## Standalone demo
```bash
python lc_demo.py
```

## Files
See PRODUCTION-READINESS.md for full test report and remaining work.
