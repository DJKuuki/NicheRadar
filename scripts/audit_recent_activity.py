import sqlite3
import json

conn = sqlite3.connect("data/shadow_trading.sqlite")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== RECENTLY SETTLED ORDERS (Since Sept 20) ===")
cur.execute("""
    SELECT order_id, event_slug, bracket_name, fill_price, cost_usdc, settled_price, realized_pnl, placed_at_utc, settled_at_utc, metadata_json
    FROM shadow_orders
    WHERE status = 'SETTLED' AND settled_at_utc >= '2026-09-20'
    ORDER BY settled_at_utc DESC
""")
recent = cur.fetchall()
print(f"Total settled since Sept 20: {len(recent)}")
for r in recent:
    win = "WIN " if r['realized_pnl'] > 0 else "LOSS"
    print(f"[{win}] {r['order_id']} | {r['event_slug']} | {r['bracket_name']} | Px={r['fill_price']} | Term={r['settled_price']} | Cost=${r['cost_usdc']:.2f} | PnL=${r['realized_pnl']:+.2f} | Settled={r['settled_at_utc']}")

print("\n=== ORDERS PLACED AFTER COMMIT 55588e1 (Sept 23+) ===")
cur.execute("""
    SELECT order_id, event_slug, bracket_name, limit_price, fill_price, status, cost_usdc, placed_at_utc, metadata_json
    FROM shadow_orders
    WHERE placed_at_utc >= '2026-09-23'
    ORDER BY placed_at_utc DESC
""")
v3_orders = cur.fetchall()
print(f"Total orders placed on or after Sept 23: {len(v3_orders)}")
for r in v3_orders:
    meta = json.loads(r['metadata_json']) if r['metadata_json'] else {}
    version = meta.get("strategy_version", "legacy")
    print(f"[{r['status']}] {r['order_id']} (ver={version}) | {r['event_slug']} | {r['bracket_name']} | LimPx={r['limit_price']} | FillPx={r['fill_price']} | Cost=${r['cost_usdc']:.2f} | Placed={r['placed_at_utc']}")

print("\n=== CURRENT ACTIVE ORDERS (OPEN + FILLED) ===")
cur.execute("""
    SELECT order_id, status, event_slug, bracket_name, limit_price, fill_price, cost_usdc, placed_at_utc
    FROM shadow_orders
    WHERE status IN ('OPEN', 'FILLED')
    ORDER BY status ASC, placed_at_utc DESC
""")
active = cur.fetchall()
print(f"Total active orders: {len(active)}")
for r in active:
    p = r['fill_price'] or r['limit_price']
    print(f"[{r['status']}] {r['order_id']} | {r['event_slug'][:30]} | {r['bracket_name'][:40]} | Px={p} | Cost=${r['cost_usdc']:.2f} | Placed={r['placed_at_utc']}")
