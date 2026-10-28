import sqlite3

db = sqlite3.connect(r"I:\tools\chat-index\chatmap.db")
c = db.cursor()


def q(sql, *a):
    return c.execute(sql, a).fetchone()


print("sessions total          ", q("select count(*) from session")[0])
print("sessions with messages  ", q("select count(*) from session where n_msg>0")[0])
print("sessions md-only        ", q("select count(*) from session where n_msg=0")[0])
print("sessions with blocks    ", q("select count(*) from session where n_block>0")[0])
print("sessions with vault md  ", q("select count(*) from session where has_vault_md=1")[0])
print("msg total               ", q("select count(*) from msg")[0])
print("msg with real clock     ", q("select count(*) from msg where ts_src=1")[0])
print("msg with text           ", q("select count(*) from msg where text is not null")[0])
print("vault turns             ", q("select count(*) from vault_turn")[0])
print("blocks                  ", q("select count(*) from block")[0])
print("blocks with p3          ", q("select count(*) from block where p3 is not null and p3<>''")[0])
print("facts                   ", q("select count(*) from fact")[0])
print("facts with source sid   ", q("select count(*) from fact where source_sid not null")[0])
print("commits                 ", q("select count(*) from gitcommit")[0])
print("ds conv / turns / chunks", q("select count(*) from ds_conv")[0],
      q("select count(*) from ds_turn")[0], q("select count(*) from ds_chunk")[0])
print("ds chunks with conv     ", q("select count(*) from ds_chunk where cid is not null")[0])
print("ann                     ", q("select count(*) from ann")[0])
print("artifacts               ", q("select count(*) from artifact")[0])
print("artifacts by kind       ", dict(c.execute("select kind,count(*) from artifact group by 1 order by 2 desc")))
print("span                    ", q("select min(first_ms),max(last_ms) from session where n_msg>0"))
print("distinct project        ", len(c.execute("select distinct project from session where project not null").fetchall()))
print("db bytes                ", q("select sum(pgsize) from dbstat")[0] if False else "")
db.close()
