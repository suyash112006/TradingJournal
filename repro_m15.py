import pandas as pd
import numpy as np

# 1. Mocking the Logic from app.py
def detect_timeframe_logic(diffs):
    min_diff = diffs.min()
    print(f"Min Diff: {min_diff}")
    
    if 0.9 <= min_diff <= 1.1:
        return 1, "M1"
    if 2.9 <= min_diff <= 3.1:
        return 3, "M3"
    if 4.9 <= min_diff <= 5.1:
        return 5, "M5"
    
    mode_diff = int(diffs.mode()[0])
    return mode_diff, f"M{mode_diff}"

def resample_candles(df, minutes):
    rule = f"{minutes}min"
    agg_df = df.resample(rule, closed='left', label='left', origin='start_day').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()
    return agg_df

# 2. Create Sparse M1 Data (Similar to User Scenario)
# Gaps of 15-20 minutes, but some 1-minute consecutive candles
timestamps = [
    "2023-01-01 10:00:00",
    "2023-01-01 10:01:00", # 1 min diff
    "2023-01-01 10:02:00", # 1 min diff
    "2023-01-01 10:15:00", # Gap
    "2023-01-01 10:30:00", # Gap
    "2023-01-01 10:31:00", # 1 min diff
]

df = pd.DataFrame({'DT': pd.to_datetime(timestamps)})
df['open'] = 100
df['high'] = 110
df['low'] = 90
df['close'] = 105
df['volume'] = 10
df.set_index('DT', inplace=True)

# 3. Test Detection
diffs = df.index.to_series().diff().dt.total_seconds() / 60
diffs = diffs[diffs > 0]
detected_minutes, label = detect_timeframe_logic(diffs)
print(f"✅ Detected: {label} ({detected_minutes}m)")

# 4. Test M15 Aggregation
if detected_minutes == 1:
    print("🚀 Expanding to 1M Backbone (Not needed here as index is already timestamps)")
    # But wait, app.py expands it.
    # expand_to_1m_backbone only needed if input was HIGHER TF.
    # If input is M1 (detected 1), we treat df as M1.
    
    # Aggregate M15
    print("🔄 Aggregating M15...")
    m15_df = resample_candles(df, 15)
    print(m15_df)
    
    # Expected:
    # 10:00 -> Contains 10:00, 10:01, 10:02
    # 10:15 -> Contains 10:15
    # 10:30 -> Contains 10:30, 10:31
    
    print(f"Expected 3 candles. Got {len(m15_df)}")
    if len(m15_df) == 3:
        print("✅ M15 Aggregation Successful")
    else:
        print("❌ M15 Aggregation Failed")

else:
    print("❌ Failed to detect M1 base")
