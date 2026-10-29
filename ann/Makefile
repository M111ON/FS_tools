CC      := gcc
CFLAGS  := -O2 -std=c11 -Wall -Wno-unused-parameter -Wno-sign-compare -Wno-format -Wno-unknown-pragmas -Iinclude
LDFLAGS := -lws2_32 -lpsapi -lm
BUILD   := build
# The python tests need numpy, which the bare `python` on PATH does NOT have
# (measured). I:\python3.14.4\python.exe does. Override with PY=... if needed.
PY      ?= I:/python3.14.4/python.exe

# Tests that are green in the upstream repo (byte-identical copies).
# NOTE: test_anchor_determinism is deliberately NOT here — it fails in the
# upstream repo too (anch_assign vs anch_route[0] tie-break on equidistant
# centroids). See FORK.md §Known upstream issue. Do not "fix" it here first.
TESTS := test_anchor_route test_anchor_routed test_wang_latch test_anchor_letter test_anchor_lettercube
# The anchor->LetterCube bridge also has a REAL-DATA gate. It reads the served
# index (263 MB) so it is not in `test`; run it by hand after touching the bridge:
#   ./build/alc_real_gate.exe data/memcore/ann_index.jsonl data/memcore/ann_anchors.bin

all: $(BUILD)/ann_search.exe

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/ann_search.exe: src/ann_search.c $(wildcard include/*.h) | $(BUILD)
	$(CC) $(CFLAGS) -o $@ src/ann_search.c $(LDFLAGS)

test: | $(BUILD)
	@fail=0; \
	for t in $(TESTS); do \
	  echo "--- $$t ---"; \
	  $(CC) $(CFLAGS) -o $(BUILD)/$$t.exe tests/$$t.c -lm || { fail=1; continue; }; \
	  ./$(BUILD)/$$t.exe || fail=1; \
	done; \
	echo "--- test_p3_compact (python) ---"; \
	$(PY) tests/test_p3_compact.py || fail=1; \
	echo "--- test_vec_format (python, needs numpy) ---"; \
	$(PY) tests/test_vec_format.py || fail=1; \
	if [ $$fail -eq 0 ]; then echo "ALL PASS"; else echo "FAILURES"; fi; \
	exit $$fail

serve: $(BUILD)/ann_search.exe
	./$(BUILD)/ann_search.exe --serve 8096

# P3 route-log maintenance (the tombstone log). Compact is lossless by
# construction: replay is last-event-wins per slot, so superseded records are
# dropped and a replayed latch is bit-identical (verified by p3_compact_verify).
$(BUILD)/p3_compact_verify.exe: tests/p3_compact_verify.c $(wildcard include/*.h) | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tests/p3_compact_verify.c -lm

$(BUILD)/p3_append_test.exe: tests/p3_append_test.c $(wildcard include/*.h) | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tests/p3_append_test.c -lm

p3-tools: $(BUILD)/p3_compact_verify.exe $(BUILD)/p3_append_test.exe

# Dry-run the compactor (no writes). Apply with:
#   python scripts/p3_compact.py --log data/memcore/lz_route.log --apply
p3-compact-dry: | $(BUILD)
	$(PY) scripts/p3_compact.py --log data/memcore/lz_route.log

# Where a query's time goes (embed vs scan). Needs the live server on :8096.
latency-split:
	$(PY) bench_latency_split.py

# Phase-2 latch A/B (card task-tcce8f5f2): budget-limited, snapshot-pinned,
# repeatable. The earlier A/B scored every row on both arms because its scratch
# copy was inconsistent (anchors of one moment, index of another) — this one
# refuses to run unless the snapshot is a coherent prefix, and its VALID gate
# fails the run when the walk degenerates into a full scan.
latch-ab: $(BUILD)/ann_search.exe | $(BUILD)
	$(PY) bench_latch_ab.py --runs 5 --queries 40 --exe $(BUILD)/ann_search.exe

clean:
	rm -f $(BUILD)/ann_search.exe $(BUILD)/test_*.exe $(BUILD)/p3_*.exe

.PHONY: all test serve clean p3-tools p3-compact-dry latency-split latch-ab
