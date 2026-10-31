-- Producer-owned semantic route metadata. ANN never infers these fields.
-- Keep this schema separate from fact_store.v2.sqlite3 until the upstream
-- producer writes real records.
CREATE TABLE IF NOT EXISTS route_metadata (
    sid TEXT PRIMARY KEY,
    intent TEXT NOT NULL CHECK (intent IN ('value','relation','shape','composite','unknown')),
    value_routes_json TEXT NOT NULL DEFAULT '[]',
    shape_routes_json TEXT NOT NULL DEFAULT '[]',
    junction_routes_json TEXT NOT NULL DEFAULT '[]',
    producer_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS relation_edges (
    source_sid TEXT NOT NULL,
    target_sid TEXT NOT NULL,
    kind TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('in','out')),
    PRIMARY KEY (source_sid, target_sid, kind, direction),
    FOREIGN KEY (source_sid) REFERENCES route_metadata(sid)
);

CREATE INDEX IF NOT EXISTS relation_edges_source_idx
    ON relation_edges(source_sid, direction, kind, target_sid);
