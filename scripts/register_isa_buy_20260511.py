"""Register ISA buys to SQLite via execute_buy."""
import sys
sys.path.insert(0, r"C:\dev\Sanjuk-Stock-Simulator")

from core.portfolio import execute_buy
from db.store import get_cash, get_positions, get_trades, save_cash

# Before
print("=== Before ===")
print(f"Cash: KRW {get_cash():,.0f}")
print("Positions:")
for p in get_positions():
    print(f"  {p.ticker} {p.name}: {p.shares} @ {p.avg_price:,.2f}")

required = 31_700 * 30 + 1_320_000 * 1
current = get_cash()
if current < required:
    topup = required - current + 100  # buffer
    print(f"\n[Topup] DB cash {current:,.0f} < required {required:,.0f}. Adding {topup:,.0f}")
    save_cash(current + topup)

# Execute buys
r1 = execute_buy("462870.KS", "시프트업", 31_700.0, 30, reason="[ISA] 실제 매수 2026-05-11")
print(f"\nBUY: {r1.name} {r1.shares}주 @ {r1.price:,.0f} ({r1.created_at})")

r2 = execute_buy("012450.KS", "한화에어로스페이스", 1_320_000.0, 1, reason="[ISA] 실제 매수 2026-05-11")
print(f"BUY: {r2.name} {r2.shares}주 @ {r2.price:,.0f} ({r2.created_at})")

# After
print("\n=== After ===")
print(f"Cash: KRW {get_cash():,.0f}")
print("Positions:")
for p in get_positions():
    print(f"  {p.ticker} {p.name}: {p.shares} @ {p.avg_price:,.2f}")

print("\nRecent trades:")
for t in get_trades(limit=5):
    print(f"  [{t.created_at}] {t.action} {t.name} {t.shares}@{t.price:,.0f} - {t.reason}")
