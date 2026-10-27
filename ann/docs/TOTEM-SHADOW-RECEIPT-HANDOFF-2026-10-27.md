# Totem shadow receipt validation — handoff

## Verdict

Route-level shadow validation is live and safe. Candidate-level hard validation is
not enabled because the ANN response does not yet carry producer-owned receipts
per SID.

## Live receipt

Probe: `ann/totem_shadow_probe.py` against `http://127.0.0.1:8096/v1/state/search`

Latest real-vector probe:

```text
returned_hits: 5
candidate_receipts: 0
candidate_receipts_missing: 5
route_violations: []
items_scored: 12948
items_total: 63911
```

The route receipt was present and `totem.lit` matched the five route buckets.
Hits and ranking were not filtered or changed.

## Implemented

- `ann/totem_shadow_validate.py`: deterministic receipt checks.
- `ann/totem_shadow_probe.py`: live observe-only probe and append-only JSONL report.
- `ann/tests/test_totem_shadow_validate.py`
- `ann/tests/test_totem_shadow_probe.py`
- `ann/tests/test_totem_receipt_fixture.py`

Checks cover generation, totem, frustum, Wang edge, relation validity, route
bucket/lit consistency, missing receipts, and malformed sidecar rows.

## Remaining blocker

The live hit shape is currently only:

```text
sid, file, npast, score
```

The producer/index must eventually provide a sidecar row of the form:

```json
{"sid":"...","totem_receipt":{"generation":7,"totem_id":12,"frustum_id":4,"wang_edge":"L","relation_valid":true}}
```

Do not derive this from vector values, bucket IDs, `term_hint`, or geometry.
Until producer-owned receipts exist, missing receipts remain an observation and
must not reject candidates.

## Verification

- Shadow/receipt tests: `7/7 PASS`
- `py_compile`: pass
- `git diff --check`: pass
- Production ANN output: unchanged
- Hard reject: disabled

## SIFT1M structural gate

The existing SIFT1M artifacts were used as a structural, not semantic, producer
fixture. The full sidecar contains `1,000,000` rows:

```text
file: ann/data/memcore/sift1m_receipts.jsonl
sha256: 37d2e2b8c70f28f7a2c3194c42ab7c96be0295acbd6531fd0bb129712db1588d
size: 102,555,519 bytes
```

With 100 real SIFT queries and top-k 10:

```text
checked: 1000
valid_receipts: 1000
missing_receipts: 0
coverage: 1.0000
recall@10: 0.9220
```

The exact 10k universe also passed with `200/200` valid receipts and zero
missing. Negative tests detect stale generation, wrong frustum, and wrong Wang
edge. SIFT assignments are derived structural test coordinates; they are not
semantic route truth and must not be used to claim relation or multi-view recall.

## Final scope verdict

Structural receipt generation, join, deterministic hashing, shadow reporting,
and scale test: **PASS**. Semantic producer metadata and candidate-level live
hard gating remain blocked until the upstream producer supplies real
`sid -> totem_receipt` metadata. Keep hard reject disabled; continue observing
missing receipts on the memcore live index.

## Next handoff

When producer metadata exists: build the receipt sidecar, verify SID/index
generation binding, run shadow coverage on real queries, then evaluate a hard
gate with fallback only after missing receipts reach the agreed threshold.
