import sqlite3
import json

conn = sqlite3.connect("data/shadow_trading.sqlite")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Query price buckets
cur.execute("""
    SELECT 
        CASE 
            WHEN fill_price < 0.10 THEN '1. Deep OTM (< 0.10)'
            WHEN fill_price < 0.30 THEN '2. Moderate OTM (0.10 - 0.30)'
            WHEN fill_price < 0.60 THEN '3. ATM (0.30 - 0.60)'
            ELSE '4. ITM/High Prob (>= 0.60)'
        END as bucket,
        COUNT(*) as total,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        ROUND(SUM(realized_pnl), 2) as net_pnl,
        ROUND(SUM(cost_usdc), 2) as cost
    FROM shadow_orders
    WHERE status = 'SETTLED'
    GROUP BY bucket
    ORDER BY bucket
""")
print("=== PnL BY PRICE BUCKET ===")
for r in cur.fetchall():
    win_rate = (r['wins'] / r['total']) * 100 if r['total'] > 0 else 0
    roi = (r['net_pnl'] / r['cost']) * 100 if r['cost'] > 0 else 0
    print(f"{r['bucket']}: {r['wins']}/{r['total']} ({win_rate:.1f}% win) | Invested: ${r['cost']} | PnL: ${r['net_pnl']} (ROI: {roi:.1f}%)")

# Query by Handle
cur.execute("""
    SELECT 
        CASE 
            WHEN event_slug LIKE '%elon%' THEN 'Elon Musk'
            WHEN event_slug LIKE '%trump%' THEN 'Donald Trump'
            WHEN event_slug LIKE '%cz%' THEN 'CZ Binance'
            WHEN event_slug LIKE '%white-house%' THEN 'White House'
            ELSE 'Other'
        END as target,
        COUNT(*) as total,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        ROUND(SUM(realized_pnl), 2) as net_pnl,
        ROUND(SUM(cost_usdc), 2) as cost
    FROM shadow_orders
    WHERE status = 'SETTLED'
    GROUP BY target
    ORDER BY net_pnl DESC
""")
print("\n=== PnL BY TARGET ACCOUNT ===")
for r in cur.fetchall():
    win_rate = (r['wins'] / r['total']) * 100 if r['total'] > 0 else 0
    roi = (r['net_pnl'] / r['cost']) * 100 if r['cost'] > 0 else 0
    print(f"{r['target']}: {r['wins']}/{r['total']} ({win_rate:.1f}% win) | Invested: ${r['cost']} | PnL: ${r['net_pnl']} (ROI: {roi:.1f}%)")
