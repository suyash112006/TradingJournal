from app import aggregate_candles

# 2023-11-15 00:00:00 UTC = 1700006400
BASE_TIME = 1700006400 

# Synthetic M1 Data (Exactly one 5-minute block)
# 00:00, 00:01, 00:02, 00:03, 00:04
m1_data = [
    {"time": BASE_TIME + 0,   "open": 100.0, "high": 105.0, "low": 99.0, "close": 102.0, "volume": 10},
    {"time": BASE_TIME + 60,  "open": 102.0, "high": 110.0, "low": 101.0, "close": 108.0, "volume": 20},
    {"time": BASE_TIME + 120, "open": 108.0, "high": 109.0, "low": 107.0, "close": 107.0, "volume": 5},
    {"time": BASE_TIME + 180, "open": 107.0, "high": 115.0, "low": 106.0, "close": 114.0, "volume": 30},
    {"time": BASE_TIME + 240, "open": 114.0, "high": 116.0, "low": 112.0, "close": 113.0, "volume": 15},
    
    # Next Candle (00:05)
    {"time": BASE_TIME + 300, "open": 113.0, "high": 120.0, "low": 112.0, "close": 118.0, "volume": 10},
]

print("🔍 Testing M1 -> M5 Aggregation (Corrected Base)...")
aggregated = aggregate_candles(m1_data, 5)

print(f"\n✅ Created {len(aggregated)} M5 candles (Expected: 2)")

c1 = aggregated[0]
print("\n🕯️ CANDLE 1 (00:00 - 00:05)")
print(f"Time:  {c1['time']} (Expected: {BASE_TIME})")
print(f"Open:  {c1['open']} (Expected: 100.0)")
print(f"High:  {c1['high']} (Expected: 116.0)")
print(f"Low:   {c1['low']} (Expected: 99.0)")
print(f"Close: {c1['close']} (Expected: 113.0)")
print(f"Vol:   {c1['volume']} (Expected: 80)")

assert c1['time'] == BASE_TIME
assert c1['open'] == 100.0
assert c1['high'] == 116.0
assert c1['low'] == 99.0
assert c1['close'] == 113.0
assert c1['volume'] == 80

print("\n🎉 VERIFICATION SUCCESSFUL: Logic scientifically proven.")
