from flask import Flask, render_template, redirect, url_for, flash, request, jsonify, send_from_directory, abort, session, send_file
from sqlalchemy import text, func, inspect
from functools import wraps
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer
import json
import calendar
import pytz
import os
import csv
import io
import zipfile
import time
from datetime import datetime, timedelta, date
from werkzeug.utils import secure_filename

from models import db, bcrypt, User, Task, Trade, RiskSettings
from config import Config
import hashlib
import threading

# ---------------- GLOBAL STATE ----------------
ABORT_PROCESSING = {} # Key: user_id, Value: bool

# ---------------- APP SETUP ----------------
app = Flask(__name__, 
            static_folder='../frontend/src')
app.config.from_object(Config)

from jinja2 import ChoiceLoader, FileSystemLoader
app.jinja_loader = ChoiceLoader([
    FileSystemLoader(os.path.join(app.root_path, '../frontend')),
    FileSystemLoader(os.path.join(app.root_path, '../frontend/src/pages'))
])

# Ensure upload folder exists
if not os.path.exists(app.config.get('UPLOAD_FOLDER', 'uploads')):
    os.makedirs(app.config.get('UPLOAD_FOLDER', 'uploads'))

# MT5 CSV Storage Setup - New Symbol-Based Hierarchy
def get_user_mt5_base(user_id):
    folder = os.path.join(app.root_path, 'uploads', 'users', f'user_{user_id}', 'mt5')
    os.makedirs(folder, exist_ok=True)
    return folder

def get_symbol_folder(user_id, symbol):
    base = get_user_mt5_base(user_id)
    folder = os.path.join(base, symbol.upper())
    os.makedirs(folder, exist_ok=True)
    return folder

app.config['MT5_UPLOAD_BASE_FOLDER'] = os.path.join(app.root_path, 'uploads', 'users')

# TradingView-style timeframe lockdown (Gold Standard: M1 Backbone Enabled)
ALLOWED_TIMEFRAMES = ["M1", "M3", "M5", "M15", "M30", "H1", "H2", "H4", "D1", "W1"]

db.init_app(app)
bcrypt.init_app(app)

# --- Database Initialization ---
with app.app_context():
    try:
        db.create_all()
        
        # Seed RiskSettings if empty
        if not RiskSettings.query.first():
            print("🛠️ Seeding default Risk Settings...")
            default_settings = RiskSettings(profit_target=800.0, max_daily_loss=500.0)
            db.session.add(default_settings)
            db.session.commit()
            print("✅ Risk Settings seeded.")
            
    except Exception as e:
        print(f"⚠️ DB Init failed: {e}")

login_manager = LoginManager(app)
login_manager.login_view = "index"

# Mail Config Safety
if app.config.get("MAIL_USERNAME"):
    mail = Mail(app)
else:
    # Dummy mail object or handle gracefully if mail not configured
    print("⚠️ Mail not configured. Emails will not send.")
    mail = Mail(app) # Initialize anyway to avoid import errors later, but send() might fail if not caught
serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])

# Session & Security Config
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False  # Set to True in production (HTTPS)
)

@app.before_request
def refresh_session():
    session.permanent = True

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != "admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated

def allowed_file(filename):
    allowed_ext = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'mp4'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_ext

def get_broker_day_window():
    """
    Returns (start_utc, end_utc) for the current broker day.
    Standard broker day start is 03:30 IST (which is -2:00 from midnight UTC often, 
    but we calculate it based on Asia/Kolkata).
    """
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    
    # Broker day starts at 03:30 IST
    broker_start_ist = now_ist.replace(hour=3, minute=30, second=0, microsecond=0)
    if now_ist < broker_start_ist:
        broker_start_ist -= timedelta(days=1)
    
    broker_end_ist = broker_start_ist + timedelta(days=1)
    
    # Convert to UTC for DB queries
    start_utc = broker_start_ist.astimezone(pytz.utc).replace(tzinfo=None)
    end_utc = broker_end_ist.astimezone(pytz.utc).replace(tzinfo=None)
    
    return start_utc, end_utc

def discipline_rating(score):
    """Maps a 0-100 internal score to a 1-5 rating."""
    return round(score / 20, 1)

def calculate_discipline(user_id, limit=None):
    """Calculates average discipline based on ALL active (rated) trades."""
    # Fetch ALL rated trades (discipline > 0)
    trades_data = db.session.query(Trade.discipline).filter(
        Trade.user_id == user_id,
        Trade.discipline > 0,
        Trade.is_deleted == False
    ).order_by(Trade.date.desc())
    
    if limit:
        trades_data = trades_data.limit(limit)
        
    trades_data = trades_data.all()
    
    if not trades_data:
        return None
        
    # Calculate average from the fetched list
    values = [t[0] for t in trades_data]
    avg_score = sum(values) / len(values)
    
    return round(avg_score, 1)

def calculate_emotion_score(user_id, limit=None):
    """Calculates Discipline Score based on ALL active trades (0-100%)."""
    # Fetch ALL rated trades (discipline > 0)
    trades_data = db.session.query(Trade.discipline).filter(
        Trade.user_id == user_id, 
        Trade.discipline > 0,
        Trade.is_deleted == False
    ).all()
    
    if not trades_data:
        return None

    # Calculate average of all scores
    values = [t[0] for t in trades_data]
    avg_score = sum(values) / len(values)
    
    # Convert average (1-5) to percentage (0-100)
    return round((avg_score / 5) * 100)

def calculate_rr(entry, stop, take_profit, direction):
    """Calculates Risk:Reward ratio based on Entry, Stop, and TP."""
    if not entry or not stop or not take_profit:
        return None
    try:
        risk = 0
        reward = 0
        if direction == 'Long':
            risk = entry - stop
            reward = take_profit - entry
        else: # Short
            risk = stop - entry
            reward = entry - take_profit
            
        if risk <= 0 or reward <= 0:
            return None
            
        return round(reward / risk, 2)
    except:
        return None

def get_avg_rr(user_id):
    """Calculates average R:R for all user trades."""
    trades = Trade.query.filter(
        Trade.user_id == user_id, 
        Trade.is_deleted == False, 
        Trade.rr != None
    ).all()
    
    if not trades:
        return 0.0
    
    total_rr = sum(t.rr or 0 for t in trades)
    return round(total_rr / len(trades), 2)

def hash_file(file_path):
    """Calculates SHA256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def aggregate_candles(m1_candles, timeframe_minutes):
    """
    Professional Timeframe Aggregation Engine (Gold Standard).
    Groups M1 candles into natural UTC time boundaries (e.g. :00, :05).
    Strictly follows OHLC rules and ensures timestamps align to grid.
    """
    if not m1_candles:
        return []
    
    # 1. Ensure input is sorted chronologically
    sorted_m1 = sorted(m1_candles, key=lambda x: x['time'])
    
    aggregated = []
    interval_seconds = timeframe_minutes * 60
    
    current_bucket_start = None
    bucket_candles = []
    
    for candle in sorted_m1:
        ts = candle['time']
        # Floor to the start of the timeframe interval (Standard Alignment)
        bucket_start = (ts // interval_seconds) * interval_seconds
        
        # If we moved to a new bucket, flush the old one
        if current_bucket_start is not None and bucket_start != current_bucket_start:
            if bucket_candles:
                aggregated.append(build_candle(current_bucket_start, bucket_candles))
            bucket_candles = []
            
        current_bucket_start = bucket_start
        bucket_candles.append(candle)
        
    # Flush final bucket
    if bucket_candles and current_bucket_start is not None:
        aggregated.append(build_candle(current_bucket_start, bucket_candles))
            
    return aggregated

def build_candle(timestamp, group):
    """Helper to build a single OHLC candle from a group."""
    return {
        "time": timestamp, # Aligned to grid start
        "open": group[0]['open'],
        "high": max(c['high'] for c in group),
        "low": min(c['low'] for c in group),
        "close": group[-1]['close'],
        "volume": sum(c['volume'] for c in group)
    }

def background_process_csv(file_path, symbol_folder, symbol, user_id):
    """Background task to process CSV into hierarchical symbol structure."""
    status_path = os.path.join(symbol_folder, 'status.json')
    meta_path = os.path.join(symbol_folder, 'meta.json')
    processed_base = os.path.join(symbol_folder, 'processed')
    
    def update_progress(percent, stage="Processing CSV..."):
        try:
            with open(status_path, 'w') as f:
                json.dump({"state": "PROCESSING", "progress": percent, "stage": stage}, f)
        except: pass

    try:
        update_progress(0, "Starting...")
        print(f"🔄 [BACKGROUND] Started processing for {symbol}")

        # Pass callback and user_id to parser
        parsed_data = parse_mt5_csv(file_path, symbol=symbol, progress_callback=update_progress, user_id=user_id)
        
        if parsed_data:
            update_progress(90, "Finalizing storage...")
            # Save meta.json (Rich Data from Parser)
            with open(meta_path, 'w') as f:
                json.dump(parsed_data['meta'], f, indent=2)
            
            # Save each timeframe separately in zone.json format
            for tf, candles in parsed_data['timeframes'].items():
                tf_folder = os.path.join(processed_base, tf.upper())
                os.makedirs(tf_folder, exist_ok=True)
                zone_path = os.path.join(tf_folder, 'zone.json')
                zone_data = {
                    "symbol": symbol,
                    "timeframe": tf.upper(),
                    "candles": candles
                }
                with open(zone_path, 'w') as f:
                    json.dump(zone_data, f)
            
            with open(status_path, 'w') as f:
                json.dump({"state": "READY", "timestamp": datetime.utcnow().isoformat(), "progress": 100}, f)
            print(f"✅ Hierarchical processing complete for: {symbol}")
        else:
            with open(status_path, 'w') as f:
                json.dump({"state": "ERROR", "message": "Parsing failed"}, f)
    except Exception as e:
        print(f"❌ [BACKGROUND] Processing error for {symbol}: {e}")
        import traceback
        traceback.print_exc()
        try:
            with open(status_path, 'w') as f:
                json.dump({"state": "ERROR", "message": str(e)}, f)
        except Exception as write_err:
             print(f"❌ [BACKGROUND] Failed to write status file: {write_err}")

import multiprocessing
from multiprocessing import Pool, cpu_count
import pandas as pd

# --- Multiprocessing & TF Detection Helpers ---

TF_MAP = {
    1: "M1", 3: "M3", 5: "M5", 15: "M15", 30: "M30",
    60: "H1", 120: "H2", 240: "H4", 1440: "D1", 10080: "W1"
}

def tf_label(minutes):
    return TF_MAP.get(minutes, f"M{minutes}")

def detect_timeframe_from_csv(file_path, sep=','):
    """
    Auto-detect timeframe by scanning first 10,000 rows.
    Returns: (minutes, label) e.g., (5, 'M5')
    """
    try:
        # 1. Broad Scan (Increase to 10k to catch sparse starts)
        scan_rows = 10000
        
        # Optimize Engine: Use 'c' if simple separator, else 'python'
        engine = 'c' if len(sep) == 1 else 'python'
        
        # Use Detected Separator!
        df = pd.read_csv(file_path, sep=sep, usecols=[0, 1], nrows=scan_rows, header=None, engine=engine, on_bad_lines='skip', encoding_errors='ignore')
        
        # Sniff header
        if type(df.iloc[0,0]) == str and 'DATE' in df.iloc[0,0].upper():
             df = pd.read_csv(file_path, sep=sep, nrows=scan_rows, engine=engine, on_bad_lines='skip', encoding_errors='ignore')
        else:
             df.columns = ['DATE', 'TIME'] + [str(i) for i in range(2, len(df.columns))]

        df.columns = [c.upper().strip() for c in df.columns]

        # Parse Dates
        if 'TIME' in df.columns:
            df['DT_STR'] = df['DATE'].astype(str) + ' ' + df['TIME'].astype(str)
            df['DT'] = pd.to_datetime(df['DT_STR'], format='mixed', errors='coerce')
        else:
             df['DT'] = pd.to_datetime(df['DATE'], format='mixed', errors='coerce')

        df = df.dropna(subset=['DT']).sort_values('DT')
        
        # Calculate Diffs in Minutes
        diffs = df['DT'].diff().dt.total_seconds() / 60
        diffs = diffs[diffs > 0] # Ignore 0 or negative
        
        if len(diffs) == 0:
            return 1, "M1" # Fallback

        # Smart Detection:
        # If we see ANY valid intervals <= 1.1 minutes, it IS M1 base.
        # This acts as a 'resolution' check. Even if mode is 15min (sparse),
        # the presence of 1m diffs proves 1m resolution capability.
        min_diff = diffs.min()
        
        # <= 1.1 catches M1 (1.0) and Sub-minute/Tick data (0.001 - 0.99)
        if min_diff <= 1.1:
            print(f"🕵️ [DETECTOR] Found high-res intervals ({min_diff:.4f}m). Forcing M1 Base.")
            return 1, "M1"
        
        if 2.9 <= min_diff <= 3.1:
             return 3, "M3"

        if 4.9 <= min_diff <= 5.1:
             return 5, "M5"

        # Fallback to mode
        mode_diff = int(diffs.mode()[0])
        # Safety: If mode is 1, return 1 (Double Check)
        if mode_diff <= 1:
             return 1, "M1"

        return mode_diff, tf_label(mode_diff)

    except Exception as e:
        print(f"TF Auto-Detect Warning: {e}")
    
    return 1, "M1" # Fallback

def process_chunk_worker(chunk):
    """
    Worker function for Multiprocessing CSV Parse.
    Must be top-level to be pickleable on Windows.
    """
    try:
        # Normalize Columns
        chunk.columns = [c.upper().strip().replace('<','').replace('>','') for c in chunk.columns]
        
        # Standardize Names
        cols = chunk.columns
        
        # Vectorized Time Parsing
        if 'TIME' in cols:
            chunk['DT_STR'] = chunk['DATE'].astype(str) + ' ' + chunk['TIME'].astype(str)
            chunk['DT'] = pd.to_datetime(chunk['DT_STR'], format='mixed', errors='coerce')
        else:
            chunk['DT'] = pd.to_datetime(chunk['DATE'], format='mixed', errors='coerce')
            
        chunk = chunk.dropna(subset=['DT'])
        
        # Vectorized Float Conversion
        for col in ['OPEN', 'HIGH', 'LOW', 'CLOSE']:
            if col in cols:
                 chunk[col] = pd.to_numeric(chunk[col], errors='coerce')

        # Volume Handling
        vol_col = 'VOL'
        if 'TICKVOL' in cols: vol_col = 'TICKVOL'
        elif 'VOLUME' in cols: vol_col = 'VOLUME'
        
        if vol_col in cols:
             chunk['VOL'] = pd.to_numeric(chunk[vol_col], errors='coerce').fillna(0)
        else:
             chunk['VOL'] = 0
             
        chunk = chunk.dropna(subset=['OPEN', 'HIGH', 'LOW', 'CLOSE'])
        
        # Return lightweight dict list for main thread aggregation
        # Converting to dict here reduces pickling overhead of full DF? 
        # Actually returning DF is usually fine for pandas, but let's return processed DF
        return chunk[['DT', 'OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOL']]
        
    except Exception as e:
        # print(f"Worker Error: {e}") 
        return None

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
     aggregates 1M/base DataFrame to target minutes using Pandas Resample.
     Returns a list of dicts (standard candle format).
     Enforces strict time alignment (TradingView style).
    """
    try:
        rule = f"{minutes}min"
        
        # Resample with specific trading rules (Left/Left closed)
        # origin='epoch' ensures alignment to 00:00:00
        agg_df = df.resample(rule, closed='left', label='left', origin='start_day').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        
        # Reset index to access 'DT' as column
        agg_df = agg_df.reset_index()
        
        # Convert back to standard dict list
        # Vectorized dict creation
        agg_df['time'] = agg_df['DT'].astype('int64') // 10**9
        
        return agg_df[['time', 'open', 'high', 'low', 'close', 'volume']].to_dict('records')
    except Exception as e:
        print(f"Resample Error ({minutes}m): {e}")
        return []

def parse_mt5_csv(file_path, symbol="Unknown", progress_callback=None, user_id=None):
    """
    Parses MT5 CSV using Multiprocessing + Pandas for MAX speed.
    Auto-detects the base timeframe first.
    """
    base_candles_dict = {} # Deduplication Buffer
    
    # Check for early abort
    if user_id and ABORT_PROCESSING.get(user_id):
        print(f"🛑 [PARSER] Aborting before start for user {user_id}")
        return None
    
    try:
        # 1. Quick Line Count
        total_rows = 1000000 
        try:
           with open(file_path, 'rb') as f:
               total_rows = sum(1 for _ in f)
        except: pass
        
        # 2. Detect Separator FIRST (Prevent read hang)
        sep = ',' # Default assumption
        try:
            # Try sniffing with different encodings
            for enc in ['utf-8', 'utf-16', 'latin1']:
                try:
                    with open(file_path, 'r', encoding=enc) as f:
                        header = f.readline()
                        if ',' in header: 
                            sep = ','
                            break
                        elif ';' in header: 
                            sep = ';'
                            break
                        elif '\t' in header: 
                            sep = '\t'
                            break
                        
                        # Check strict whitespace last
                        if len(header.split()) > 1:
                            sep = r'\s+'
                            break
                except: continue
        except: pass

        # 3. Auto-Detect Timeframe (Pass Separator)
        if progress_callback: progress_callback(1, "Detecting Timeframe...")
        # Now we pass the separator so it reads correctly!
        detected_tf_minutes, detected_tf = detect_timeframe_from_csv(file_path, sep=sep)
        print(f"✅ Auto-Detected Timeframe: {detected_tf} ({detected_tf_minutes}m)")

        # 4. Multiprocessing Setup
        chunksize = 100000
        cpu_workers = max(1, cpu_count() - 1)
        
        if progress_callback:
            progress_callback(5, f"Spawning {cpu_workers} Workers...")
            
        pool = Pool(processes=cpu_workers)
        processed_chunks = []
        
        # Initialize Reader
        reader = pd.read_csv(
            file_path, 
            sep=sep, 
            chunksize=chunksize, 
            engine='python',
            on_bad_lines='skip',
            encoding_errors='ignore'
        )
        if sep == r'\s+':
             reader = pd.read_csv(file_path, sep=r'\s+', chunksize=chunksize, engine='python')
             
        # Submit Jobs
        for chunk in reader:
            # Async submit to pool
            processed_chunks.append(pool.apply_async(process_chunk_worker, (chunk,)))
        
        pool.close()
        
        # Monitor Progress & Collect
        total_chunks = len(processed_chunks)
        completed_chunks = 0
        
        for res in processed_chunks:
            # Check for cancellation during collection
            if user_id and ABORT_PROCESSING.get(user_id):
                print(f"🛑 [PARSER] Aborting collection for user {user_id}")
                pool.terminate() # Kill workers
                return None

            chunk_data = res.get() # Block until done
            completed_chunks += 1
            
            if chunk_data is not None and not chunk_data.empty:
                # Merge into dict (Blocking but fast in memory)
                # Convert to numpy arrays for speed
                times  = chunk_data['DT'].values.astype('int64') // 10**9
                opens  = chunk_data['OPEN'].values
                highs  = chunk_data['HIGH'].values
                lows   = chunk_data['LOW'].values
                closes = chunk_data['CLOSE'].values
                vols   = chunk_data['VOL'].values
                
                for t, o, h, l, c, v in zip(times, opens, highs, lows, closes, vols):
                    base_candles_dict[t] = {
                        "time": int(t),
                        "open": float(o),
                        "high": float(h),
                        "low": float(l),
                        "close": float(c),
                        "volume": int(v)
                    }
            
            if progress_callback:
                pct = 5 + int((completed_chunks / total_chunks) * 80) # 5% to 85%
                progress_callback(pct, f"Parallel Parsing... ({pct}%)")
        
        pool.join()

    except Exception as e:
        print(f"Multiprocessing Parse Error: {e}")
        import traceback
        traceback.print_exc()
        return None
        
    if not base_candles_dict: 
        print("No candles parsed (Pandas MP)")
        return None
        
    # Convert to list and sort
    base_candles = list(base_candles_dict.values())
    base_candles.sort(key=lambda x: x['time'])

    # --- AGGREGATION PHASE (Pandas Resample) ---
    
    # 1. Prepare Master DataFrame (Source of Truth)
    # This is critical for correct alignment (resample needs DateTimeIndex)
    base_df = pd.DataFrame(base_candles)
    base_df['DT'] = pd.to_datetime(base_df['time'], unit='s')
    base_df.set_index('DT', inplace=True)
    base_df.sort_index(inplace=True)

    # Build all timeframes (GOLD STANDARD: M1, M3, M5 restored)
    tf_hierarchy = [
        ("M1", 1), ("M3", 3), ("M5", 5), ("M15", 15), ("M30", 30),
        ("H1", 60), ("H2", 120), ("H4", 240), ("D1", 1440), ("W1", 10080)
    ]
    
    aggregated_tfs = {}
    
    if progress_callback: progress_callback(90, "Building M1 Backbone & Aggregating...")

    # 1. GENERATE GOLDEN 1M BACKBONE
    if detected_tf_minutes == 1:
        df_1m = base_df.copy()
    else:
        print(f"🛠️ [PARSER] Expanding {detected_tf} to 1M Backbone...")
        df_1m = expand_to_1m_backbone(base_df.copy(), detected_tf_minutes)

    # 2. AGGREGATE ALL FROM BACKBONE
    for tf_name, tf_minutes in tf_hierarchy:
        try:
            if tf_minutes == 1:
                # M1 is the backbone itself
                aggregated_tfs["M1"] = df_1m[['time', 'open', 'high', 'low', 'close', 'volume']].reset_index(drop=True).to_dict('records')
            elif tf_minutes == detected_tf_minutes:
                # If target == source, use original to avoid any tiny float/resample drift
                aggregated_tfs[tf_name] = [c.copy() for c in base_candles]
            else:
                # Aggregate using optimized resample from 1M source
                aggregated_tfs[tf_name] = resample_candles(df_1m, tf_minutes)
                
        except Exception as e:
            print(f"Failed to build {tf_name}: {e}")

    # --- FINAL SANITY CHECKS (Lock-in Phase) ---
    print(f"\n📈 [LOCK-IN] Sanity check for {symbol}:")
    for tf_name in [t[0] for t in tf_hierarchy]:
        if tf_name in aggregated_tfs:
            print(f"  {tf_name}: {len(aggregated_tfs[tf_name])} candles")
    
    # OHLC Integrity Test (Verify M5 if M1 source exists)
    if "M1" in aggregated_tfs and "M5" in aggregated_tfs and len(aggregated_tfs["M5"]) > 0:
        test_candle = aggregated_tfs["M5"][0]
        m1_start = test_candle["time"]
        m1_relevant = [c for c in aggregated_tfs["M1"] if m1_start <= c["time"] < m1_start + 300]
        if m1_relevant:
            passed = (
                test_candle["open"] == m1_relevant[0]["open"] and
                test_candle["close"] == m1_relevant[-1]["close"] and
                test_candle["high"] == max(c["high"] for c in m1_relevant) and
                test_candle["low"] == min(c["low"] for c in m1_relevant)
            )
            print(f"  OHLC Integrity (M1->M5): {'✅ PASSED' if passed else '❌ FAILED'}")

    # Build Rich Metadata
    candle_counts = {tf: len(data) for tf, data in aggregated_tfs.items()}

    result = {
        "meta": {
            "symbol": symbol,
            "source": "MT5",
            # ... [Metadata preserved] ...
            "source_tf": detected_tf,
            "original_timeframe": detected_tf,
            "csv_hash": "hash_placeholder", 
            "created_at": datetime.utcnow().isoformat() + "Z",
            "available_timeframes": list(aggregated_tfs.keys()),
            "derived_timeframes": [tf for tf in aggregated_tfs.keys() if tf != detected_tf],
            "candle_counts": candle_counts,
            "data_quality": {
                "source_count": len(base_candles),
                "detected_timeframe": detected_tf,
            }
        },
        "timeframes": aggregated_tfs
    }
    return result

# -------------------------------------------------------------
# ----------------EMAILS ----------------

def send_reset_email(user):
    token = serializer.dumps(user.email, salt="reset-password")
    link = url_for("reset_password", token=token, _external=True)

    msg = Message("Reset Your Password",
                  sender=app.config.get("MAIL_DEFAULT_SENDER"),
                  recipients=[user.email])

    msg.body = f"Hello,\n\nYou requested a password reset for your TradeJournal account. Click the link below to set a new password:\n{link}\n\nIf you did not make this request, please ignore this email."
    try:
        mail.send(msg)
    except Exception as e:
        print(f"Error sending email: {e}")

# -------------------------------------------------------------
# File upload endpoint (binary‑safe, MIME‑checked)
@app.route('/upload/<int:trade_id>', methods=['POST'])
@login_required
def upload_file(trade_id: int):
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file type'}), 400
    
    upload_folder = app.config.get('UPLOAD_FOLDER', 'static/uploads')
    trade_folder = os.path.join(upload_folder, f'trade_{trade_id}')
    os.makedirs(trade_folder, exist_ok=True)
    filename = secure_filename(file.filename)
    file_path = os.path.join(trade_folder, filename)
    # Binary write to avoid corruption
    with open(file_path, 'wb') as f:
        f.write(file.read())
    file_url = url_for('serve_file', filename=f'trade_{trade_id}/{filename}', _external=False)
    return jsonify({'success': True, 'url': file_url})
# -------------------------------------------------------------
# -------------------------------------------------------------
# Serve uploaded files with correct MIME type
@app.route('/files/<path:filename>')
@login_required
def serve_file(filename: str):
    return send_from_directory(app.config.get('UPLOAD_FOLDER', 'uploads'), filename)
# -------------------------------------------------------------

# -------------------------------------------------------------
# Serve the React frontend (built assets)
@app.route('/frontend')
@login_required
def frontend():
    return send_from_directory('../frontend', 'index.html')
# -------------------------------------------------------------

# ---------------- INIT DB ----------------

# ---------------- INIT DB ----------------

# Initialization logic moved to init_db() function check bottom of file

@app.template_filter('format_currency')
def format_currency_filter(amount):
    if amount is None:
        return "$0.00"
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):.2f}"

# ---------------- ROUTES ----------------

# Simple rate limiting for login
login_attempts = {}

@app.route("/ping")
def ping():
    return "pong", 200

@app.route('/', methods=['GET', 'POST'])
@app.route('/login', methods=['GET', 'POST'])
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        ip = request.remote_addr
        login_attempts[ip] = login_attempts.get(ip, 0) + 1
        
        if login_attempts[ip] > 10:  # Allow 10 attempts before blocking
            flash('Too many login attempts. Please try again later.')
            return redirect(url_for('index'))

        email = request.form.get('email').strip().lower()
        password = request.form.get('password')

        if not email or not password:
             flash('Please enter both email and password.')
             return redirect(url_for('index'))

        try:
             user = User.query.filter_by(email=email).first()
        except Exception as e:
             # Safety for DB crashes (like "no such table" if create_all failed)
             print(f"❌ DB ERROR IN LOGIN: {e}")
             flash("Database error. Please check logs.")
             return redirect(url_for('index'))

        print(f"DEBUG LOGIN: Email={email}, Found={bool(user)}")
        
        if not user:
            flash("User not found")
            return redirect(url_for("index"))

        if user:
             print(f"DEBUG HASH: {user.password_hash}")
             print(f"DEBUG CHECK: {user.check_password(password)}")
             
        if user.check_password(password):
            login_attempts[ip] = 0  # Reset on success
            login_user(user)
            
            # 🔥 Redirection Logic based on account_type
            if user.account_type == 'backtest':
                return redirect(url_for('chart_page'))
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password')

    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
@app.route('/signup', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        password = request.form.get('password')
        confirm = request.form.get('confirm_password')

        # Auto-generate username from email
        base_username = email.split('@')[0]
        # Clean username
        import re
        base_username = re.sub(r'[^a-zA-Z0-9]', '_', base_username)
        username = base_username
        
        # Ensure uniqueness
        if User.query.filter_by(username=username).first():
            import random
            username = f"{base_username}_{random.randint(100, 999)}"
        
        name = username # Default name to username

        if not email or not password:
            flash('All fields are required')
            return redirect(url_for('register'))

        if password != confirm:
            flash('Passwords do not match')
            return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():
            flash('Email already registered')
            return redirect(url_for('register'))

        account_type = request.form.get('account_type', 'journal')
        user = User(username=username, name=name, email=email, account_type=account_type)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        flash('Account created! You can now log in.')
        return redirect(url_for('index'))

    return render_template('register.html')


@app.route('/forgot', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(email=email).first()

        if user:
            send_reset_email(user)
        
        flash("If that email is in our system, we've sent a reset link.")
        return redirect(url_for("index"))

    return render_template('forgot_password.html')

@app.route('/reset/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        email = serializer.loads(token, salt="reset-password", max_age=3600)
    except:
        flash("Reset link expired or invalid")
        return redirect(url_for("index"))

    if request.method == 'POST':
        password = request.form.get('password')
        confirm = request.form.get('confirm_password')
        
        if password != confirm:
            flash("Passwords do not match")
            return render_template('reset_password.html', token=token)

        user = User.query.filter_by(email=email).first()
        if user:
            user.set_password(password)
            db.session.commit()
            flash("Password updated! You can now log in.")
            return redirect(url_for("index"))

    return render_template('reset_password.html', token=token)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/chart')
@login_required
def chart_page():
    # Access Check: Backtest user only
    # Treat None as 'journal'
    if current_user.account_type != 'backtest':
        return redirect(url_for('dashboard'))
    
    user_base = get_user_mt5_base(current_user.id)
    active_symbol = session.get('active_symbol')
    
    # 1. Validate Active Symbol if present
    if active_symbol:
        symbol_folder = get_symbol_folder(current_user.id, active_symbol)
        try:
            # Check for meaningful data presence
            has_meta = os.path.exists(os.path.join(symbol_folder, 'meta.json'))
            has_processed = os.path.exists(os.path.join(symbol_folder, 'processed'))
            has_status = os.path.exists(os.path.join(symbol_folder, 'status.json'))
            
            if not (has_meta or has_processed or has_status):
                print(f"⚠️ Active symbol {active_symbol} found empty/invalid. Clearing from session.")
                session.pop('active_symbol', None)
                active_symbol = None
        except Exception as e:
            print(f"Error validating symbol {active_symbol}: {e}")
            active_symbol = None

    # 2. Fallback Selection: If no active symbol, pick latest valid one
    if not active_symbol:
        if os.path.exists(user_base):
            all_symbols = [d for d in os.listdir(user_base) if os.path.isdir(os.path.join(user_base, d))]
            valid_symbols = []
            for s in all_symbols:
                s_path = os.path.join(user_base, s)
                if os.path.exists(os.path.join(s_path, 'meta.json')) or os.path.exists(os.path.join(s_path, 'processed')):
                    valid_symbols.append(s)
            
            if valid_symbols:
                # Sort by recently modified
                valid_symbols.sort(key=lambda d: os.path.getmtime(os.path.join(user_base, d)), reverse=True)
                active_symbol = valid_symbols[0]
                session['active_symbol'] = active_symbol
    
    status = "IDLE"
    meta = None
    
    if active_symbol:
        symbol_folder = get_symbol_folder(current_user.id, active_symbol)
        status_path = os.path.join(symbol_folder, 'status.json')
        meta_path = os.path.join(symbol_folder, 'meta.json')
        
        if os.path.exists(status_path):
            with open(status_path, 'r') as f:
                try:
                    status_data = json.load(f)
                    status = status_data.get('status', 'IDLE')
                except: status = 'IDLE'
        
        if os.path.exists(meta_path):
            with open(meta_path, 'r') as f:
                try: meta = json.load(f)
                except: meta = None
            
    return render_template('chart.html', symbol=active_symbol, meta=meta, processing_status=status)


@app.route('/upload-mt5-csv', methods=['POST'])
@login_required
def upload_mt5_csv():
    if 'file' not in request.files:
        flash('No file part')
        return redirect(url_for('chart_page'))
    
    file = request.files['file']
    if file.filename == '':
        flash('No selected file')
        return redirect(url_for('chart_page'))
    
    if file and file.filename.lower().endswith('.csv'):
        filename = secure_filename(file.filename)
        
        # 💡 Extract symbol from filename (e.g., NAS100_M1.csv -> NAS100)
        symbol = filename.split('_')[0].upper()
        if not symbol: symbol = "UNKNOWN"
        
        print(f"📂 [UPLOAD] File: {filename} -> Symbol: {symbol}")

        symbol_folder = get_symbol_folder(current_user.id, symbol)
        raw_folder = os.path.join(symbol_folder, 'raw')
        os.makedirs(raw_folder, exist_ok=True)
        
        file_path = os.path.join(raw_folder, 'original.csv')
        file.save(file_path)
        
        # Calculate hash for duplicate detection
        file_hash = hash_file(file_path)
        print(f"#️⃣ [UPLOAD] File Hash: {file_hash}")
        meta_path = os.path.join(symbol_folder, 'meta.json')
        meta_path = os.path.join(symbol_folder, 'meta.json')
        status_path = os.path.join(symbol_folder, 'status.json')
        
        should_process = True
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                    if meta.get('csv_hash') == file_hash:
                        print(f"⚡ Cache hit for {symbol} (hash matched)")
                        should_process = False
                        with open(status_path, 'w') as sf:
                            json.dump({"state": "READY"}, sf)
            except Exception:
                pass

        if should_process:
            print(f"🔄 Starting hierarchical processing for {symbol}...")
            # Clear abort flag for this user before starting
            ABORT_PROCESSING[current_user.id] = False
            thread = threading.Thread(target=background_process_csv, args=(file_path, symbol_folder, symbol, current_user.id))
            thread.start()
        
        session['active_symbol'] = symbol
        msg = f'MT5 CSV for {symbol} uploaded! Processing in background...' if should_process else f'MT5 CSV for {symbol} uploaded (Used Cache)!'
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.args.get('ajax') == '1':
            return jsonify({
                "status": "success",
                "message": msg,
                "symbol": symbol,
                "should_process": should_process
            })

        flash(msg)
        return redirect(url_for('chart_page'))
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.args.get('ajax') == '1':
         return jsonify({"status": "error", "message": "Invalid file type. Please upload a CSV."})

    flash('Invalid file type. Please upload a CSV.')
    return redirect(url_for('chart_page'))


@app.route('/upload-mt5-folder', methods=['POST'])
@login_required
def upload_mt5_folder():
    if 'files' not in request.files:
        flash('No files part')
        return redirect(url_for('settings'))
        
    files = request.files.getlist('files')
    if not files or files[0].filename == '':
        flash('No files selected')
        return redirect(url_for('settings'))

    # Filter for valid CSVs
    csv_files = [f for f in files if f.filename.lower().endswith('.csv')]
    
    if not csv_files:
        flash('No CSV files found in the folder.')
        return redirect(url_for('settings'))

    print(f"📂 [UPLOAD FOLDER] Received {len(csv_files)} CSVs. Analyzing hierarchy...")

    # Group files by Symbol
    symbol_groups = {}
    
    for f in csv_files:
        raw_path = f.filename.replace('\\', '/')
        parts = raw_path.split('/')
        
        symbol = "UNKNOWN"
        
        if len(parts) > 1:
             possible = parts[-2].upper()
             if possible.isalnum() or '_' in possible:
                 symbol = possible
        
        if symbol == "UNKNOWN" or symbol.upper() in ["DATA", "QUOTES", "HISTORY"]:
            basename = secure_filename(parts[-1])
            if '_' in basename:
                symbol = basename.split('_')[0].upper()
        
        if symbol == "UNKNOWN":
             continue
             
        if symbol not in symbol_groups:
            symbol_groups[symbol] = []
        symbol_groups[symbol].append(f)
    
    if not symbol_groups:
        flash('Could not identify any symbols from folder structure. Please use "Symbol/file.csv" structure.')
        return redirect(url_for('settings'))

    processed_count = 0
    
    for symbol, sym_files in symbol_groups.items():
        best_candidate = None
        for f in sym_files:
            if "_M1" in f.filename.upper() or "M1.CSV" in f.filename.upper():
                best_candidate = f
                break
        
        if not best_candidate: best_candidate = sym_files[0]
        
        file = best_candidate
        filename = secure_filename(file.filename)
        
        print(f"🚀 Processing group {symbol} using {filename}")

        symbol_folder = get_symbol_folder(current_user.id, symbol)
        raw_folder = os.path.join(symbol_folder, 'raw')
        os.makedirs(raw_folder, exist_ok=True)
        
        file_path = os.path.join(raw_folder, 'original.csv')
        
        file.seek(0)
        file.save(file_path)
        
        status_path = os.path.join(symbol_folder, 'status.json')
        meta_path = os.path.join(symbol_folder, 'meta.json')
        file_hash = hash_file(file_path)
        
        should_process = True
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                    if meta.get('csv_hash') == file_hash:
                        should_process = False
                        with open(status_path, 'w') as sf:
                            json.dump({"state": "READY"}, sf)
            except: pass

        if should_process:
            # Clear abort flag for this user
            ABORT_PROCESSING[current_user.id] = False
            thread = threading.Thread(target=background_process_csv, args=(file_path, symbol_folder, symbol, current_user.id))
            thread.start()
            
        processed_count += 1
    
    summary = ", ".join(list(symbol_groups.keys())[:3])
    msg = f'Folder uploaded! Processing {processed_count} symbols ({summary}...)'
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.args.get('ajax') == '1':
        return jsonify({
            "status": "success",
            "message": msg,
            "symbol": list(symbol_groups.keys())[0], # Return the first symbol for status tracking
            "should_process": True
        })

    flash(msg)
    return redirect(url_for('settings'))


@app.route('/api/symbol-meta/<symbol>')
@login_required
def symbol_meta_api(symbol):
    # Returns available timeframes for a symbol based on processed data
    symbol_folder = get_symbol_folder(current_user.id, symbol)
    meta_path = os.path.join(symbol_folder, 'meta.json')
    
    if os.path.exists(meta_path):
        try:
            with open(meta_path, 'r') as f:
                rich_meta = json.load(f)
            
            if 'available_timeframes' not in rich_meta:
                 processed_base = os.path.join(symbol_folder, 'processed')
                 if os.path.exists(processed_base):
                     found_tfs = []
                     for d in os.listdir(processed_base):
                         if os.path.isdir(os.path.join(processed_base, d)) and d in ALLOWED_TIMEFRAMES:
                             found_tfs.append(d)
                     rich_meta['available_timeframes'] = found_tfs

            return jsonify(rich_meta)
        except Exception as e:
            print(f"Error reading meta.json for {symbol}: {e}")

    processed_base = os.path.join(symbol_folder, 'processed')
    available_timeframes = []
    
    if os.path.exists(processed_base):
        for d in os.listdir(processed_base):
            if os.path.isdir(os.path.join(processed_base, d)) and d in ALLOWED_TIMEFRAMES:
                available_timeframes.append(d)
    
    tf_order = {tf: i for i, tf in enumerate(ALLOWED_TIMEFRAMES)}
    available_timeframes.sort(key=lambda x: tf_order.get(x, 999))
    
    return jsonify({
        "symbol": symbol,
        "base_tf": "M1",
        "available_timeframes": available_timeframes
    })


@app.route('/api/mt5-data/<symbol>/<timeframe>')
@login_required
def get_mt5_timeframe_data(symbol, timeframe):
    tf_upper = timeframe.upper()
    print(f"🔥 [API] Request: {symbol} {tf_upper}") 
    if tf_upper not in ALLOWED_TIMEFRAMES:
        abort(400, description="Invalid timeframe")
        
    symbol_folder = get_symbol_folder(current_user.id, symbol)
    zone_file = os.path.join(symbol_folder, 'processed', tf_upper, 'zone.json')
    if not os.path.exists(zone_file):
        zone_file = os.path.join(symbol_folder, 'processed', tf_upper, 'candles.json')
    
    if not os.path.exists(zone_file):
        abort(404, description="Data not found")
        
    with open(zone_file, 'r') as f:
        data = json.load(f)
        
    if isinstance(data, list):
        data = {"symbol": symbol, "timeframe": tf_upper, "candles": data}
        
    from_ts = request.args.get('from', type=int)
    if from_ts:
        data['candles'] = [c for c in data['candles'] if c['time'] >= from_ts]

    to_ts = request.args.get('to', type=int)
    if to_ts:
        data['candles'] = [c for c in data['candles'] if c['time'] <= to_ts]

    # Limit logic restored for performance (optional)
    limit = request.args.get('limit', type=int)
    if limit and limit > 0:
        if len(data['candles']) > limit:
            data['candles'] = data['candles'][-limit:]

    print(f"✅ [API] Serving {len(data['candles'])} candles for {symbol} ({tf_upper})")
    return jsonify(data)


@app.route('/api/mt5-status')
@login_required
def mt5_status_api():
    active_symbol = session.get('active_symbol')
    if not active_symbol:
        return jsonify({"state": "IDLE"})
    
    symbol_folder = get_symbol_folder(current_user.id, active_symbol)
    status_path = os.path.join(symbol_folder, 'status.json')
    
    if os.path.exists(status_path):
        with open(status_path, 'r') as f:
            status_data = json.load(f)
            if 'status' in status_data and 'state' not in status_data:
                status_data['state'] = status_data.pop('status')
            return jsonify(status_data)
            
    return jsonify({"state": "IDLE"})


@app.route('/api/mt5-symbols')
@login_required
def get_mt5_symbols():
    user_base = get_user_mt5_base(current_user.id)
    if not os.path.exists(user_base):
        return jsonify({"symbols": []})
        
    all_symbols = [d for d in os.listdir(user_base) if os.path.isdir(os.path.join(user_base, d))]
    valid_symbols = []
    
    for s in all_symbols:
        s_path = os.path.join(user_base, s)
        # Check for valid data markers
        if os.path.exists(os.path.join(s_path, 'meta.json')) or \
           os.path.exists(os.path.join(s_path, 'processed')) or \
           os.path.exists(os.path.join(s_path, 'status.json')):
            valid_symbols.append(s)
            
    return jsonify({"symbols": sorted(valid_symbols)})


@app.route('/dashboard')
@login_required
def dashboard():
    # 🔥 Access Check: Journal user only (treat None as journal)
    if current_user.account_type == 'backtest':
        return redirect(url_for('chart_page'))

    trades = Trade.query.filter_by(user_id=current_user.id, is_deleted=False).order_by(Trade.date.asc()).all()

    total_trades = len(trades)
    net_profit = sum(t.pnl for t in trades)
    
    wins = len([t for t in trades if t.pnl > 0])
    losses = len([t for t in trades if t.pnl < 0])

    win_rate = round((wins / total_trades) * 100, 2) if total_trades else 0
    profit_factor = round(sum(t.pnl for t in trades if t.pnl > 0) / abs(sum(t.pnl for t in trades if t.pnl < 0)), 2) if any(t.pnl < 0 for t in trades) else 0
    
    # Calculate Equity Curve
    equity_labels = []
    equity_data = []
    running_balance = current_user.initial_balance
    
    # Add initial point
    equity_labels.append('Start')
    equity_data.append(running_balance)

    for t in trades:
        running_balance += t.pnl
        date_str = t.date.strftime('%Y-%m-%d') if t.date else "N/A"
        equity_labels.append(date_str)
        equity_data.append(round(running_balance, 2))

    stats = {
        "net_profit": net_profit,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "total_trades": total_trades,
        "todays_pnl": 0,
        "risk_alert": False,
        "current_balance": current_user.initial_balance + net_profit
    }
    
    # Calculate today's PnL correctly using SQL (Prop-Firm Standard)
    # Using 'trade' table (singular) as confirmed by model definition
    # COALESCE ensures we get 0 instead of None if no trades today
    # Calculate today's PnL correctly using Broker Day Window (03:30 IST)
    start_utc, end_utc = get_broker_day_window()
    
    today_pnl_row = db.session.execute(text("SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE date >= :start AND date < :end AND (is_deleted = FALSE OR is_deleted IS NULL)"), {"start": start_utc, "end": end_utc}).fetchone()
    
    todays_trades = [t for t in trades if t.date and t.date.date() == date.today()] # Simple day check if utc/ist not critical here, but ideally uses window
    # Actually, let's use all_trades for global gross profit to be accurate across pagination if any
    all_user_trades = Trade.query.filter_by(user_id=current_user.id, is_deleted=False).all()
    all_time_gross_profit = sum(t.pnl for t in all_user_trades if t.pnl > 0)
    
    # Professional Risk Tracking: Profit doesn't buffer loss limit
    todays_gross_loss = abs(sum(t.pnl for t in all_user_trades if t.date and t.date.date() == date.today() and t.pnl < 0))

    stats['todays_pnl'] = sum(t.pnl for t in all_user_trades if t.date and t.date.date() == date.today())
    stats['todays_gross_loss'] = todays_gross_loss

    # --- Goals & Limits Logic ---
    goals_row = db.session.execute(text("SELECT profit_target, max_daily_loss FROM risk_settings LIMIT 1")).fetchone()
    
    # Defaults in case of error (though migration ensures they exist)
    profit_target = goals_row[0] if goals_row else 800.0
    max_daily_loss = goals_row[1] if goals_row else 500.0

    # 1. Profit Target Logic (Based on GROSS PROFIT)
    # Goal: Losses shouldn't discourage progress toward the "Take Profit" milestone.
    profit_progress = 0
    if profit_target > 0:
        profit_progress = min(max((all_time_gross_profit / profit_target) * 100, 0), 100)
    
    profit_status = "Reached" if all_time_gross_profit >= profit_target else "Active"

    # 2. Max Daily Loss Logic (Based on GROSS LOSS Today)
    # Rule: Profit doesn't "save" you fromhitting your drawdown limit.
    daily_loss_progress = 0
    if max_daily_loss > 0:
        daily_loss_progress = min(max(todays_gross_loss / max_daily_loss * 100, 0), 100)
    
    # Status: Breached if today's gross loss exceeds limit
    daily_status = "Breached" if todays_gross_loss >= max_daily_loss else "Safe"
    
    # 3. Discipline Score Logic (Average of ALL active trades)
    avg_discipline_raw = calculate_discipline(current_user.id, limit=None)
    
    # Internal score (0-100) based on 1-5 raw average
    internal_score = (avg_discipline_raw * 20) if avg_discipline_raw else 0
    discipline_1_5 = avg_discipline_raw if avg_discipline_raw else 0

    # Map 1-5 Average to Label
    if avg_discipline_raw and avg_discipline_raw >= 4.5:
        discipline_label = "Elite Discipline"
        discipline_color = "text-green-400"
    elif avg_discipline_raw and avg_discipline_raw >= 3.7:
        discipline_label = "Very Good"
        discipline_color = "text-blue-400"
    elif avg_discipline_raw and avg_discipline_raw >= 3.0:
        discipline_label = "Average"
        discipline_color = "text-yellow-400"
    elif avg_discipline_raw and avg_discipline_raw >= 2.0:
        discipline_label = "Poor"
        discipline_color = "text-orange-400"
    elif avg_discipline_raw:
        discipline_label = "Undisciplined"
        discipline_color = "text-red-500"
    else:
        discipline_label = "No Data"
        discipline_color = "text-gray-500"

    emotion_score = calculate_emotion_score(current_user.id, limit=None)

    goals = {
        "profit_target": profit_target,
        "max_daily_loss": max_daily_loss,
        "profit_progress": profit_progress,
        "profit_status": profit_status,
        "daily_loss_progress": daily_loss_progress,
        "daily_status": daily_status,
        "avg_discipline": avg_discipline_raw if avg_discipline_raw else 0.0,
        "discipline_1_5": discipline_1_5,
        "has_discipline": avg_discipline_raw is not None,
        "discipline_label": discipline_label,
        "discipline_color": discipline_color,
        "emotion_score": emotion_score
    }

    # --- Prop-Firm Calendar Logic ---
    today = date.today()
    curr_year = today.year
    curr_month = today.month
    
    # Get all trades for the current month (using broker-day logic for grouping)
    # SQLite logic: date(date, '+2 hours') shifts 10:00 PM UTC to 12:00 AM next day
    month_str = f"{curr_year}-{curr_month:02d}"
    
    # Check dialect for correct SQL syntax
    is_sqlite = 'sqlite' in db.engine.dialect.name
    
    if is_sqlite:
        sql = "SELECT date(date, '+2 hours') AS broker_day, SUM(pnl) AS pnl, COUNT(*) AS trade_count FROM trades WHERE user_id = :uid AND (is_deleted = FALSE OR is_deleted IS NULL) AND strftime('%Y-%m', datetime(date, '+2 hours')) = :month GROUP BY broker_day ORDER BY broker_day"
    else:
        sql = "SELECT (date + INTERVAL '2 hours')::date AS broker_day, SUM(pnl) AS pnl, COUNT(*) AS trade_count FROM trades WHERE user_id = :uid AND (is_deleted = FALSE OR is_deleted IS NULL) AND TO_CHAR(date + INTERVAL '2 hours', 'YYYY-MM') = :month GROUP BY broker_day ORDER BY broker_day"

    calendar_data_rows = db.session.execute(text(sql), {"uid": current_user.id, "month": month_str}).fetchall()
    
    calendar_map = {row.broker_day: {"pnl": row.pnl, "count": row.trade_count, "breached": (row.pnl < 0 and abs(row.pnl) >= max_daily_loss)} 
                    for row in calendar_data_rows}
    
    # Generate Calendar Grid
    cal = calendar.Calendar(firstweekday=6) # Sunday start
    month_days = cal.monthdatescalendar(curr_year, curr_month)
    
    calendar_weeks = []
    monthly_total_pnl = 0
    
    for week in month_days:
        week_days = []
        for d in week:
            d_str = d.strftime('%Y-%m-%d')
            day_info = {
                "date": d_str,
                "day_num": d.day,
                "is_current_month": d.month == curr_month,
                "data": calendar_map.get(d_str)
            }
            if day_info["is_current_month"] and day_info["data"]:
                monthly_total_pnl += day_info["data"]["pnl"]
            week_days.append(day_info)
        calendar_weeks.append(week_days)

    return render_template('dashboard.html', 
                         base_balance=current_user.initial_balance,
                         stats=stats, 
                         trades=trades[::-1][:10], 
                         equity_labels=equity_labels, 
                         equity_data=equity_data, 
                         goals=goals,
                         calendar_weeks=calendar_weeks,
                         monthly_total_pnl=monthly_total_pnl,
                         current_month_name=calendar.month_name[curr_month])


@app.route('/api/calendar_data')
@login_required
def calendar_api():
    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', date.today().month, type=int)
    
    # Get risk settings for breach check
    # We use text() for consistency with existing SQL in app.py
    goals_row = db.session.execute(text("SELECT max_daily_loss FROM risk_settings LIMIT 1")).fetchone()
    max_daily_loss = goals_row[0] if goals_row else 500.0
    
    month_str = f"{year}-{month:02d}"
    
    # Check dialect for correct SQL syntax
    is_sqlite = 'sqlite' in db.engine.dialect.name
    
    if is_sqlite:
        sql = "SELECT date(date, '+2 hours') AS broker_day, SUM(pnl) AS pnl, COUNT(*) AS trade_count FROM trades WHERE user_id = :uid AND (is_deleted = FALSE OR is_deleted IS NULL) AND strftime('%Y-%m', datetime(date, '+2 hours')) = :month GROUP BY broker_day ORDER BY broker_day"
    else:
        sql = "SELECT (date + INTERVAL '2 hours')::date AS broker_day, SUM(pnl) AS pnl, COUNT(*) AS trade_count FROM trades WHERE user_id = :uid AND (is_deleted = FALSE OR is_deleted IS NULL) AND TO_CHAR(date + INTERVAL '2 hours', 'YYYY-MM') = :month GROUP BY broker_day ORDER BY broker_day"
    
    calendar_data_rows = db.session.execute(text(sql), {"uid": current_user.id, "month": month_str}).fetchall()
    
    calendar_map = {str(row.broker_day): {
        "pnl": float(row.pnl), 
        "count": int(row.trade_count), 
        "breached": (row.pnl < 0 and abs(row.pnl) >= max_daily_loss)
    } for row in calendar_data_rows}
    
    monthly_total = sum(row.pnl for row in calendar_data_rows)
    
    return jsonify({
        "status": "success",
        "month": month,
        "year": year,
        "data": calendar_map,
        "monthly_total": float(monthly_total)
    })


@app.route('/account/update', methods=['POST'])
@login_required
def update_account():
    name = request.form.get('account_name')
    balance = request.form.get('initial_balance')

    if name:
        current_user.account_name = name
    if balance:
        try:
            current_user.initial_balance = float(balance)
        except ValueError:
            pass # Ignore invalid numbers

    db.session.commit()
    return redirect(url_for('dashboard'))

@app.route('/account/update_mode', methods=['POST'])
@login_required
def update_account_mode():
    new_mode = request.form.get('account_type')
    if new_mode in ['journal', 'backtest']:
        current_user.account_type = new_mode
        db.session.commit()
        
        if new_mode == 'backtest':
            return redirect(url_for('chart_page'))
        return redirect(url_for('dashboard'))
    return redirect(url_for('settings'))


@app.route('/api/toggle-mode', methods=['POST'])
@login_required
def toggle_mode_api():
    is_on = request.json.get('isOn')
    new_mode = 'backtest' if is_on else 'journal'
    mode_label = 'Backtesting' if is_on else 'Trading Journal'
    
    current_user.account_type = new_mode
    db.session.commit()
    
    return jsonify({
        'success': True,
        'mode': new_mode,
        'message': f'Switched to {mode_label} Mode'
    })


@app.route('/account/update_goals', methods=['POST'])
@login_required
def update_goals():
    goal = request.form.get('monthly_goal')
    limit = request.form.get('daily_loss_limit')

    if goal and limit:
        try:
            target = float(goal)
            max_loss = float(limit)
            
            # Update the single row in risk_settings
            # We assume ID 1 exists because of the migration
            db.session.execute(text("UPDATE risk_settings SET profit_target = :t, max_daily_loss = :l WHERE id = (SELECT id FROM risk_settings LIMIT 1)"), {"t": target, "l": max_loss})
            db.session.commit()
        except ValueError:
            flash("Invalid values provided")

    return redirect(url_for('dashboard'))


@app.route('/new-entry', methods=['GET', 'POST'])
@login_required
def new_entry():
    # 🔥 Access Check: Journal user only
    if current_user.account_type != 'journal':
        return redirect(url_for('chart_page'))

    if request.method == 'POST':
        symbol = request.form.get('symbol').upper()
        direction = request.form.get('direction')
        qty_val = request.form.get('quantity')
        quantity = float(qty_val) if qty_val and qty_val.strip() else 1.0
        entry = float(request.form.get('entry_price'))
        pnl = float(request.form.get('pnl'))
        
        notes = request.form.get('notes')
        strategy = request.form.get('strategy')
        session_time = request.form.get('session')
        discipline_raw = request.form.get('discipline')
        discipline = int(discipline_raw) if discipline_raw else 0
        date_str = request.form.get('date')
        
        duration_val = request.form.get('duration')
        duration = int(float(duration_val) * 60) if duration_val and duration_val.strip() else None
        
        screenshot_json = None
        screenshots = []
        

        
        # 1. Handle Multiple File Uploads
        uploaded_map = {}
        if 'screenshot' in request.files:
            files = request.files.getlist('screenshot')
            for file in files:
                if file and file.filename != '':
                    allowed_mimetypes = {'image/png', 'image/jpeg', 'image/gif', 'application/pdf', 'video/mp4'}
                    if file.mimetype in allowed_mimetypes:
                        filename = secure_filename(file.filename)
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
                        saved_filename = timestamp + filename
                        file_path = os.path.join(app.config['UPLOAD_FOLDER'], saved_filename)
                        with open(file_path, 'wb') as f:
                            f.write(file.read())
                        uploaded_map[file.filename] = 'uploads/' + saved_filename

        # Apply Order if provided
        screenshot_order = request.form.get('screenshot_order')
        if screenshot_order:
            try:
                order_list = json.loads(screenshot_order)
                for name in order_list:
                    if name in uploaded_map:
                        screenshots.append(uploaded_map[name])
                        del uploaded_map[name]
            except:
                pass
        
        # Append remaining uploads
        for path in uploaded_map.values():
            screenshots.append(path)
        
        # 2. Handle Image URL
        screenshot_url = request.form.get('screenshot_url')
        if screenshot_url and screenshot_url.strip():
            screenshots.append(screenshot_url.strip())

        if screenshots:
            screenshot_data = json.dumps(screenshots)
        else:
            screenshot_data = None

        stop_loss = request.form.get('stop_loss')
        take_profit = request.form.get('take_profit')
        stop_loss = float(stop_loss) if stop_loss else None
        take_profit = float(take_profit) if take_profit else None

        direction_mod = 1 if direction == "Long" else -1
        # Calculate Exit Price from PnL
        # PnL = (Exit - Entry) * Qty * Dir
        # Exit = Entry + (PnL / (Qty * Dir))
        exit_price = entry + (pnl / (quantity * direction_mod))
        
        result = "Win" if pnl > 0 else ("Loss" if pnl < 0 else "BE")

        emotion = request.form.get('emotion')
        
        # Calculate R:R
        rr = calculate_rr(entry, stop_loss, take_profit, direction)

        trade = Trade(
            user_id=current_user.id,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            entry_price=entry,
            exit_price=exit_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            pnl=pnl,
            result=result,
            notes=notes,
            strategy=strategy,
            screenshot=screenshot_data,
            session=session_time,
            discipline=discipline,
            emotion=emotion,
            rr=rr,
            duration=duration
        )

        if date_str:
            trade.date = datetime.strptime(date_str, '%Y-%m-%dT%H:%M')

        db.session.add(trade)
        db.session.commit()
        return redirect(url_for('journal'))

        return redirect(url_for('journal'))

    avg_rr = get_avg_rr(current_user.id)
    return render_template('new_entry.html', avg_rr=avg_rr)


@app.route('/journal')
@login_required
def journal():
    # 🔥 Access Check: Journal user only
    if current_user.account_type != 'journal':
        return redirect(url_for('chart_page'))

    view = request.args.get('view', 'active')
    
    # Filters
    symbol = request.args.get('symbol')
    direction = request.args.get('direction')
    tag = None
    date_filter = request.args.get('date')
    emotion_filter = request.args.get('emotion')
    min_rr = request.args.get('min_rr')

    query = Trade.query.filter_by(user_id=current_user.id)
    
    if view == 'trash':
        query = query.filter_by(is_deleted=True)
    else:
        query = query.filter_by(is_deleted=False)
        
    if symbol:
        query = query.filter(Trade.symbol == symbol)
    if direction:
        query = query.filter(Trade.direction == direction)
    if tag:
        search = f"%{tag.lower()}%"
        query = query.filter(Trade.tags.like(search))
    
    if date_filter:
        # Broker day filter: trades where (date + INTERVAL '2 hours')::date == date_filter
        is_sqlite = 'sqlite' in db.engine.dialect.name
        if is_sqlite:
            query = query.filter(text("date(date, '+2 hours') = :d")).params(d=date_filter)
        else:
            query = query.filter(text("(date + INTERVAL '2 hours')::date = :d")).params(d=date_filter)
    
    if emotion_filter:
        query = query.filter(Trade.emotion == emotion_filter)

    if min_rr:
        query = query.filter(Trade.rr >= float(min_rr))

    # Pagination Logic
    import math
    PER_PAGE = 10
    page = request.args.get('page', 1, type=int)
    
    total_trades_count = query.count()
    total_pages = math.ceil(total_trades_count / PER_PAGE)
    
    trades = query.order_by(Trade.date.desc())\
                  .offset((page - 1) * PER_PAGE)\
                  .limit(PER_PAGE)\
                  .all()
        
    # Auto-cleanup old trash (permanently delete > 2 days)
    expiration_date = datetime.utcnow() - timedelta(days=2)
    try:
        deleted_count = Trade.query.filter(
            Trade.user_id == current_user.id, 
            Trade.is_deleted == True, 
            Trade.deleted_at < expiration_date
        ).delete()
        if deleted_count > 0:
            db.session.commit()
            print(f"♻️ Auto-cleaned {deleted_count} old trash items")
    except Exception as e:
        print(f"⚠️ Auto-cleanup failed: {e}")

    return render_template('journal.html', 
                         trades=trades, 
                         view=view, 
                         date_filter=date_filter,
                         page=page,
                         total_pages=total_pages,
                         total_trades=total_trades_count)




@app.route('/trade/edit/<int:trade_id>', methods=['GET', 'POST'])
@login_required
def edit_trade(trade_id):
    trade = Trade.query.get_or_404(trade_id)
    if trade.user_id != current_user.id:
        return redirect(url_for('journal'))

    if request.method == 'POST':
        trade.symbol = request.form.get('symbol').upper()
        trade.direction = request.form.get('direction')
        trade.quantity = float(request.form.get('quantity', 1))
        trade.entry_price = float(request.form.get('entry_price'))
        
        # Calculate Exit Price from new PnL
        new_pnl = float(request.form.get('pnl'))
        trade.pnl = new_pnl
        direction_mod = 1 if trade.direction == 'Long' else -1
        trade.exit_price = trade.entry_price + (new_pnl / (trade.quantity * direction_mod))
        
        trade.result = 'Win' if trade.pnl > 0 else ('Loss' if trade.pnl < 0 else 'BE')

        trade.notes = request.form.get('notes')
        trade.strategy = request.form.get('strategy')
        trade.session = request.form.get('session')
        
        discipline_raw = request.form.get('discipline')
        trade.discipline = int(discipline_raw) if discipline_raw else 0
        trade.emotion = request.form.get('emotion', trade.emotion)

        duration_val = request.form.get('duration')
        trade.duration = int(float(duration_val) * 60) if duration_val and duration_val.strip() else None
        
        date_str = request.form.get('date')

        # Handle Screenshots
        current_screenshots = []
        if trade.screenshot:
            try: current_screenshots = json.loads(trade.screenshot)
            except: current_screenshots = [trade.screenshot] if trade.screenshot else []
        
        new_upload_map = {}
        if 'screenshot' in request.files:
            files = request.files.getlist('screenshot')
            for file in files:
                if file and file.filename != '':
                    allowed_mimetypes = {'image/png', 'image/jpeg', 'image/gif', 'application/pdf', 'video/mp4'}
                    if file.mimetype in allowed_mimetypes:
                        filename = secure_filename(file.filename)
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
                        saved_filename = timestamp + filename
                        file_path = os.path.join(app.config['UPLOAD_FOLDER'], saved_filename)
                        with open(file_path, 'wb') as f:
                            f.write(file.read())
                        # Map original filename to saved path for ordering
                        new_upload_map[file.filename] = 'uploads/' + saved_filename
        
        screenshot_url = request.form.get('screenshot_url')
        new_url = screenshot_url.strip() if screenshot_url and screenshot_url.strip() else None
            
        # 3. Final Ordering Logic
        final_screenshots = []
        order_json = request.form.get('screenshot_order')
        if order_json:
            try:
                order = json.loads(order_json)
                for item in order:
                    if item in current_screenshots:
                        final_screenshots.append(item)
                    elif item in new_upload_map:
                        final_screenshots.append(new_upload_map[item])
                    elif item == new_url:
                        final_screenshots.append(item)
                        new_url = None # Used
                
                # Append any new uploads NOT in order just in case
                for key, val in new_upload_map.items():
                    if val not in final_screenshots:
                        final_screenshots.append(val)
                if new_url:
                    final_screenshots.append(new_url)
            except:
                final_screenshots = current_screenshots + list(new_upload_map.values())
                if new_url: final_screenshots.append(new_url)
        else:
            final_screenshots = current_screenshots + list(new_upload_map.values())
            if new_url: final_screenshots.append(new_url)
            
        trade.screenshot = json.dumps(final_screenshots) if final_screenshots else None

        stop_loss = request.form.get('stop_loss')
        take_profit = request.form.get('take_profit')
        trade.stop_loss = float(stop_loss) if stop_loss else None
        trade.take_profit = float(take_profit) if take_profit else None

        # Update R:R
        trade.rr = calculate_rr(trade.entry_price, trade.stop_loss, trade.take_profit, trade.direction)

        date_str = request.form.get('date')
        if date_str:
            trade.date = datetime.strptime(date_str, '%Y-%m-%dT%H:%M')

        db.session.commit()
        return redirect(url_for('journal'))

    screenshots = []
    if trade.screenshot:
        try:
            screenshots = json.loads(trade.screenshot)
        except:
            if trade.screenshot:
                screenshots = [trade.screenshot]

    avg_rr = get_avg_rr(current_user.id)
    return render_template('edit_trade.html', trade=trade, screenshots=screenshots, avg_rr=avg_rr)


@app.route('/trade/<int:trade_id>/add_url', methods=['POST'])
@login_required
def add_trade_url(trade_id):
    trade = Trade.query.get_or_404(trade_id)
    if trade.user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    # Load existing
    current_screenshots = []
    if trade.screenshot:
        try: 
            current_screenshots = json.loads(trade.screenshot)
        except: 
            current_screenshots = [trade.screenshot]

    current_screenshots.append(url)
    trade.screenshot = json.dumps(current_screenshots)
    
    db.session.commit()
    return jsonify({'success': True})


@app.route('/trade/delete/<int:trade_id>', methods=['POST'])
@login_required
def delete_trade(trade_id):
    trade = Trade.query.get_or_404(trade_id)
    if trade.user_id == current_user.id:
        trade.is_deleted = True
        trade.deleted_at = datetime.utcnow()
        db.session.commit()
        if request.headers.get('Content-Type') == 'application/json' or request.args.get('ajax'):
            return jsonify({'success': True})
        flash('Trade moved to trash.', 'success')
    return redirect(url_for('journal'))


@app.route('/trade/permanent_delete/<int:trade_id>', methods=['POST'])
@login_required
def permanent_delete_trade(trade_id):
    trade = Trade.query.get_or_404(trade_id)
    if trade.user_id == current_user.id and trade.is_deleted:
        db.session.delete(trade)
        db.session.commit()
        if request.headers.get('Content-Type') == 'application/json' or request.args.get('ajax'):
            return jsonify({'success': True})
        flash('Trade permanently deleted.', 'success')
    return redirect(url_for('journal', view='trash'))


@app.route('/trade/restore/<int:trade_id>', methods=['POST'])
@login_required
def restore_trade(trade_id):
    trade = Trade.query.get_or_404(trade_id)
    if trade.user_id == current_user.id:
        trade.is_deleted = False
        db.session.commit()
        if request.headers.get('Content-Type') == 'application/json' or request.args.get('ajax'):
            return jsonify({'success': True})
    return redirect(url_for('journal', view='trash'))



@app.route('/tasks', methods=['GET', 'POST'])
@login_required
def tasks():
    if request.method == 'POST':
        content = request.form.get('content')
        if content:
            task = Task(content=content, user_id=current_user.id)
            db.session.add(task)
            db.session.commit()
        return redirect(url_for('tasks'))

    tasks = Task.query.filter_by(user_id=current_user.id).order_by(Task.created_at.desc()).all()
    return render_template('tasks.html', tasks=tasks)


@app.route('/tasks/complete/<int:task_id>')
@login_required
def complete_task(task_id):
    task = Task.query.get_or_404(task_id)
    if task.user_id == current_user.id:
        task.is_completed = not task.is_completed
        db.session.commit()
    return redirect(url_for('tasks'))


@app.route('/tasks/delete/<int:task_id>')
@login_required
def delete_task(task_id):
    task = Task.query.get_or_404(task_id)
    if task.user_id == current_user.id:
        db.session.delete(task)
        db.session.commit()
    return redirect(url_for('tasks'))


@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    users = User.query.all()
    # Also fetch some global stats if needed
    total_trades = Trade.query.count()
    return render_template("admin.html", users=users, total_trades=total_trades)

@app.route('/import', methods=['GET', 'POST'])
@login_required
def import_trades():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file selected')
            return redirect(request.url)

        file = request.files['file']
        if file.filename == '' or not file.filename.endswith('.csv'):
            flash('Invalid CSV file')
            return redirect(request.url)

        import csv, io
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        reader = csv.DictReader(stream)

        count = 0
        for row in reader:
            try:
                symbol = row['Symbol'].upper()
                direction = row['Direction']
                quantity = float(row['Quantity'])
                entry = float(row['Entry'])
                exit_price = float(row['Exit'])

                direction_mod = 1 if direction == 'Long' else -1
                pnl = (exit_price - entry) * quantity * direction_mod
                result = 'Win' if pnl > 0 else ('Loss' if pnl < 0 else 'BE')

                trade = Trade(
                    user_id=current_user.id,
                    symbol=symbol,
                    direction=direction,
                    quantity=quantity,
                    entry_price=entry,
                    exit_price=exit_price,
                    pnl=pnl,
                    result=result,
                    notes='Imported CSV'
                )
                db.session.add(trade)
                count += 1
            except:
                continue

        db.session.commit()
        flash(f'Imported {count} trades')
        return redirect(url_for('journal'))

    return render_template('import.html')


@app.route('/settings')
@login_required
def settings():
    goals_row = db.session.execute(text("SELECT profit_target, max_daily_loss FROM risk_settings LIMIT 1")).fetchone()
    profit_target = goals_row[0] if goals_row else 800.0
    max_daily_loss = goals_row[1] if goals_row else 500.0
    
    current_goals = {
        'profit_target': profit_target,
        'daily_loss_limit': max_daily_loss
    }

    # Fetch MT5 symbols for backtesting mode
    symbols = []
    if current_user.account_type == 'backtest':
        user_folder = get_user_mt5_base(current_user.id)
        if os.path.exists(user_folder):
            symbols = [d for d in os.listdir(user_folder) if os.path.isdir(os.path.join(user_folder, d))]

    return render_template('settings.html', goals=current_goals, symbols=symbols)

@app.route('/api/backtest/delete-symbol', methods=['POST'])
@login_required
def delete_symbol_api():
    data = request.json
    symbol = data.get('symbol')
    if not symbol:
        abort(400, description="Symbol is required")
        
    symbol_folder = get_symbol_folder(current_user.id, symbol)
    if os.path.exists(symbol_folder):
        import shutil
        shutil.rmtree(symbol_folder)
        
        # FIX: If deleted symbol was active, clear it from session
        if session.get('active_symbol') == symbol:
            session.pop('active_symbol', None)
            
        return jsonify({"status": "success", "message": f"Deleted {symbol}"})
    
    return jsonify({"status": "error", "message": "Symbol not found"}), 404

@app.route('/api/backtest/clear-all', methods=['POST'])
@login_required
def clear_all_data_api():
    # 1. Set Abort Flag to stop current background processing
    ABORT_PROCESSING[current_user.id] = True
    
    # 2. Clear Session State
    session.pop('active_symbol', None)
    session.pop(f'backtest_cursor_{current_user.id}', None)
    session.pop(f'active_symbol_{current_user.id}', None)
    
    # 3. Delete Physical Data
    user_folder = get_user_mt5_base(current_user.id)
    if os.path.exists(user_folder):
        import shutil
        try:
            shutil.rmtree(user_folder)
            os.makedirs(user_folder)
            return jsonify({"status": "success", "message": "All data wiped successfully"})
        except Exception as e:
             return jsonify({"status": "error", "message": f"Wipe failed: {str(e)}"}), 500
             
    return jsonify({"status": "success", "message": "No data to clear"})


@app.route('/export_csv')
@login_required
def export_csv():
    import csv, io
    from flask import Response

    trades = Trade.query.filter_by(user_id=current_user.id, is_deleted=False).order_by(Trade.date.desc()).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow(['Date', 'Symbol', 'Direction', 'Quantity', 'Entry', 'Exit', 'PnL', 'Result', 'Strategy', 'Tags', 'Discipline', 'Screenshot'])
    
    for t in trades:
        writer.writerow([
            t.date.strftime('%Y-%m-%d %H:%M'),
            t.symbol,
            t.direction,
            t.quantity,
            t.entry_price,
            t.exit_price,
            t.pnl,
            t.result,
            t.strategy or '',
            t.tags or '',
            t.discipline or '',
            t.screenshot or ''
        ])
    
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=trade_journal_export.csv"}
    )


@app.route('/settings/export-full')
@login_required
def export_full_backup():
    all_trades = Trade.query.filter_by(user_id=current_user.id).all()
    csv_output = io.StringIO()
    writer = csv.writer(csv_output)
    writer.writerow(['Date', 'Symbol', 'Direction', 'Quantity', 'Entry', 'Exit', 'PnL', 'Result', 'Strategy', 'Tags', 'Discipline', 'Screenshot'])
    
    referenced_images = set()
    for t in all_trades:
        writer.writerow([
            t.date.strftime('%Y-%m-%d %H:%M') if t.date else '',
            t.symbol, t.direction, t.quantity, t.entry_price, t.exit_price,
            t.pnl, t.result, t.strategy or '', t.tags or '', t.discipline or '', t.screenshot or ''
        ])
        if t.screenshot:
            try:
                if t.screenshot.startswith('['):
                    imgs = json.loads(t.screenshot)
                    if isinstance(imgs, list):
                        for img in imgs: referenced_images.add(img)
                    else: referenced_images.add(t.screenshot)
                else: referenced_images.add(t.screenshot)
            except: referenced_images.add(t.screenshot)

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('trade_history.csv', csv_output.getvalue())
        upload_folder = app.config.get('UPLOAD_FOLDER', 'uploads')
        for img_name in referenced_images:
            if not img_name: continue
            img_path = os.path.join(upload_folder, img_name)
            if os.path.exists(img_path):
                zf.write(img_path, arcname=f'photos/{img_name}')

    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"TraderPro_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    )


@app.route('/settings/import-csv', methods=['POST'])
@login_required
def import_csv():
    files = request.files.getlist('csv_files')
    if not files:
        flash('No files selected', 'error')
        return redirect(url_for('settings'))
    
    imported_count = 0
    error_count = 0
    images_saved = 0
    csv_files = []
    
    for file in files:
        if file.filename == '': continue
        filename = secure_filename(file.filename)
        if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
            file_path = os.path.join(app.config.get('UPLOAD_FOLDER', 'uploads'), filename)
            file.save(file_path)
            images_saved += 1
        elif filename.lower().endswith('.csv'):
            csv_files.append(file)

    if not csv_files and images_saved > 0:
        flash(f'Saved {images_saved} images. No CSV file found.', 'info')
        return redirect(url_for('settings'))
    
    if not csv_files:
        flash('No CSV file found', 'error')
        return redirect(url_for('settings'))

    skipped_count = 0
    
    for file in csv_files:
        try:
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            reader = csv.DictReader(stream)
            for row in reader:
                try:
                    trade_date = datetime.strptime(row['Date'], '%Y-%m-%d %H:%M') if 'Date' in row and row['Date'] else datetime.utcnow()
                    
                    # Duplicate Check
                    symbol = row.get('Symbol', 'UNKNOWN').upper()
                    direction = row.get('Direction', 'Long')
                    pnl = float(row.get('PnL', 0.0))
                    
                    existing = Trade.query.filter_by(
                        user_id=current_user.id,
                        date=trade_date,
                        symbol=symbol,
                        direction=direction,
                        pnl=pnl
                    ).first()
                    
                    if existing:
                        skipped_count += 1
                        continue # Skip duplicates
                    
                    new_trade = Trade(
                        user_id=current_user.id,
                        symbol=symbol,
                        direction=direction,
                        quantity=float(row.get('Quantity', 1.0)),
                        entry_price=float(row.get('Entry', 0.0)),
                        exit_price=float(row.get('Exit', 0.0)),
                        pnl=pnl,
                        result=row.get('Result', ''),
                        strategy=row.get('Strategy', ''),
                        tags=row.get('Tags', ''),
                        discipline=int(row.get('Discipline', 5)) if row.get('Discipline') else 5,
                        screenshot=row.get('Screenshot', ''),
                        date=trade_date
                    )
                    db.session.add(new_trade)
                    imported_count += 1
                except Exception as e:
                    print(f"Error importing row: {e}")
                    error_count += 1
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f"Error reading {file.filename}: {e}", "error")

    msg = f"Imported {imported_count} trades."
    if skipped_count > 0: msg += f" Skipped {skipped_count} duplicates."
    if images_saved > 0: msg += f" Saved {images_saved} images."
    flash(msg, 'warning' if error_count > 0 or skipped_count > 0 else 'success')
    return redirect(url_for('settings'))


@app.route('/analytics')
@login_required
def analytics():
    # 🔥 Access Check: Journal user only
    if current_user.account_type != 'journal':
        return redirect(url_for('chart_page'))
    is_sqlite = 'sqlite' in db.engine.dialect.name
    
    # 1. Daily Breakdown (Table)
    if is_sqlite:
        daily_sql = "SELECT DATE(date) AS day, COUNT(*) AS trades, SUM(pnl) AS pnl FROM trades WHERE user_id = :uid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY day ORDER BY day DESC"
    else:
        daily_sql = "SELECT date(date) AS day, COUNT(*) AS trades, SUM(pnl) AS pnl FROM trades WHERE user_id = :uid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY day ORDER BY day DESC"
    
    daily = db.session.execute(text(daily_sql), {"uid": current_user.id}).fetchall()

    # 2. Weekly Breakdown (Table)
    if is_sqlite:
        weekly_sql = "SELECT strftime('%Y-W%W', date) AS week, COUNT(*) AS trades, SUM(pnl) AS pnl FROM trades WHERE user_id = :uid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY week ORDER BY week DESC"
    else:
        weekly_sql = "SELECT TO_CHAR(date, 'IYYY-IW') AS week, COUNT(*) AS trades, SUM(pnl) AS pnl FROM trades WHERE user_id = :uid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY week ORDER BY week DESC"
        
    weekly = db.session.execute(text(weekly_sql), {"uid": current_user.id}).fetchall()

    # 3. Monthly Breakdown (Table)
    if is_sqlite:
        monthly_sql = "SELECT strftime('%Y-%m', date) AS month, COUNT(*) AS trades, SUM(pnl) AS pnl FROM trades WHERE user_id = :uid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY month ORDER BY month DESC"
    else:
        monthly_sql = "SELECT TO_CHAR(date, 'YYYY-MM') AS month, COUNT(*) AS trades, SUM(pnl) AS pnl FROM trades WHERE user_id = :uid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY month ORDER BY month DESC"
        
    monthly = db.session.execute(text(monthly_sql), {"uid": current_user.id}).fetchall()
    
    # 4. Equity Curve (Trade-by-Trade) & Drawdown
    # Fetch all trades ordered by date to build granular curve
    all_trades = Trade.query.filter_by(user_id=current_user.id, is_deleted=False).order_by(Trade.date.asc()).all()
    
    weekly_equity = [] # Keeping variable name for compatibility, but now it's per-trade
    drawdown_data = []
    
    current_equity = 0
    max_equity = 0
    
    # Add initial point (optional, starting at 0)
    # weekly_equity.append({"time": "Start", "value": 0, "label": "Start"})

    for t in all_trades:
        current_equity += t.pnl
        
        # Track Max Equity for Drawdown
        max_equity = max(max_equity, current_equity)
        current_drawdown = current_equity - max_equity
        
        trade_time = t.date.strftime('%Y-%m-%d %H:%M') if t.date else "N/A"
        
        weekly_equity.append({
            "time": trade_time,
            "value": round(current_equity, 2),
            "label": t.symbol # Useful context
        })
        
        drawdown_data.append({
            "time": trade_time,
            "value": round(current_drawdown, 2)
        })
    
    # For weekly list table (keep existing logic if needed, or remove if unused)
    # We will keep the query for the table below the chart if it exists
    if is_sqlite:
        equity_sql = "SELECT date(date, '-6 days', 'weekday 1') as week_start, strftime('%Y-W%W', date) as week_label, SUM(pnl) as pnl FROM trades WHERE user_id = :uid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY week_start, week_label ORDER BY week_start ASC"
    else:
        equity_sql = "SELECT DATE_TRUNC('week', date)::date as week_start, TO_CHAR(date, 'IYYY-IW') as week_label, SUM(pnl) as pnl FROM trades WHERE user_id = :uid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY week_start, week_label ORDER BY week_start ASC"
    
    weekly_equity_rows = db.session.execute(text(equity_sql), {"uid": current_user.id}).fetchall()

    # 5. Weekly List for Dropdown
    weekly_list = [row.week_label for row in weekly_equity_rows]

    # 6. Global Analytics Stats
    gross_profit = sum(t.pnl for t in all_trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in all_trades if t.pnl < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0

    # 7. Advanced Stats (Short vs Long)
    short_trades = [t for t in all_trades if t.direction == 'Short']
    long_trades = [t for t in all_trades if t.direction == 'Long']
    
    def calc_stats(trade_list):
        count = len(trade_list)
        if count == 0:
            return {"pnl": 0, "wins": 0, "losses": 0, "win_rate": 0, "pnl_wins": 0, "pnl_losses": 0}
            
        pnl = sum(t.pnl for t in trade_list)
        
        wins_trades = [t for t in trade_list if t.pnl > 0]
        losses_trades = [t for t in trade_list if t.pnl <= 0]
        
        wins = len(wins_trades)
        losses = count - wins
        
        win_rate = round((wins / count) * 100, 1)
        
        pnl_wins = sum(t.pnl for t in wins_trades)
        pnl_losses = sum(t.pnl for t in losses_trades)
        
        return {
            "pnl": pnl, 
            "wins": wins, 
            "losses": losses, 
            "win_rate": win_rate,
            "pnl_wins": pnl_wins,
            "pnl_losses": pnl_losses
        }

    # 7. Short/Long Analysis -> Now Last 7 Days / Overall
    now = datetime.now()
    cutoff_date = now - timedelta(days=7)

    # Re-purposing variables to avoid breaking template contracts immediately
    # short_trades = LAST 7 DAYS
    # long_trades = OVERALL (ALL TRADES)
    
    short_trades = [t for t in all_trades if t.date and t.date >= cutoff_date]
    long_trades = all_trades # Overall
    
    short_stats = calc_stats(short_trades)
    long_stats = calc_stats(long_trades)

    # 8. Profitability (All Trades)
    profitability_stats = calc_stats(all_trades)
    profitability_stats['total'] = len(all_trades)

    # 9. Duration Analysis
    duration_data = []
    for t in all_trades:
        if t.duration and t.duration > 0:
            duration_data.append({"x": round(t.duration / 60, 2), "y": t.pnl, "symbol": t.symbol})

    # 10. Duration Distribution (Buckets)
    dist_buckets = {
        "< 5m": {"pnl": 0, "count": 0, "wins": 0, "pnl_wins": 0, "pnl_losses": 0},
        "5-15m": {"pnl": 0, "count": 0, "wins": 0, "pnl_wins": 0, "pnl_losses": 0},
        "15-30m": {"pnl": 0, "count": 0, "wins": 0, "pnl_wins": 0, "pnl_losses": 0},
        "30m-1h": {"pnl": 0, "count": 0, "wins": 0, "pnl_wins": 0, "pnl_losses": 0},
        "1h-4h": {"pnl": 0, "count": 0, "wins": 0, "pnl_wins": 0, "pnl_losses": 0},
        "> 4h": {"pnl": 0, "count": 0, "wins": 0, "pnl_wins": 0, "pnl_losses": 0}
    }
    
    for t in all_trades:
        if not t.duration: continue
        d_min = t.duration / 60 
        
        bucket = "> 4h"
        if d_min < 5: bucket = "< 5m"
        elif d_min < 15: bucket = "5-15m"
        elif d_min < 30: bucket = "15-30m"
        elif d_min < 60: bucket = "30m-1h"
        elif d_min < 240: bucket = "1h-4h"
        
        dist_buckets[bucket]["pnl"] += t.pnl
        dist_buckets[bucket]["count"] += 1
        if t.pnl > 0: 
            dist_buckets[bucket]["wins"] += 1
            dist_buckets[bucket]["pnl_wins"] += t.pnl
        else:
            dist_buckets[bucket]["pnl_losses"] += t.pnl

    distribution_data = {
        "labels": list(dist_buckets.keys()),
        "pnl": [round(dist_buckets[k]["pnl"], 2) for k in dist_buckets],
        "pnl_wins": [round(dist_buckets[k]["pnl_wins"], 2) for k in dist_buckets],
        "pnl_losses": [round(dist_buckets[k]["pnl_losses"], 2) for k in dist_buckets],
        "count": [dist_buckets[k]["count"] for k in dist_buckets]
    }
    
    # 11. Instrument Profit Analysis
    instrument_map = {}
    for t in all_trades:
        sym = t.symbol.upper().strip()
        if sym not in instrument_map:
            instrument_map[sym] = 0.0
        instrument_map[sym] += t.pnl
    
    # Sort by PnL Descending (or we could do alphabetical) -> Choosing PnL Descending to show best performers first
    sorted_instruments = sorted(instrument_map.items(), key=lambda x: x[1], reverse=True)
    
    instrument_data = {
        "labels": [x[0] for x in sorted_instruments],
        "pnl": [round(x[1], 2) for x in sorted_instruments]
    }

    # 12. Session Win Rates
    sessions = {
        "Asian": {"wins": 0, "total": 0, "win_rate": 0},
        "London": {"wins": 0, "total": 0, "win_rate": 0},
        "New York": {"wins": 0, "total": 0, "win_rate": 0}
    }
    
    for t in all_trades:
        if t.session and t.session in sessions:
            sessions[t.session]["total"] += 1
            if t.pnl > 0:
                sessions[t.session]["wins"] += 1
    
    # Calculate percentages
    for s in sessions:
        if sessions[s]["total"] > 0:
            sessions[s]["win_rate"] = round((sessions[s]["wins"] / sessions[s]["total"]) * 100, 1)
        else:
            sessions[s]["win_rate"] = 0

    return render_template('analytics.html', 
                         view='analytics',
                         week_day=is_sqlite,
                         daily=daily,
                         weekly=weekly,
                         monthly=monthly,
                         weekly_equity=weekly_equity,
                         drawdown_data=drawdown_data,
                         weekly_equity_rows=weekly_list,
                         gross_profit=gross_profit,
                         gross_loss=gross_loss,
                         profit_factor=profit_factor,
                         short_stats=short_stats,
                         long_stats=long_stats,
                         profitability_stats=profitability_stats,
                         duration_data=duration_data,
                         distribution_data=distribution_data,
                         instrument_data=instrument_data,
                         session_stats=sessions)


def run_migrations():
    # Run partial schema migrations (add missing columns)
    with app.app_context():
        try:
            with db.engine.begin() as conn:  # engine.begin() auto-commits
                # Helper to add column if not exists
                def add_column(table, column_def):
                    name = column_def.split()[0]
                    try:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_def}"))
                        print(f"Migration: Added {table}.{name}")
                    except Exception as e:
                        # SQLite raises error if column exists
                        if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                            pass 
                        else:
                            print(f"Migration: {table}.{name} already exists or error: {e}")
                
                # Trade columns
                columns = [
                    ("stop_loss", "FLOAT"), ("take_profit", "FLOAT"), ("screenshot", "VARCHAR(255)"),
                    ("strategy", "VARCHAR(50)"), ("session", "VARCHAR(20)"), ("emotion", "VARCHAR(50)"),
                    ("is_deleted", "BOOLEAN DEFAULT 0"), ("tags", "TEXT"), ("discipline", "INTEGER"),
                    ("timeframe", "VARCHAR(20)"), ("deleted_at", "DATETIME"),
                    ("timeframe", "VARCHAR(20)"), ("deleted_at", "DATETIME"),
                    ("rr", "FLOAT"), ("duration", "INTEGER") # Ensure Duration is here
                ]
                for col, dtype in columns:
                    add_column("trades", f"{col} {dtype}")

                # User columns
                user_cols = [
                    ("account_name", "VARCHAR(100) DEFAULT 'My Trading Account'"),
                    ("initial_balance", "FLOAT DEFAULT 0.0"),
                    ("name", "VARCHAR(100)"),
                    ("email", "VARCHAR(120)"),
                    ("role", "VARCHAR(20) DEFAULT 'user'"),
                    ("account_type", "VARCHAR(20) DEFAULT 'journal'")
                ]
                for col, dtype in user_cols:
                    add_column("users", f"{col} {dtype}")

                # Risk Settings Table
                conn.execute(text('''
                    CREATE TABLE IF NOT EXISTS risk_settings (
                        id INTEGER PRIMARY KEY,
                        profit_target REAL DEFAULT 800.0,
                        max_daily_loss REAL DEFAULT 500.0
                    )
                '''))
                
                # Seed default if empty
                # Use scalar logic safe for Postgres/SQLite
                try:
                    count = conn.execute(text("SELECT count(*) FROM risk_settings")).scalar()
                    if count == 0:
                        conn.execute(text("INSERT INTO risk_settings (profit_target, max_daily_loss) VALUES (800.0, 500.0)"))
                        print("Migration: Seeded default risk settings")
                except Exception as e:
                    print(f"Risk Settings Init Error: {e}")

        except Exception as e:
            print(f"Migration failed (non-critical if DB is new): {e}")

def create_admin():
    with app.app_context():
        # Check for admin user
        # Note: run_migrations() must run BEFORE this if the table exists but is missing columns
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', name='Administrator', email='admin@tradejournal.com', role='admin')
            admin.set_password('password')
            db.session.add(admin)
            db.session.commit()
            print("Created default admin user (admin/password)")

# ---------------------------------------------------------
# 🔥 CRITICAL: Run DB Init ON STARTUP (For Render)
# ---------------------------------------------------------
with app.app_context():
    try:
        db.create_all()  # Ensure tables exist (Render Postgres needs this!)
        run_migrations() # Add any columns if updating existing DB
        create_admin()   # Ensure admin user
        print("✅ Database initialized successfully.")
    except Exception as e:
        print(f"❌ Database initialization failed: {e}")
# ---------------------------------------------------------

if __name__ == '__main__':
    app.run(debug=True)
