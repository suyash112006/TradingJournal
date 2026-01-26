import json
import os
import hashlib
from datetime import datetime

# Logic from app.py
def hash_file(file_path):
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def aggregate_candles(m1_candles, timeframe_minutes):
    if not m1_candles: return []
    aggregated = []
    interval_seconds = timeframe_minutes * 60
    current_period_start = None
    period_candles = []
    for candle in m1_candles:
        candle_time = candle['time']
        period_start = (candle_time // interval_seconds) * interval_seconds
        if current_period_start is not None and period_start != current_period_start:
            if period_candles:
                aggregated.append({
                    "time": current_period_start,
                    "open": period_candles[0]['open'],
                    "high": max(c['high'] for c in period_candles),
                    "low": min(c['low'] for c in period_candles),
                    "close": period_candles[-1]['close']
                })
            period_candles = []
        current_period_start = period_start
        period_candles.append(candle)
    if period_candles:
        aggregated.append({
            "time": current_period_start,
            "open": period_candles[0]['open'],
            "high": max(c['high'] for c in period_candles),
            "low": min(c['low'] for c in period_candles),
            "close": period_candles[-1]['close']
        })
    return aggregated

def parse_mt5_csv(file_path, symbol="Unknown"):
    m1_candles = []
    try:
        try:
            with open(file_path, 'r', encoding='utf-8') as f: lines = f.readlines()
        except UnicodeDecodeError:
            with open(file_path, 'r', encoding='utf-16') as f: lines = f.readlines()
        
        start_row = 1 if any(k in lines[0].upper() for k in ['DATE', 'OPEN']) else 0
            
        for line in lines[start_row:]:
            columns = line.split()
            if len(columns) < 6: continue
            try:
                date_str, time_str = columns[0], columns[1]
                dt = datetime.strptime(f"{date_str} {time_str}".replace('.', '-'), "%Y-%m-%d %H:%M")
                m1_candles.append({
                    "time": int(dt.timestamp()),
                    "open": round(float(columns[2]), 5),
                    "high": round(float(columns[3]), 5),
                    "low": round(float(columns[4]), 5),
                    "close": round(float(columns[5]), 5)
                })
            except: continue
        
        m1_candles.sort(key=lambda x: x['time'])
        return {
            "meta": {"symbol": symbol, "csv_hash": hash_file(file_path), "created_at": datetime.utcnow().isoformat() + "Z"},
            "timeframes": {
                "M1": m1_candles,
                "M5": aggregate_candles(m1_candles, 5),
                "M15": aggregate_candles(m1_candles, 15),
                "H1": aggregate_candles(m1_candles, 60),
                "D1": aggregate_candles(m1_candles, 1440)
            }
        }
    except Exception as e:
        print(f"Error: {e}")
        return None

# Migration
USER_ID = 3
MT5_ROOT = r"d:\trading journal\uploads\users\user_3\mt5"
LEGACY_FILES = [
    r"d:\trading journal\uploads\mt5_csv\user_3_XAUUSD_H1_202201030100_202601232300.csv",
    r"d:\trading journal\uploads\mt5_csv\user_3_XAUUSD_M15_202201030100_202601232345.csv"
]

for csv_path in LEGACY_FILES:
    if not os.path.exists(csv_path): continue
    symbol = "XAUUSD" # We know this from filename
    print(f"Migrating {symbol}...")
    
    sym_folder = os.path.join(MT5_ROOT, symbol)
    os.makedirs(os.path.join(sym_folder, "raw"), exist_ok=True)
    os.makedirs(os.path.join(sym_folder, "processed"), exist_ok=True)
    
    # Save raw
    with open(csv_path, 'rb') as src, open(os.path.join(sym_folder, "raw", "original.csv"), 'wb') as dst:
        dst.write(src.read())
        
    # Process
    data = parse_mt5_csv(csv_path, symbol)
    if data:
        with open(os.path.join(sym_folder, "meta.json"), 'w') as f:
            json.dump(data['meta'], f)
        
        for tf, candles in data['timeframes'].items():
            tf_dir = os.path.join(sym_folder, "processed", tf)
            os.makedirs(tf_dir, exist_ok=True)
            with open(os.path.join(tf_dir, "candles.json"), 'w') as f:
                json.dump(candles, f)
        
        with open(os.path.join(sym_folder, "status.json"), 'w') as f:
            json.dump({"status": "READY"}, f)
        print(f"✅ Hierarchical migration complete for {symbol}")
