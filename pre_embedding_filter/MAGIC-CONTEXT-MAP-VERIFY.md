# magic-context map / verify — standalone maintenance

**Read this before touching magic-context mapping.** It records what was built on
2026-10-02, why, and what still breaks.

## The problem

The plugin's own `map-memories` and `verify` tasks run their model through an
**opencode session** (`opencode run`). On this box that path costs ~164s of
baseline overhead for a trivial prompt (plugin load + ONNX Runtime init) and dies
with `HeadersTimeoutError` around 375s on the real ~45k-token map prompt, while a
direct 9router call accepts a 493KB prompt in 6-18s. Tasks that only orchestrate
(classify/compress/retrospective/promote) are fine; tasks that spawn a
tool-capable hidden agent (map, verify, maintain-docs) are not.

Consequence: `map-memories` committed 0 memories every run from 2026-09-26
onwards and the backlog grew to 650 unmapped (DWGLS) / 852 total, plus 891 rows
with `verified_at=0`. A failed map also freezes `verify`, because
`countUnverifiedMappedMemories` can only count rows that mapping created.

## What exists now (all in this directory)

| file | role |
|---|---|
| `map_standalone.py` | maps every unmapped memory in **all 8 project identities** by calling 9router directly |
| `verify_standalone.py` | reads the mapped file and stamps `verified_at` or rejects the mapping |
| `mc_cycle.py` | one round = map then verify; repeats until stable (`--state` prints counters) |
| `map_rejects.json` | files a verifier rejected per memory — the mapper never picks them again |
| `map_candidates.json` | on-disk cache of rg candidate searches (reruns resume) |
| `map_standalone.cmd` | nightly entry: runs `mc_cycle.py --rounds 6`, appends to `map_standalone.log` |

Scheduled task **`magiccontext-map`** (daily 03:00) runs `map_standalone.cmd`.
Proven end-to-end: round 1–2 work, then `stable — stopping`, `exit=0`.

## How the two halves behave

**Map.** Roots are hardcoded in `map_standalone.ROOTS`, derived with the plugin's
own identity rules (`dir:` = md5(path) first 12 hex, `git:` = root commit):

- `git:29b0837d…` and `dir:b7b1d59859e2` → `I:\DWGLS-native-fs`
- `dir:290383da4b02` → `I:\Hermes`
- `dir:3e16a219b8e2` and `git:174753f2…` → `I:\tools`
- `git:bf6bcdfc…` → `I:\tools\cloud_ws`
- `dir:65d8455e2565` → `I:\OpenCode`
- `dir:bb5625ea4027` → Termux phone, **no local root** → no rg, converges to file-independent

Candidates come from paths named in the memory (checked against real disk) or one
rg pass for its distinctive tokens. Reply grammar is the plugin's own:
`<mappings><memory id="N"><file path="x"/></memory></mappings>`, or
`<memory id="N" independent="true"/>`. Parsed with `parse_manifest`
(self-closing tags are matched separately — a `[^>]*` bug there once swapped ids).

**Verify.** For each memory with `file_path<>'' AND verified_at=0`: read the file
head (2000 chars) **plus rg keyword hits for the memory's own tokens** (so
evidence is not lost when the claim points at line 900 of a 3000-line header),
then ask for `<memory id="N" verdict="pass|fail"/>`.

- **pass** → keep the surviving file rows, `verified_at = now` (plugin's
  `recordMemoryVerifications` semantics: delete the memory's rows, re-insert)
- **fail** → **a rejection, not a verdict**: rows are deleted so the memory
  returns to *unmapped*, and the failed files go into `map_rejects.json`

That last rule is the whole termination argument. Every round the candidate set
shrinks, so a memory with nothing backing it ends at file-independent instead of
oscillating, and a memory the mapper picked a wrong file for gets a second, third
chance with a different file. Measured: fail 100 → 50 → 24 → 20 → 3 → 1, and
**84 file-backed mappings were recovered** that a `fail → independent` policy
would have destroyed.

## Counters — use the plugin's predicates, not your own

```
unmapped   = memories with status IN ('active','permanent')
             AND (expires_at IS NULL OR expires_at > now)
             AND no row in memory_verifications
unverified = those with a row, file_path <> '', verified_at = 0
```

Copying the SQL without `expires_at` prints numbers the dashboard does not show
(`unverified=6` while the plugin reports 0 — the extras are `status=archived`).

Current state on 2026-10-02: **unmapped 0 / unverified 0** across all 8
identities, verified 420, independent 667.

## If magic-context is updated

- Nothing here parses `dist/*.js`, so line-number drift cannot break anything.
- The only coupling is the `memory_verifications` table (5 columns). The plugin's
  migrations are additive (`CREATE TABLE IF NOT EXISTS` + `ensureColumn(... DEFAULT ...)`)
  and we INSERT the same column names the plugin itself inserts.
- `check_schema()` in `map_standalone.py` runs at startup and **exits with a
  message** if the table or a column disappears — fail loud, never silent, and it
  cannot write outside that one table.
- A project identity that is missing from `ROOTS` prints a WARNING instead of
  quietly marking everything file-independent.

Still external: 9router on `localhost:20128` with model id `working` (the
`9router/` prefix is an opencode provider id and 404s on that endpoint), and `rg`
on PATH.

## Commands

```bash
python I:\tools\pre_embedding_filter\mc_cycle.py --state          # counters (plugin predicates)
python I:\tools\pre_embedding_filter\mc_cycle.py --rounds 6       # map+verify until stable
cmd /c I:\tools\pre_embedding_filter\map_standalone.cmd           # exactly what the 03:00 task runs
python I:\tools\pre_embedding_filter\map_standalone.py --selftest
python I:\tools\pre_embedding_filter\verify_standalone.py --selftest
python I:\tools\pre_embedding_filter\map_standalone.py --revert   # drop every row this tool wrote
python I:\tools\pre_embedding_filter\verify_standalone.py --revert # set them back to verified_at=0
```

## Still open

- The plugin's own `map-memories` / `verify` tasks remain on the broken session
  path (verify has an empty schedule → manual-only). These tools replace them for
  this box; they were not disabled.
- `maintain-docs` fails the same way ("Dreamer returned no assistant output") and
  is not covered by anything here.
