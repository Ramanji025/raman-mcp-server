import psycopg

conn = psycopg.connect("postgresql://postgres:root@localhost:5432/postgres")
conn.autocommit = True
cur = conn.cursor()

cur.execute("SELECT 1 FROM pg_roles WHERE rolname='mcpkb'")
if not cur.fetchone():
    cur.execute("CREATE USER mcpkb WITH PASSWORD 'mcpkb'")
    print("User mcpkb created")
else:
    print("User mcpkb already exists")

cur.execute("SELECT 1 FROM pg_database WHERE datname='mcpkb'")
if not cur.fetchone():
    cur.execute("CREATE DATABASE mcpkb OWNER mcpkb")
    print("Database mcpkb created")
else:
    print("Database mcpkb already exists")

conn.close()
print("Done")
