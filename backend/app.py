from flask import Flask, render_template, redirect, url_for, flash, request, jsonify, send_from_directory, abort, session
from sqlalchemy import text, func, inspect
from functools import wraps
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer
import json
import calendar
import pytz
import os
from datetime import datetime, timedelta, date
from werkzeug.utils import secure_filename

from models import db, bcrypt, User, Task, Trade, RiskSettings
from config import Config
import hashlib
import threading

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

# TradingView-style timeframe lockdown
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
    Professional Timeframe Aggregation Engine.
    Groups M1 candles into natural UTC time boundaries (e.g. :00, :05).
    Strictly follows OHLC rules and ignores gaps without inventing data.
    """
    if not m1_candles:
        return []
    
    # 1. Ensure input is sorted chronologically
    sorted_m1 = sorted(m1_candles, key=lambda x: x['time'])
    
    aggregated = []
    interval_seconds = timeframe_minutes * 60
    
    # 2. Group into buckets based on natural time boundaries
    # Using a dictionary to handle potential edge cases gracefully
    buckets = {}
    
    for candle in sorted_m1:
        ts = candle['time']
        # Floor to the start of the timeframe interval
        bucket_time = (ts // interval_seconds) * interval_seconds
        
        if bucket_time not in buckets:
            buckets[bucket_time] = []
        buckets[bucket_time].append(candle)
        
    # 3. Process each bucket into a single OHLC candle
    # Sorting bucket times to ensure output is chronological
    for bucket_ts in sorted(buckets.keys()):
        period_candles = buckets[bucket_ts]
        
        try:
            open_p = period_candles[0]['open']
            close_p = period_candles[-1]['close']
            high_p = max(c['high'] for c in period_candles)
            low_p = min(c['low'] for c in period_candles)
            
            # 4. Final Validation: Ensure OHLC integrity
            if low_p > high_p:
                continue # Skip invalid data if somehow high/low are swapped
                
            aggregated.append({
                "time": bucket_ts,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p
            })
        except (IndexError, KeyError, ValueError):
            continue
            
    return aggregated

def background_process_csv(file_path, symbol_folder, symbol):
    """Background task to process CSV into hierarchical symbol structure."""
    status_path = os.path.join(symbol_folder, 'status.json')
    meta_path = os.path.join(symbol_folder, 'meta.json')
    processed_base = os.path.join(symbol_folder, 'processed')
    
    try:
        with open(status_path, 'w') as f:
            json.dump({"state": "PROCESSING", "progress": 0}, f)
            
        parsed_data = parse_mt5_csv(file_path, symbol=symbol)
        
        if parsed_data:
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
                json.dump({"state": "READY", "timestamp": datetime.utcnow().isoformat()}, f)
            print(f"✅ Hierarchical processing complete for: {symbol}")
        else:
            with open(status_path, 'w') as f:
                json.dump({"state": "ERROR", "message": "Parsing failed"}, f)
    except Exception as e:
        print(f"❌ Background processing error: {e}")
        with open(status_path, 'w') as f:
            json.dump({"state": "ERROR", "message": str(e)}, f)

def parse_mt5_csv(file_path, symbol="Unknown"):
    """
    Parses MT5 CSV and returns base M1 candles + synthesized versions.
    """
    m1_candles = []
    try:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except UnicodeDecodeError:
            with open(file_path, 'r', encoding='utf-16') as f:
                lines = f.readlines()
        
        if not lines: return None
        
        start_row = 0
        if any(k in lines[0].upper() for k in ['DATE', 'OPEN', '<TICKER>']):
            start_row = 1
            
        for line in lines[start_row:]:
            columns = line.strip().split()
            if len(columns) < 6: continue
                
            try:
                date_str, time_str = columns[0], columns[1]
                o, h, l, c = map(float, columns[2:6])
                # Check for volume in 7th column if available
                v = float(columns[6]) if len(columns) > 6 else 0
                
                clean_dt = f"{date_str} {time_str}".replace('.', '-')
                try:
                    dt = datetime.strptime(clean_dt, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    dt = datetime.strptime(clean_dt, "%Y-%m-%d %H:%M")
                
                m1_candles.append({
                    "time": int(dt.timestamp()),
                    "open": round(o, 5),
                    "high": round(h, 5),
                    "low": round(l, 5),
                    "close": round(c, 5),
                    "volume": int(v)
                })
            except: continue
        
        if not m1_candles: return None
        
        m1_candles.sort(key=lambda x: x['time'])

        # Data Verification: Ensure Source is M1
        if len(m1_candles) > 50:
            diffs = []
            for i in range(1, min(200, len(m1_candles))):
                d = m1_candles[i]['time'] - m1_candles[i-1]['time']
                if d > 0: diffs.append(d)
            
            if diffs:
                median_diff = sorted(diffs)[len(diffs)//2]
                # Allow minor data gaps, but if median is > 180s (3m), it's definitely not M1
                if median_diff > 180:
                    raise ValueError(f"CRITICAL: Uploaded data appears to be {median_diff}s candles. ONLY M1 (60s) data is allowed.")

        # Synthesize from M1
        aggregated_tfs = {
            "M1": aggregate_candles(m1_candles, 1),
            "M3": aggregate_candles(m1_candles, 3),
            "M5": aggregate_candles(m1_candles, 5),
            "M15": aggregate_candles(m1_candles, 15),
            "M30": aggregate_candles(m1_candles, 30),
            "H1": aggregate_candles(m1_candles, 60),
            "H2": aggregate_candles(m1_candles, 120),
            "H4": aggregate_candles(m1_candles, 240),
            "D1": aggregate_candles(m1_candles, 1440),
            "W1": aggregate_candles(m1_candles, 10080)
        }

        # Build Rich Metadata
        candle_counts = {tf: len(data) for tf, data in aggregated_tfs.items()}
        derived_list = [tf for tf in aggregated_tfs.keys() if tf != "M1"]

        result = {
            "meta": {
                "symbol": symbol,
                "source": "MT5",
                "source_tf": "M1",
                "original_timeframe": "M1",
                "csv_hash": hash_file(file_path),
                "created_at": datetime.utcnow().isoformat() + "Z",
                "available_timeframes": list(aggregated_tfs.keys()),
                "derived_timeframes": derived_list,
                "candle_counts": candle_counts,
                "data_quality": {
                    "source_count": len(m1_candles),
                    "verified_m1": True,
                    "median_gap": median_diff if len(m1_candles) > 50 and diffs else 60
                }
            },
            "timeframes": aggregated_tfs
        }
        return result
    except Exception as e:
        print(f"Error in parse_mt5_csv: {e}")
        return None

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
    
    # If no active symbol, pick the latest one from folders
    if not active_symbol:
        symbols = [d for d in os.listdir(user_base) if os.path.isdir(os.path.join(user_base, d))]
        if symbols:
            # Sort by most recently modified folder
            symbols.sort(key=lambda d: os.path.getmtime(os.path.join(user_base, d)), reverse=True)
            active_symbol = symbols[0]
            session['active_symbol'] = active_symbol
    
    status = "IDLE"
    meta = None
    
    if active_symbol:
        symbol_folder = get_symbol_folder(current_user.id, active_symbol)
        status_path = os.path.join(symbol_folder, 'status.json')
        meta_path = os.path.join(symbol_folder, 'meta.json')
        
        if os.path.exists(status_path):
            with open(status_path, 'r') as f:
                status_data = json.load(f)
                status = status_data.get('status', 'IDLE')
        
        if os.path.exists(meta_path):
            with open(meta_path, 'r') as f:
                meta = json.load(f)
            
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
        
        symbol_folder = get_symbol_folder(current_user.id, symbol)
        raw_folder = os.path.join(symbol_folder, 'raw')
        os.makedirs(raw_folder, exist_ok=True)
        
        file_path = os.path.join(raw_folder, 'original.csv')
        file.save(file_path)
        
        # Calculate hash for duplicate detection
        file_hash = hash_file(file_path)
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
            thread = threading.Thread(target=background_process_csv, args=(file_path, symbol_folder, symbol))
            thread.start()
        
        session['active_symbol'] = symbol
        flash(f'MT5 CSV for {symbol} uploaded! Processing in background...' if should_process else f'MT5 CSV for {symbol} uploaded (Used Cache)!')
        return redirect(url_for('chart_page'))
    
    flash('Invalid file type. Please upload a CSV.')
    return redirect(url_for('chart_page'))


@app.route('/api/symbol-meta/<symbol>')
@login_required
def symbol_meta_api(symbol):
    """Returns available timeframes for a symbol based on processed data."""
    symbol_folder = get_symbol_folder(current_user.id, symbol)
    meta_path = os.path.join(symbol_folder, 'meta.json')
    
    # Priority: Read rich metadata from meta.json
    if os.path.exists(meta_path):
        try:
            with open(meta_path, 'r') as f:
                rich_meta = json.load(f)
            # Ensure available_timeframes is populated
            if 'available_timeframes' not in rich_meta:
                 processed_base = os.path.join(symbol_folder, 'processed')
                 if os.path.exists(processed_base):
                     rich_meta['available_timeframes'] = [d for d in os.listdir(processed_base) 
                                if os.path.isdir(os.path.join(processed_base, d)) and d in ALLOWED_TIMEFRAMES]
            return jsonify(rich_meta)
        except Exception as e:
            print(f"Error reading meta.json for {symbol}: {e}")

    # Fallback: Directory Scan
    processed_base = os.path.join(symbol_folder, 'processed')
    available_timeframes = []
    
    if os.path.exists(processed_base):
        # Scan processed folder for valid TF directories
        available_timeframes = [d for d in os.listdir(processed_base) 
                              if os.path.isdir(os.path.join(processed_base, d)) and d in ALLOWED_TIMEFRAMES]
    
    # Sort TFs logically (M1 -> W1)
    tf_order = {tf: i for i, tf in enumerate(ALLOWED_TIMEFRAMES)}
    available_timeframes.sort(key=lambda x: tf_order.get(x, 999))
    
    return jsonify({
        "symbol": symbol,
        "base_tf": "M1", # Assumed base
        "available_timeframes": available_timeframes
    })


@app.route('/api/mt5-data/<symbol>/<timeframe>')
@login_required
def get_mt5_timeframe_data(symbol, timeframe):
    """Serves chart-ready JSON or reveals history up to a moving time boundary."""
    tf_upper = timeframe.upper()
    if tf_upper not in ALLOWED_TIMEFRAMES:
        abort(400, description="Invalid timeframe")
        
    symbol_folder = get_symbol_folder(current_user.id, symbol)
    zone_file = os.path.join(symbol_folder, 'processed', tf_upper, 'zone.json')
    
    # Fallback to candles.json for legacy if zone.json not yet generated
    if not os.path.exists(zone_file):
        zone_file = os.path.join(symbol_folder, 'processed', tf_upper, 'candles.json')
    
    if not os.path.exists(zone_file):
        abort(404, description="Data not found")
        
    with open(zone_file, 'r') as f:
        data = json.load(f)
        
    # Boundary logic: /api/mt5-data/NAS100/M5?to=1705900800
    to_ts = request.args.get('to', type=int)
    
    # If it was legacy candles.json (pure list), wrap it
    if isinstance(data, list):
        data = {"symbol": symbol, "timeframe": tf_upper, "candles": data}
        
    if to_ts:
        data['candles'] = [c for c in data['candles'] if c['time'] <= to_ts]
        
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
            # Ensure we return something with 'state' even if the file had 'status' (migration/compatibility)
            if 'status' in status_data and 'state' not in status_data:
                status_data['state'] = status_data.pop('status')
            return jsonify(status_data)
            
    return jsonify({"state": "IDLE"})


@app.route('/api/backtest/start', methods=['POST'])
@login_required
def start_backtest():
    """Initializes backtest cursor at a specific timestamp."""
    data = request.json
    symbol = data.get('symbol')
    start_time = data.get('start_time') # Unix TS
    tf = data.get('timeframe', 'M5')
    
    if not start_time:
        abort(400, description="start_time is required")
        
    session[f'backtest_cursor_{current_user.id}'] = start_time
    session[f'active_symbol_{current_user.id}'] = symbol
    
    return jsonify({
        "status": "success",
        "cursor": start_time,
        "symbol": symbol,
        "timeframe": tf
    })

@app.route('/api/backtest/step', methods=['POST'])
@login_required
def step_backtest():
    """Advances backtest cursor by active timeframe seconds."""
    data = request.json
    tf = data.get('timeframe', 'M5').upper()
    
    TF_MAP = {
        "M1": 60, "M3": 180, "M5": 300, "M15": 900, "M30": 1800,
        "H1": 3600, "H2": 7200, "H4": 14400, "D1": 86400, "W1": 604800
    }
    
    seconds = TF_MAP.get(tf, 300)
    current_cursor = session.get(f'backtest_cursor_{current_user.id}')
    
    if not current_cursor:
        abort(400, description="Backtest not started")
        
    new_cursor = current_cursor + seconds
    session[f'backtest_cursor_{current_user.id}'] = new_cursor
    
    return jsonify({
        "status": "success",
        "new_cursor": new_cursor
    })
@login_required
def get_mt5_symbols():
    """Returns a list of symbols available for the user."""
    user_base = get_user_mt5_base(current_user.id)
    symbols = [d for d in os.listdir(user_base) if os.path.isdir(os.path.join(user_base, d))]
    return jsonify({"symbols": sorted(symbols)})


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
    
    today_pnl_row = db.session.execute(text("""
        SELECT COALESCE(SUM(pnl), 0)
        FROM trades
        WHERE date >= :start AND date < :end AND (is_deleted = 0 OR is_deleted IS NULL)
    """), {"start": start_utc, "end": end_utc}).fetchone()
    
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
    
    calendar_data_rows = db.session.execute(text("""
        SELECT 
            date(date, '+2 hours') as broker_day,
            SUM(pnl) as pnl,
            COUNT(*) as trade_count
        FROM trades
        WHERE user_id = :uid 
          AND (is_deleted = 0 OR is_deleted IS NULL)
          AND strftime('%Y-%m', date, '+2 hours') = :month
        GROUP BY broker_day
    """), {"uid": current_user.id, "month": month_str}).fetchall()
    
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
    
    calendar_data_rows = db.session.execute(text("""
        SELECT 
            date(date, '+2 hours') as broker_day,
            SUM(pnl) as pnl,
            COUNT(*) as trade_count
        FROM trades
        WHERE user_id = :uid 
          AND (is_deleted = 0 OR is_deleted IS NULL)
          AND strftime('%Y-%m', date, '+2 hours') = :month
        GROUP BY broker_day
    """), {"uid": current_user.id, "month": month_str}).fetchall()
    
    calendar_map = {row.broker_day: {
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
    """API endpoint to toggle between Trading Journal and Backtesting Mode"""
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
        
        screenshot_json = None
        screenshots = []
        
        # 1. Handle Multiple File Uploads
        if 'screenshot' in request.files:
            files = request.files.getlist('screenshot')
            for file in files:
                if file and file.filename != '':
                    allowed_mimetypes = {'image/png', 'image/jpeg', 'image/gif', 'application/pdf', 'video/mp4'}
                    if file.mimetype in allowed_mimetypes:
                        filename = secure_filename(file.filename)
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
                        filename = timestamp + filename
                        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                        with open(file_path, 'wb') as f:
                            f.write(file.read())
                        screenshots.append('uploads/' + filename)
        
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

        direction_mod = 1 if direction == 'Long' else -1
        # Calculate Exit Price from PnL
        # PnL = (Exit - Entry) * Qty * Dir
        # Exit = Entry + (PnL / (Qty * Dir))
        exit_price = entry + (pnl / (quantity * direction_mod))
        
        result = 'Win' if pnl > 0 else ('Loss' if pnl < 0 else 'BE')

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
            rr=rr
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
        # Broker day filter: trades where date(date, '+2 hours') == date_filter
        query = query.filter(text("date(date, '+2 hours') = :d")).params(d=date_filter)
    
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
    """Deletes all processed data for a specific symbol."""
    data = request.json
    symbol = data.get('symbol')
    if not symbol:
        abort(400, description="Symbol is required")
        
    symbol_folder = get_symbol_folder(current_user.id, symbol)
    if os.path.exists(symbol_folder):
        import shutil
        shutil.rmtree(symbol_folder)
        return jsonify({"status": "success", "message": f"Deleted {symbol}"})
    
    return jsonify({"status": "error", "message": "Symbol not found"}), 404

@app.route('/api/backtest/clear-all', methods=['POST'])
@login_required
def clear_all_data_api():
    """Wipes all uploaded MT5 data for the current user."""
    user_folder = get_user_mt5_base(current_user.id)
    if os.path.exists(user_folder):
        import shutil
        shutil.rmtree(user_folder)
        os.makedirs(user_folder)
        return jsonify({"status": "success", "message": "All data cleared"})
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
    writer.writerow(['Date', 'Symbol', 'Direction', 'Quantity', 'Entry', 'Exit', 'PnL', 'Result', 'Strategy', 'Tags', 'Discipline'])
    
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
            t.discipline or ''
        ])
    
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=trade_journal_export.csv"}
    )


@app.route('/analytics')
@login_required
def analytics():
    # 🔥 Access Check: Journal user only
    if current_user.account_type != 'journal':
        return redirect(url_for('chart_page'))
    # 1. Daily Breakdown (Table)
    daily = db.session.execute(text("""
        SELECT DATE(date) AS day,
               COUNT(*) AS trades,
               SUM(pnl) AS pnl
        FROM trades
        WHERE user_id = :uid AND (is_deleted = 0 OR is_deleted IS NULL)
        GROUP BY DATE(date)
        ORDER BY day DESC
    """), {"uid": current_user.id}).fetchall()

    # 2. Weekly Breakdown (Table)
    weekly = db.session.execute(text("""
        SELECT strftime('%Y-W%W', date) AS week,
               COUNT(*) AS trades,
               SUM(pnl) AS pnl
        FROM trades
        WHERE user_id = :uid AND (is_deleted = 0 OR is_deleted IS NULL)
        GROUP BY week
        ORDER BY week DESC
    """), {"uid": current_user.id}).fetchall()

    # 3. Monthly Breakdown (Table)
    monthly = db.session.execute(text("""
        SELECT strftime('%Y-%m', date) AS month,
               COUNT(*) AS trades,
               SUM(pnl) AS pnl
        FROM trades
        WHERE user_id = :uid AND (is_deleted = 0 OR is_deleted IS NULL)
        GROUP BY month
        ORDER BY month DESC
    """), {"uid": current_user.id}).fetchall()
    
    # 4. Weekly Equity Curve (Cumulative for Area Chart)
    # Fetch all trades ordered by date to build cumulative curve
    all_trades = Trade.query.filter_by(user_id=current_user.id, is_deleted=False).order_by(Trade.date.asc()).all()
    
    weekly_equity_map = {}
    cumulative_pnl = 0
    
    # Group cumulative PnL by week
    for t in all_trades:
        if not t.date: continue
        # ISO Week format: YYYY-Www, but we need date string for chart?
        # User requested: strftime('%Y-W%W', date) as time
        # But charts need YYYY-MM-DD. Let's use Monday of the week for chart key.
        week_key = t.date.strftime('%Y-W%W')
        
        cumulative_pnl += t.pnl
        weekly_equity_map[week_key] = cumulative_pnl

    # Convert map to sorted list matching user's backend request structure
    
    weekly_equity_rows = db.session.execute(text("""
        SELECT 
            DATE(date, 'weekday 0', '-6 days') as week_start,
            strftime('%Y-W%W', date) as week_label,
            SUM(pnl) as pnl
        FROM trades
        WHERE user_id = :uid AND (is_deleted = 0 OR is_deleted IS NULL)
        GROUP BY week_label
        ORDER BY week_start ASC
    """), {"uid": current_user.id}).fetchall()
    
    weekly_equity = []
    current_equity = 0
    drawdown_data = []
    max_equity = 0
    
    for row in weekly_equity_rows:
        current_equity += row.pnl
        
        # Build Equity Data
        weekly_equity.append({
            "time": row.week_start, # YYYY-MM-DD
            "value": current_equity,
            "label": row.week_label # Keep track of week label for dropdown filtering
        })
        
        # Build Drawdown Data
        max_equity = max(max_equity, current_equity)
        dd = current_equity - max_equity
        drawdown_data.append({
            "time": row.week_start,
            "value": dd
        })

    # 5. Weekly List for Dropdown
    weekly_list = [row.week_label for row in weekly_equity_rows]

    # 6. Global Analytics Stats
    gross_profit = sum(t.pnl for t in all_trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in all_trades if t.pnl < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0

    return render_template('analytics.html', 
                         weekly=weekly, 
                         monthly=monthly,
                         weekly_equity=weekly_equity,
                         drawdown_data=drawdown_data,
                         weekly_list=weekly_list,
                         gross_profit=gross_profit,
                         gross_loss=gross_loss,
                         profit_factor=profit_factor)


def run_migrations():
    """Run partial schema migrations (add missing columns)"""
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
                    ("rr", "FLOAT") # Ensure RR is here too
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
