"""
Test script to verify timeframe aggregation fix.
This script tests that M1, M3, M5 are correctly generated from M15 source data.
"""
import pandas as pd
import json
import os

def expand_to_1m_backbone(df, base_tf_minutes):
    """
    Expands a higher timeframe DataFrame (e.g. M15) into a structurally correct 
    1-minute backbone. Vectorized for MAX speed.
    Matches TradingView-style synthetic expansion.
    """
    if base_tf_minutes <= 1:
        return df
        
    last_ts = df.index[-1]
    end_ts = last_ts + pd.Timedelta(minutes=base_tf_minutes - 1)
    
    # Expand index to 1-minute slots
    new_index = pd.date_range(start=df.index[0], end=end_ts, freq='1min')
    df_1m = df.reindex(new_index).ffill()
    
    # Adjust volume (distributed)
    df_1m['volume'] = df_1m['volume'] / base_tf_minutes
    
    # Restore time column for consistency
    df_1m['time'] = df_1m.index.astype('int64') // 10**9
    
    return df_1m

def resample_candles(df, minutes):
    """
    Aggregates 1M/base DataFrame to target minutes using Pandas Resample.
    Returns a list of dicts (standard candle format).
    Enforces strict time alignment (TradingView style).
    """
    try:
        rule = f"{minutes}min"
        
        agg_df = df.resample(rule, closed='left', label='left', origin='start_day').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        
        agg_df['time'] = agg_df.index.astype('int64') // 10**9
        
        return agg_df[['time', 'open', 'high', 'low', 'close', 'volume']].to_dict('records')
    except Exception as e:
        print(f"Resample Error ({minutes}m): {e}")
        return []

# Test with sample M15 data
print("=" * 60)
print("TIMEFRAME AGGREGATION TEST")
print("=" * 60)

# Create sample M15 data (2 candles = 30 minutes)
data = [
    {"time": 1641171600, "open": 1830.63, "high": 1831.82, "low": 1829.52, "close": 1830.51, "volume": 450},
    {"time": 1641172500, "open": 1830.48, "high": 1830.76, "low": 1828.99, "close": 1829.28, "volume": 360}
]

base_df = pd.DataFrame(data)
base_df['DT'] = pd.to_datetime(base_df['time'], unit='s')
base_df.set_index('DT', inplace=True)
base_df.sort_index(inplace=True)

print(f"\n📊 Source Data (M15): {len(data)} candles")
print(base_df[['time', 'open', 'close', 'volume']].head())

# Expand to M1 backbone
detected_tf_minutes = 15
df_1m = expand_to_1m_backbone(base_df.copy(), detected_tf_minutes)

print(f"\n🔧 M1 Backbone: {len(df_1m)} candles (Expected: {len(data) * 15} = {len(data) * 15})")
print(f"   First 5 rows:")
print(df_1m[['time', 'open', 'close', 'volume']].head())

# Test the FIXED M1 assignment logic
m1_data = df_1m[['time', 'open', 'high', 'low', 'close', 'volume']].reset_index(drop=True).to_dict('records')
print(f"\n✅ M1 Dict Conversion: {len(m1_data)} records")
print(f"   Sample: {m1_data[0]}")

# Test M3 and M5 aggregation
m3_data = resample_candles(df_1m, 3)
m5_data = resample_candles(df_1m, 5)

print(f"\n📈 M3 Aggregation: {len(m3_data)} candles (Expected: {len(data) * 15 // 3} = {len(data) * 5})")
print(f"📈 M5 Aggregation: {len(m5_data)} candles (Expected: {len(data) * 15 // 5} = {len(data) * 3})")

# Verify counts
print("\n" + "=" * 60)
print("VERIFICATION RESULTS")
print("=" * 60)

expected_m1 = len(data) * 15
expected_m3 = len(data) * 5
expected_m5 = len(data) * 3

m1_pass = len(m1_data) == expected_m1
m3_pass = len(m3_data) == expected_m3
m5_pass = len(m5_data) == expected_m5

print(f"M1 Count: {'✅ PASS' if m1_pass else '❌ FAIL'} ({len(m1_data)} == {expected_m1})")
print(f"M3 Count: {'✅ PASS' if m3_pass else '❌ FAIL'} ({len(m3_data)} == {expected_m3})")
print(f"M5 Count: {'✅ PASS' if m5_pass else '❌ FAIL'} ({len(m5_data)} == {expected_m5})")

# OHLC Integrity Check
if len(m5_data) > 0 and len(m1_data) >= 5:
    test_m5 = m5_data[0]
    m1_start = test_m5["time"]
    m1_relevant = [c for c in m1_data if m1_start <= c["time"] < m1_start + 300]
    
    if m1_relevant:
        ohlc_pass = (
            test_m5["open"] == m1_relevant[0]["open"] and
            test_m5["close"] == m1_relevant[-1]["close"] and
            abs(test_m5["high"] - max(c["high"] for c in m1_relevant)) < 0.01 and
            abs(test_m5["low"] - min(c["low"] for c in m1_relevant)) < 0.01
        )
        print(f"OHLC Integrity: {'✅ PASS' if ohlc_pass else '❌ FAIL'}")
    else:
        print("OHLC Integrity: ⚠️ SKIP (insufficient M1 data)")

print("\n" + "=" * 60)
if m1_pass and m3_pass and m5_pass:
    print("🎉 ALL TESTS PASSED!")
else:
    print("❌ SOME TESTS FAILED - Review the fix")
print("=" * 60)
