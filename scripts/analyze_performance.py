import sqlite3
import json

conn = sqlite3.connect("data/shadow_trading.sqlite")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== SETTLED TRADES SUMMARY BY EVENT ===")
cur.execute("""
    SELECT 
        event_slug,
        COUNT(*) as total_orders,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        SUM(realized_pnl) as net_pnl,
        SUM(cost_usdc) as total_cost
    FROM shadow_orders
    WHERE status = 'SETTLED'
    GROUP BY event_slug
    ORDER BY net_pnl ASC
""")
for r in cur.fetchall():
    print(f"Event: {r['event_slug']}")
    print(f"  Orders: {r['total_orders']}, Wins: {r['wins']}, PnL: ${r['net_pnl']:.2f}, Invested: ${r['total_cost']:.2f}")

print("\n=== INDIVIDUAL SETTLED TRADES ===")
cur.execute("""
    SELECT 
        order_id,
        event_slug,
        bracket_name,
        side,
        limit_price,
        fill_price,
        size_shares,
        cost_usdc,
        settled_price,
        realized_pnl,
        placed_at_utc,
        settled_at_utc,
        metadata_json
    FROM shadow_orders
    WHERE status = 'SETTLED'
    ORDER BY placed_at_utc ASC
""")
for r in cur.fetchall():
    win_mark = "WIN " if r['realized_pnl'] > 0 else "LOSS"
    meta = json.loads(r['metadata_json']) if r['metadata_json'] else {}
    model_p = meta.get('model_prob', 0)
    market_p = meta.get('market_quote_ask', 0)
    print(f"[{win_mark}] {r['order_id']} | {r['event_slug'][:25]} | {r['bracket_name'][:45]} | Px={r['fill_price']} (lim={r['limit_price']}) | ModelP={model_p:.3f} MktP={market_p:.3f} | Term={r['settled_price']} | Cost=${r['cost_usdc']:.2f} | PnL=${r['realized_pnl']:.2f}")

print("\n=== ACTIVE FILLED POSITIONS CAPITAL BREAKDOWN ===")
cur.execute("""
    SELECT 
        event_slug,
        COUNT(*) as pos_count,
        SUM(cost_usdc) as total_locked,
        MIN(placed_at_utc) as earliest_order,
        MAX(placed_at_utc) as latest_order
    FROM shadow_orders
    WHERE status = 'FILLED'
    GROUP BY event_slug
    ORDER BY total_locked DESC
""")
for r in cur.fetchall():
    print(f"{r['event_slug']}: {r['pos_count']} pos | Locked: ${r['total_locked']:.2f} | Range: {r['earliest_order']} to {r['latest_order']}")

print("\n=== OPEN RESTING ORDERS ===")
cur.execute("""
    SELECT order_id, event_slug, bracket_name, limit_price, size_shares, cost_usdc, placed_at_utc
    FROM shadow_orders
    WHERE status = 'OPEN'
""")
for r in cur.fetchall():
    print(f"OPEN: {r['order_id']} | {r['bracket_name'][:40]} | Px={r['limit_price']} | Cost=${r['cost_usdc']:.2f} | Placed={r['placed_at_utc']}")
