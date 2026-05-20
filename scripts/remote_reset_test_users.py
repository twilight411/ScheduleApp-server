#!/usr/bin/env python3
"""在服务器目录内执行：重置 test01~05 密码。用法: python remote_reset_test_users.py"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

APP_DIR = os.environ.get("APP_DIR", "/app/spirit-scheduler-pre-git-20260516-161024")
DB_PATH = os.environ.get("DB_PATH", os.path.join(APP_DIR, "spirit.db"))
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

from app.utils.jwt import hash_password  # noqa: E402

users_spec = json.loads(os.environ["USERS_JSON"])

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
now = datetime.now(timezone.utc).isoformat()
results = []

for spec in users_spec:
    email = spec["email"].lower()
    pw_hash = hash_password(spec["password"])
    name = spec["name"]

    cur.execute("SELECT id, email, name FROM users WHERE lower(email)=?", (email,))
    row = cur.fetchone()
    if not row:
        print(json.dumps({"ok": False, "error": f"missing user {email}"}))
        sys.exit(1)

    user_id = row["id"]
    cur.execute(
        "UPDATE users SET hashed_password=?, name=?, is_active=1, is_deleted=0, updated_at=? WHERE id=?",
        (pw_hash, name, now, user_id),
    )

    cur.execute(
        "SELECT id FROM tasks WHERE user_id=? AND (title LIKE ? OR raw_input LIKE ?)",
        (user_id, "%iso-test%", "%iso-test%"),
    )
    for tid_row in cur.fetchall():
        tid = tid_row[0]
        cur.execute("DELETE FROM subtasks WHERE task_id=?", (tid,))
        cur.execute("DELETE FROM tasks WHERE id=?", (tid,))

    results.append({
        "email": email,
        "user_id": user_id,
        "name": name,
        "marker": spec["marker"],
        "action": "password_reset",
    })

conn.commit()
conn.close()
print(json.dumps({"ok": True, "users": results}, ensure_ascii=False))
