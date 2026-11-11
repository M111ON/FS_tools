# ANN X-ray + JEV Integration

## Overview

- X-ray: stateless, zero-write, integer-only entropy/density coarse filter over ANN cells.
- JEV: jev-1.13 decision engine scoring candidate relevance (0-1) post-query.
- They run together: X-ray prunes first, JEV scores survivors (fallback if all pruned).

## Thresholds

- Density prune band: 470-530 (prune rate ~19% on observed corpus).
- Score uses density balance around target 500.

## FNV Config

- FNV hashing disabled by default; query log stores `input_text` (plaintext) for debugging.
- Re-enable hashing with env `ANN_QUERY_LOG_HASH=1`.
- Plaintext logging can be disabled with env `ANN_QUERY_LOG_TEXT=0`.

## Verification

- batch_recall_test.py: self-recall@1/@5/@10 = 1.000
- batch_recall_noisy.py: recall@10 = 0.950
- Hot-path latency ~10ms; cold start up to ~13.6s.

## Dashboard

- Port 8391: JEV Audit + X-ray Audit tabs.
- Auto-audit loop runs X-ray then JEV every 15s.
"