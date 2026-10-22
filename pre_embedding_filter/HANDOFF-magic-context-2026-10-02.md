# Handoff — magic-context map/verify: next phase (quality watch + curation)

## Goal of next session

Two candidate readings of "พัฒนาส่วนต่อไป", in the order they are most likely intended:

1. **Continue the magic-context thread** that closed today (2026-10-02): make the
   maintenance path trustworthy and shrink what it has to process.
2. If the user instead means DWGLS development — see *Open decisions* #5.

Do not re-derive any of today's work. **Start by reading the runbook:**
`I:\tools\pre_embedding_filter\MAGIC-CONTEXT-MAP-VERIFY.md`
(there is a pointer to it in `C:\Users\Administrator.AVENTADOR\.config\opencode\AGENTS.md`
under `# Agent Rules — magic-context map/verify backlog`).

## State of play

**Done today (receipts, not claims):**
- map backlog 0 / verify backlog 0 across **all 8 project identities**, counted with
  the plugin's own SQL predicates.
- Reject loop in place: verify fail → memory returns to unmapped + file recorded in
  `map_rejects.json` (never `independent`). Measured fail 100→50→24→20→3→1; **84
  file-backed mappings recovered** that a `fail→independent` policy destroyed.
- Nightly scheduled task `magiccontext-map` (03:00) → `map_standalone.cmd` →
  `mc_cycle.py --rounds 6`; proven `stable — stopping`, `exit=0`.
- Schema guard `check_schema()` (fail loud if magic-context migrates the table) and
  a `WARNING` for unknown project identities — both exercised.

**Nothing is blocking.** The system runs unattended.

**Immediate first action for the next session:** confirm last night's 03:00 run
happened and did not regress:
```bash
python I:\tools\pre_embedding_filter\mc_cycle.py --state   # expect unmapped=0 unverified=0
Get-Content I:\tools\pre_embedding_filter\map_standalone.log -Tail 30
schtasks /query /tn magiccontext-map
```

## Open decisions

1. **Watch the reject loop for 2–3 nightly rounds.** Decide whether
   `map_rejects.json` converges (fails draining to file-independent at a sensible
   rate) or whether verifier false-negatives are over-rejecting. If rejects grow
   without converging, the knob is the evidence extraction in
   `verify_standalone.read_excerpt` (file head 2000 chars + rg keyword hits), not
   the model.
2. **Curation stages 4–6** — the user explicitly chose "curate then map" and paused
   it to run mapping first. DWGLS still holds CONSTRAINTS 135 · CONFIG_VALUES 165 ·
   NAMING 40 (PROJECT_RULES down to 102); the other 7 identities were never curated.
   Stages 1–3 archived 55 total; rules/architecture must be cut rule-by-rule, not
   in bulk by `retrieval_count`.
3. **The plugin's own tasks**: `map-memories` and `verify` still run through the
   dead opencode session path (`verify` also has an empty schedule → manual-only).
   Decide: disable them (now redundant) or leave them as a second writer. Same
   question for `maintain-docs` ("Dreamer returned no assistant output").
4. **memcore feed cadence** — `feed_memcore.py` + `build_store.py` + `v2.py embed`
   is a standing maintenance order, run on demand rather than per question.
5. **If the intended next part is DWGLS development:** the unresolved thread from
   earlier today is R36_Comp (composition rule, strides `ry38 rx37`, needs a
   `tests/test_*.c` in the `GEO_FAST` group per repo rule #7067). It was
   investigated and reached no verdict in this session — treat it as fresh.

## Skills to use

- `verification-before-completion` — every claim here has a receipt; keep that up
- `git-commit` — the tools live in `I:\tools` (it is a git repo, `git:174753f2`);
  nothing from today was committed
- `handoff` — for the next hop after this one
- For DWGLS work instead: `graft` (repo graph) + the repo's own `.agents/rules.md`

## Artifacts (reference, do not copy into this doc)

- Runbook: `I:\tools\pre_embedding_filter\MAGIC-CONTEXT-MAP-VERIFY.md`
- Tools: `map_standalone.py`, `verify_standalone.py`, `mc_cycle.py`,
  `map_standalone.cmd` (all in `I:\tools\pre_embedding_filter\`)
- Sidecars: `map_rejects.json`, `map_candidates.json`, `map_standalone.log`
- Rule pointer: `C:\Users\Administrator.AVENTADOR\.config\opencode\AGENTS.md`
  → `# Agent Rules — magic-context map/verify backlog`
- DB: `I:\cortexkit\magic-context\context.db` (owner: plugin; write only the
  `memory_verifications` table, `INSERT OR IGNORE`)
- Session note `#9`; project memory `#7126` (CONFIG_VALUES)
- Scheduled task: `magiccontext-map`
