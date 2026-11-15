"""Прогон лукапа на реальной реплике (запускать на ВПС). Печатает карточку без Telegram."""
import re
import sys
from dotenv import load_dotenv
load_dotenv()

import db
import bot as botmod


def render(row):
    calls = db.get_gate_calls(row["id"]) if row.get("_kind") != "out" else None
    txt = botmod.card(row, calls)
    return re.sub(r"<[^>]+>", "", txt)


print("connection:", db.test_connection())
for q in sys.argv[1:]:
    print("\n" + "=" * 60)
    print("QUERY:", q)
    rows = db.search(q, limit=10)
    print("matches:", len(rows), "kinds:", [r.get("_kind") for r in rows])
    if rows:
        print(render(rows[0]))
