#!/usr/bin/env python3
"""
直接用SQLite检查数据库
"""
import sqlite3
import json

db_path = "spirit.db"

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 查询所有表
print("📋 数据库表:")
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()
for table in tables:
    print(f"  - {table[0]}")

# 查询weekly_reports表
print("\n📊 weekly_reports 表结构:")
cursor.execute("PRAGMA table_info(weekly_reports);")
columns = cursor.fetchall()
for col in columns:
    print(f"  - {col[1]} ({col[2]})")

# 查询周报数据
print("\n📅 周报数据:")
cursor.execute("SELECT * FROM weekly_reports;")
reports = cursor.fetchall()
print(f"  周报总数: {len(reports)}")
if reports:
    for i, report in enumerate(reports[:3], 1):
        print(f"\n  周报 {i}:")
        print(f"    ID: {report[0]}")
        print(f"    用户ID: {report[1]}")
        print(f"    周开始: {report[2]}")
        print(f"    周结束: {report[3]}")
        print(f"    标题: {report[4]}")
        print(f"    总分: {report[5]}")
        tree_data = json.loads(report[8])
        print(f"    树健康度: {tree_data.get('tree_health', 'N/A')}")

# 查询spirit_weekly_scores表
print("\n🧚 spirit_weekly_scores 表:")
cursor.execute("SELECT COUNT(*) FROM spirit_weekly_scores;")
count = cursor.fetchone()[0]
print(f"  评分记录数: {count}")

conn.close()
