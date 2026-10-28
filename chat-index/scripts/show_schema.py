import sqlite3

d = sqlite3.connect("file:I:/tools/chat-index/chatmap.db?mode=ro", uri=True)
for name in ("fact", "block", "gitcommit", "vault_turn", "ann"):
    r = d.execute("select sql from sqlite_master where name=?", (name,)).fetchone()
    print("---", name)
    print(r[0] if r else "MISSING")
