"""Демо-данные для проверки подтверждения формата прошедшего занятия."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
args = sys.argv[1:]
subprocess.run([sys.executable, str(ROOT / "tools" / "seed_demo.py"), *args], check=True)
db = "sqlite+aiosqlite:///./_sim.db"
for i, value in enumerate(args):
    if value == "--db" and i + 1 < len(args):
        db = args[i + 1]
path = db.replace("sqlite+aiosqlite:///", "")
connection = sqlite3.connect(path)
connection.execute(
    "update mentoring_sessions set format='one_on_one', format_confirmed=NULL "
    "where creation_key='demo-past'")
connection.commit()
connection.close()
print("demo-past оставлен без подтверждённого формата")
