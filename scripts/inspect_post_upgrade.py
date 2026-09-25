import sqlite3

conn = sqlite3.connect("data/shadow_trading.sqlite")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("""
    SELECT * FROM shadow_orders
    WHERE placed_at_utc >= '2026-09-18T07:45:00'
    ORDER BY placed_at_utc DESC
""")
rows = cur.fetchall()
print(f"Orders placed AFTER upgrade: {len(rows)}")
for r in rows:
    print(f"{r['order_id']} | {r['status']} | {r['event_slug']} | {r['bracket_name']} | Px={r['limit_price']} | FillPx={r['fill_price']} | Cost=${r['cost_usdc']}")

print("\n--- ALL ACTIVE FILLED POSITIONS ---")
cur.execute("""
    SELECT order_id, event_slug, bracket_name, limit_price, fill_price, cost_usdc, placed_at_utc
    FROM shadow_orders
    WHERE status = 'FILLED'
    ORDER BY placed_at_utc DESC
""")
filled = cur.fetchall()
print(f"Total active FILLED positions: {len(filled)}")
legacy_otm = [f for f in filled if f['fill_price'] < 0.10]
print(f"Of which, legacy OTM (< 0.10) positions still awaiting settlement: {len(legacy_otm)}")
for f in legacy_otm:
    print(f"  Legacy OTM in flight: {f['order_id']} | {f['event_slug']} | {f['bracket_name']} | Px={f['fill_price']} | Cost=${f['cost_usdc']} | Placed={f['placed_at_utc']}")
