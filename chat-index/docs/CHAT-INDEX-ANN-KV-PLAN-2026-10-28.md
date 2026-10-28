# Chat Index → ANN / KV wiring plan (2026-10-28)

Status: **plan, nothing wired yet.** Every number below is measured today, not estimated.

---

## 0. The rule this plan must obey

The three parts stay three chains, talking only through input/output — never one merged schema
(HJ-Jet doctrine #6350, chain rule #7511, KV-is-a-user-of-ANN #7587):

| part | owns | answers | must never hold |
|---|---|---|---|
| **chat-index** | coordinates over text: `(sid, ordinal)` → words, time, block, commit | *where did this come from and what was said* | vectors, KV bytes, model identity |
| **ANN** | route over vectors: buckets, anchors, totem, P3 log | *which rows are similar* | chat vocabulary, KV format, file paths |
| **KV** | carried state: frame artifacts + timeline | *can this context be resumed cheaply* | vectors, chat text |

The only thing all three may share is a **coordinate**: `sid` + `ordinal` (chat) and
`sid` + `turn` (KV frame). Coordinates are integers and opaque ids — value-blind, so passing
them across a chain boundary does not leak payload.

## 1. Measured state of each surface (today)

### ANN — `I:\tools\ann\data\memcore`

```
ann_index.jsonl   63,915 rows   13,135 distinct labels   6 label families
  exchange           30,168 rows /   721 labels     label = content snippet
  chatlog            19,569 rows /   184 labels     label = "chatlog/.. #5"
  dictionary_entry   11,398 rows / 10,941 labels
  embedded_document   2,767 rows / 1,281 labels
  external_ref           12 rows /     7 labels
  insight_exchange        1 row
ann_tags.tsv      12 kinds: v1-migrate 21,316 · chatpool 14,250 · hermes 12,206 ·
                  brainstorm 9,918 · opencode 4,169 · freebuff 1,012 · architecture 334 ·
                  config_values 268 · constraints 246 · project_rules 136 · naming 59 · memo 1
```

Row shape: `{sid: md5(content), file: "<family>/<content-derived string>", npast, dim: 384, vec}`.
`sid` is a content hash, not a chat session. **There is no chat coordinate in any row.**

Title-join feasibility: chat-index has 2,226 sessions but only **275 distinct normalised
titles** (1,020 of them literally `magic-context-compartment`). Join rate **2.2%** → titles are
not identifiers. This is the "roster" gap (`#7799`): ANN has doors (labels) and no roster.

Server surface: `POST /v1/state/search`, `POST /v1/state/regrow`, `POST /v1/state/restamp`,
`GET /v1/state/tags`, `GET /v1/canary` (`src/ann_search.c:5207-5305`).

### KV — `I:\DWGLS-native-fs`

`core/kv_frame_index.h` already defines the right record:

```c
typedef struct {
    int32_t  turn;          /* logical frame index (1-based) */
    int32_t  base_n_tokens;
    uint32_t n_delta;
    uint64_t prefix_hash;   /* FNV-1a over tokens[0..key_n) — frame key */
    uint64_t t_unix;
    char     base_file[FTL_PATH];   /* chat_<hash>.kvcb */
    char     delta_file[FTL_PATH];  /* chat_t<N>.kvd   */
    char     sid[FTL_SID];          /* chat session id ('' if none) */
} FTLFrame;
```

So the coordinate slot **exists and is empty**: no `kv_timeline.idx` exists anywhere on `I:\`,
and the artifacts are named only by prefix hash —
`I:\DWGLS-native-fs\build\kvslots\` holds 14 `.kvcb` + 4 `.kvd` + 3 prompt `.txt`
(50–102 bytes each: test data, not real conversations).

## 2. The three bridges, in dependency order

### Phase A — make the coordinate callable (chat-index only, no other repo touched)

Add one read-only command that returns the coordinate record as JSON, stable field names:

```
chatmap.py coord --sid ses_xxx [--ord N] [--json]
  -> {sid, ordinal, block_id, ts_ms, ts_src, vault_file, vault_turn,
      ann: [{ann_sid, kind}], kv: [{turn, prefix_hash, base_file, delta_file}]}
```

Ship it as a **view + CLI**, not an API server: the other chains call the CLI (or read the
SQLite file read-only). No schema of ANN or KV appears in chat-index; the `ann`/`kv` arrays are
filled from sidecar files described below, and are empty until those exist.

*Gate A:* 200 random coordinates resolve in <5 ms each, byte-identical across two runs; a
coordinate with no ANN/KV link returns empty arrays rather than guessing.

### Phase B — give ANN a roster (the real gap)

**B1 (measurement, done today).** Can existing rows recover a coordinate from their own label
text? 300 random rows from the chat-ish families, FTS query built from the label stem:

```
hit a message:  164 / 300  = 54.7%
  chatlog            86 / 106 = 81%
  exchange           68 / 178 = 38%
  embedded_document  10 /  16 = 63%
```

This is an **upper bound, precision unverified** — an 8-word AND query can land on a different
message that merely shares words. It is a candidate generator, not a mapping.

**B2 (verification gate).** A candidate is only accepted as a link if the ANN row's stored
vector and a fresh embedding of the candidate message agree: `cos >= 0.995`. Embeddings come from
the existing `/v1/embeddings` used by `ann_search.c:569`. Anything below the threshold stays
**unlinked** (never guessed).

**B3 (write path).** New rows carry the coordinate at ingest: `ref = "<sid>#<ordinal>"` stored
in a sidecar `ann_ref.tsv` keyed by the ANN row `sid` (content hash) — the index format itself is
not modified, so no regrow is needed and the router stays byte-compatible. Historical rows are
filled in by B2 only.

*Gate B:* `audit` reports `ann_linked / ann_total` and
`ann_linked_verified / ann_linked`; a wrong link (cosine below threshold) must be impossible by
construction. Target for the first pass: **100% of new rows linked, ≥40% of the 52,504 chat-ish
historical rows linked with verification**, the rest honestly unlinked.

### Phase C — fill the KV timeline with coordinates

1. Write `kv_timeline.idx` (`FTLHeader` + one `FTLFrame` per committed turn) for the existing
   `build\kvslots` artifacts: `turn`, `prefix_hash`, `base_file`, `delta_file`, `sid` — the first
   three are recomputable from the artifact bytes and the prompt `.txt` files, so the manifest can
   be **rebuilt, not migrated**.
2. When a chat session is captured cold, `sid` is written from the caller (already a field);
   chat-index then resolves `sid` → which messages/turn that frame covers.
3. `ftl_seek(dir, turn)` returns the resume plan; chat-index's `coord` reports it per coordinate.

*Gate C:* for every artifact, `prefix_hash` recomputed from the prompt text matches the manifest
entry; `ftl_seek` returns a base that exists + the delta chain in order; `sid=''` frames are
reported as unattributed rather than guessed. Cross-model seek still refuses (`model_hash`).

### Phase D — one question, three answers

```
chatmap ask "ANN bucket rule"        (exists today)
  + coord -> ann: which vector rows came from that message
  + coord -> kv : is that context resumable, and from which frame
```

Delivered as extra columns in `ask` output, not as a new service. Each column prints `—` when the
link is absent, and `unlinked (54.7% candidate, unverified)` when a candidate exists but failed
B2 — the same fail-loud rule the map already follows.

## 3. Ordering and why

| # | step | why first | cost |
|---|---|---|---|
| 1 | A: `coord` CLI + view | everything else needs one way to ask "what is this coordinate" | ~1 h |
| 2 | C: rebuild `kv_timeline.idx` | KV artifacts already exist and the struct already has `sid`; nothing to design | ~2 h |
| 3 | B1/B2: ANN candidate + verification pass | the only place precision must be proven before anything is written | ~3 h |
| 4 | B3: ingest writes `ref` | stops the gap from growing, needs the memcore/ANN writer | ~1 h + writer owner |
| 5 | D: columns in `ask` | trivial once A–C exist | ~1 h |

Phase C before Phase B on purpose: KV is a **rebuildable manifest with a field already in the
struct** (low risk, immediate O(1) resume), while ANN is a **write-path change with a precision
proof** (higher risk, needs the verification gate).

## 4. What would falsify each step (write these tests before the code)

| step | falsifier |
|---|---|
| A | two runs of `coord` disagree on any field, or a coordinate resolves to a different block than `block` table says |
| B2 | a verified link whose cosine is <0.995, or a message that embeds to a vector already claimed by another ANN row (`cos≥0.999` collisions must be reported, not silently overwritten) |
| B3 | a new ANN row appears without `ref` in the sidecar, or `restamp`/`regrow` drops the sidecar alignment |
| C | `prefix_hash` mismatch against the prompt text, or `ftl_seek` returning a delta chain with a gap |
| D | `ask` shows a KV frame for a session that has none, or hides an unlinked ANN candidate |

## 5. Explicit non-goals

- No merging of tables or vocabularies between chat-index, ANN and KV.
- No writes into `I:\OpenCode-data\opencode.db` (titles/timestamps of the 67 recovered sessions
  stay repaired **inside the map**, `title_src='first_message'`); writing to the live app db
  needs the app closed and the owner's explicit go-ahead.
- No re-embedding of the 63,915 existing rows — new rows carry `ref`, old rows are linked only
  when verified.
- No storage/compression claims anywhere in this layer: it routes, it does not shrink bytes.

## 6. Receipts to collect when this plan is executed

1. `coord` latency p50/p95 over 200 samples.
2. `ann_linked / ann_total`, and the cosine distribution of accepted links.
3. `kv_timeline.idx` frame count vs artifact count, hash re-verification rate.
4. One end-to-end trace: decision text → coordinate → 3 ANN rows → 1 KV frame, with the frame's
   resume plan printed.
