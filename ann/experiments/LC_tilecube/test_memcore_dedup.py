"""Self-check for memcore_dedup.py (tf73942fc)."""
import os, sys, json
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

def test_map_exists_and_valid():
    p = os.path.join(HERE, 'memcore_dedup_map.json')
    assert os.path.exists(p)
    d = json.load(open(p))
    assert d['n_original'] == 600
    assert d['n_canonical'] < d['n_original']
    assert d['n_canonical'] == len(set(d['mapping'].values()))
    assert len(d['mapping']) == d['n_original']
    # every original maps to a canonical that is itself in the map
    for k, v in d['mapping'].items():
        assert v in d['mapping']

def test_canonical_subset():
    d = json.load(open(os.path.join(HERE, 'memcore_dedup_map.json')))
    # canonical ids are those where mapping[id]==id
    canons = [k for k, v in d['mapping'].items() if k == v]
    assert len(canons) == d['n_canonical']

if __name__ == "__main__":
    test_map_exists_and_valid()
    print("map_valid OK")
    test_canonical_subset()
    print("canonical_subset OK")
    print("ALL PASS")
