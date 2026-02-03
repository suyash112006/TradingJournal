
import pandas as pd
import numpy as np

def expand_to_1m_backbone(df, base_tf_minutes):
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

# Test Case: M15 data
# 01:00 (413 volume)
# 01:15 (351 volume)
data = [
    {"time": 1641171600, "open": 1830.63, "high": 1831.82, "low": 1829.52, "close": 1830.51, "volume": 413},
    {"time": 1641171600 + 900, "open": 1830.48, "high": 1830.76, "low": 1828.99, "close": 1829.28, "volume": 351}
]

df = pd.DataFrame(data)
df['DT'] = pd.to_datetime(df['time'], unit='s')
df.set_index('DT', inplace=True)

print("--- Source Data (M15) ---")
print(df)

df_1m = expand_to_1m_backbone(df, 15)
print("\n--- Backbone Rows (First 5) ---")
print(df_1m.iloc[:5][['time', 'open', 'volume']])
print("\n--- Backbone Rows (Gap 14-16) ---")
print(df_1m.iloc[14:17][['time', 'open', 'volume']])

m5_data = resample_candles(df_1m, 5)
print("\n--- Resampled (M5) ---")
for c in m5_data:
    print(c)
