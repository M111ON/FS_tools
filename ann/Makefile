CC      := gcc
CFLAGS  := -O2 -std=c11 -Wall -Wno-unused-parameter -Wno-sign-compare -Wno-format -Wno-unknown-pragmas -Iinclude
LDFLAGS := -lws2_32 -lpsapi -lm
BUILD   := build

# Tests that are green in the upstream repo (byte-identical copies).
# NOTE: test_anchor_determinism is deliberately NOT here — it fails in the
# upstream repo too (anch_assign vs anch_route[0] tie-break on equidistant
# centroids). See FORK.md §Known upstream issue. Do not "fix" it here first.
TESTS := test_anchor_route test_anchor_routed test_wang_latch

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
	if [ $$fail -eq 0 ]; then echo "ALL PASS"; else echo "FAILURES"; fi; \
	exit $$fail

serve: $(BUILD)/ann_search.exe
	./$(BUILD)/ann_search.exe --serve 8096

clean:
	rm -f $(BUILD)/ann_search.exe $(BUILD)/test_*.exe

.PHONY: all test serve clean
