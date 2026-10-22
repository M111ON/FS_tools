import sqlite3, os

C_DB = r'C:\Users\Administrator.AVENTADOR\.local\share\cortexkit\magic-context\context.db'
BACKUP = C_DB + '.pre-merge-backup'

conn = sqlite3.connect(C_DB)
conn_c = sqlite3.connect(BACKUP)
c = conn.cursor()
cc = conn_c.cursor()

# === Check C: notes schema ===
cc.execute('PRAGMA table_info(notes)')
c_cols = [r[1] for r in cc.fetchall()]
print(f'C: notes columns: {c_cols}')

c.execute('PRAGMA table_info(notes)')
i_cols = [r[1] for r in c.fetchall()]
print(f'I: notes columns: {i_cols}')

# === Insert notes with all C: columns ===
cc.execute('SELECT * FROM notes')
c_notes = cc.fetchall()
c.execute('SELECT max(id) FROM notes')
max_id = c.fetchone()[0] or 0
inserted = 0
for row in c_notes:
    max_id += 1
    cols = ','.join(c_cols)
    placeholders = ','.join(['?' for _ in c_cols])
    vals = list(row)
    vals[0] = max_id  # reassign id
    try:
        c.execute(f'INSERT INTO notes ({cols}) VALUES ({placeholders})', vals)
        inserted += 1
    except Exception as e:
        print(f'  note error: {e}')
conn.commit()
print(f'Notes: inserted {inserted}/{len(c_notes)}')

# === Check C: user_memories schema ===
cc.execute('PRAGMA table_info(user_memories)')
c_um_cols = [r[1] for r in cc.fetchall()]
print(f'C: user_memories columns: {c_um_cols}')

c.execute('PRAGMA table_info(user_memories)')
i_um_cols = [r[1] for r in c.fetchall()]
print(f'I: user_memories columns: {i_um_cols}')

# Insert user_memories with C: columns
cc.execute('SELECT * FROM user_memories')
c_um = cc.fetchall()
c.execute('SELECT max(id) FROM user_memories')
max_um = c.fetchone()[0] or 0
inserted_um = 0
for row in c_um:
    max_um += 1
    cols = ','.join(c_um_cols)
    placeholders = ','.join(['?' for _ in c_um_cols])
    vals = list(row)
    vals[0] = max_um
    try:
        c.execute(f'INSERT INTO user_memories ({cols}) VALUES ({placeholders})', vals)
        inserted_um += 1
    except Exception as e:
        print(f'  um error: {e}')
conn.commit()
print(f'User memories: inserted {inserted_um}/{len(c_um)}')

# Final counts
c.execute('SELECT count(*) FROM notes')
print(f'\n=== FINAL ===')
print(f'Notes: {c.fetchone()[0]}')
c.execute('SELECT count(*) FROM user_memories')
print(f'User memories: {c.fetchone()[0]}')
c.execute('SELECT count(*) FROM memories')
print(f'Memories: {c.fetchone()[0]}')
print(f'Size: {os.path.getsize(C_DB)/1024/1024:.1f} MB')

conn.close()
conn_c.close()
