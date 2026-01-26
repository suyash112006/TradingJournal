import json
import os
import shutil

# Config
USER_ID = 3
BASE_DIR = r"d:\trading journal\uploads\users"
USER_BASE = os.path.join(BASE_DIR, f"user_{USER_ID}", "mt5")

print(f"Checking Symbol-Based Hierarchy at: {USER_BASE}")

if os.path.exists(USER_BASE):
    symbols = [d for d in os.listdir(USER_BASE) if os.path.isdir(os.path.join(USER_BASE, d))]
    print(f"Found symbols: {symbols}")
    
    for sym in symbols:
        sym_path = os.path.join(USER_BASE, sym)
        print(f"\n--- Symbol: {sym} ---")
        
        # Check subfolders
        raw_path = os.path.join(sym_path, "raw")
        proc_path = os.path.join(sym_path, "processed")
        meta_path = os.path.join(sym_path, "meta.json")
        status_path = os.path.join(sym_path, "status.json")
        
        print(f" Raw Folder: {'✅' if os.path.exists(raw_path) else '❌'}")
        print(f" Processed Folder: {'✅' if os.path.exists(proc_path) else '❌'}")
        print(f" meta.json: {'✅' if os.path.exists(meta_path) else '❌'}")
        print(f" status.json: {'✅' if os.path.exists(status_path) else '❌'}")
        
        if os.path.exists(proc_path):
            timeframes = ["M1", "M5", "M15", "H1", "D1"]
            for tf in timeframes:
                tf_json = os.path.join(proc_path, tf, "candles.json")
                print(f"   [{tf}] candles.json: {'✅' if os.path.exists(tf_json) else '❌'} ({os.path.getsize(tf_json) if os.path.exists(tf_json) else 0} bytes)")
else:
    print(f"❌ User base not found: {USER_BASE}")
