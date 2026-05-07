import sys
import os
# Ensure the current directory is in the path for Vercel
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

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
import secrets
import re
from datetime import datetime, timedelta, date
from werkzeug.utils import secure_filename
from uuid import uuid4

from models import db, bcrypt, User, Task, Trade, RiskSettings, AnalysisHistory, FundedAccount, PropFirm
from config import Config
import hashlib
import threading
import cloudinary
import cloudinary.uploader



# ---------------- APP SETUP ----------------
app = Flask(__name__, 
            static_folder='../frontend/src',
            instance_path=os.path.abspath('instance') if not os.environ.get('VERCEL') else '/tmp')
app.config.from_object(Config)


from jinja2 import ChoiceLoader, FileSystemLoader
template_paths = [
    os.path.join(app.root_path, '../frontend'),
    os.path.join(app.root_path, '../frontend/src/pages')
]
app.jinja_loader = ChoiceLoader([
    FileSystemLoader(p) for p in template_paths
])

# Ensure upload folder exists (wrapped for Vercel/Read-only environments)
try:
    upload_folder = app.config.get('UPLOAD_FOLDER', 'uploads')
    if not os.path.exists(upload_folder):
        os.makedirs(upload_folder)
except Exception as e:
    print(f"Warning: Could not create upload folder: {e}")


# Cloudinary Configuration
cloudinary.config(
    cloud_name=app.config.get("CLOUDINARY_CLOUD_NAME"),
    api_key=app.config.get("CLOUDINARY_API_KEY"),
    api_secret=app.config.get("CLOUDINARY_API_SECRET"),
    secure=True
)



# TradingView-style timeframe lockdown (Gold Standard: M1 Backbone Enabled)


db.init_app(app)
bcrypt.init_app(app)

# --- Database Initialization ---
print("INFO: Starting database initialization check...")
with app.app_context():
    try:
        # Check if we have a database URL
        db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        if 'sqlite' in db_uri:
            print(f"WARNING: Using SQLite in a serverless environment (Vercel). Path: {db_uri}")
        # Disable automatic creation on Vercel to avoid startup crashes
        if not os.environ.get("VERCEL"):
             db.create_all()
             print("INFO: db.create_all() completed.")

        
        # Skip slow inspections on Vercel to speed up cold starts
        # Safe Migration Helper
        def safe_add_column(table_name, column_name, column_type):
            try:
                inspector = inspect(db.engine)
                columns = [c['name'] for c in inspector.get_columns(table_name)]
                if column_name not in columns:
                    db.session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))
                    db.session.commit()
                    print(f"Migration Success: Added column {column_name} to {table_name}")
            except Exception as e:
                db.session.rollback()
                print(f"Warning: Migration Error (Adding {column_name} to {table_name}): {e}")

        # Run Migrations for missing columns
        safe_add_column("trades", "duration", "INTEGER")
        safe_add_column("trades", "rr", "FLOAT")
        safe_add_column("trades", "emotion", "VARCHAR(50)")
        safe_add_column("trades", "timeframe", "VARCHAR(20)")
        
        # Users migrations
        safe_add_column("users", "active_account_id", "INTEGER")
        
        # Prop Firms & Funded Accounts migrations
        safe_add_column("prop_firms", "is_deleted", "BOOLEAN DEFAULT FALSE")
        safe_add_column("prop_firms", "deleted_at", "DATETIME")
        safe_add_column("funded_accounts", "is_deleted", "BOOLEAN DEFAULT FALSE")
        safe_add_column("funded_accounts", "deleted_at", "DATETIME")
        safe_add_column("funded_accounts", "firm_id", "INTEGER")


        # Seed RiskSettings if empty (skip on Vercel - tables already initialized)
        if not os.environ.get("VERCEL"):
            try:
                if not RiskSettings.query.first():
                    default_settings = RiskSettings(profit_target=800.0, max_daily_loss=500.0)
                    db.session.add(default_settings)
                    db.session.commit()
            except Exception as seed_err:
                print(f"Warning: Could not seed database: {seed_err}")
            
    except Exception as e:
        print(f"CRITICAL ERROR: DB Init failed: {e}")
        # In serverless, we might want to continue even if DB init fails (e.g. if DB is already up but read-only)


login_manager = LoginManager(app)
login_manager.login_view = "index"

# Mail Config Safety
if app.config.get("MAIL_USERNAME"):
    mail = Mail(app)
else:
    # Dummy mail object or handle gracefully if mail not configured
    print("Warning: Mail not configured. Emails will not send.")
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
    if current_user.is_authenticated:
        # Ensure user has an active account set
        if not current_user.active_account_id:
            account = FundedAccount.query.filter_by(user_id=current_user.id).first()
            if account:
                current_user.active_account_id = account.id
                db.session.commit()
            else:
                # Create a default if somehow missing
                default_acc = FundedAccount(user_id=current_user.id, name="Main Account", initial_balance=current_user.initial_balance)
                db.session.add(default_acc)
                db.session.flush()
                current_user.active_account_id = default_acc.id
                db.session.commit()

@app.context_processor
def inject_active_account():
    if current_user.is_authenticated:
        active_acc = FundedAccount.query.get(current_user.active_account_id)
        # Fetch all non-deleted firms with their non-deleted accounts
        all_firms = PropFirm.query.filter_by(user_id=current_user.id, is_deleted=False).order_by(PropFirm.created_at.desc()).all()
        return dict(active_account=active_acc, all_firms=all_firms)
    return dict(active_account=None, all_firms=[])

@app.route('/set_active_account/<int:account_id>')
@login_required
def set_active_account(account_id):
    account = FundedAccount.query.filter_by(id=account_id, user_id=current_user.id).first_or_404()
    current_user.active_account_id = account.id
    db.session.commit()
    flash(f"Switched to account: {account.phase}", "success")
    return redirect(request.referrer or url_for('dashboard'))

@app.route('/prop_firm/add', methods=['POST'])
@login_required
def add_prop_firm():
    name = request.form.get('name')
    if name:
        new_firm = PropFirm(user_id=current_user.id, name=name)
        db.session.add(new_firm)
        db.session.commit()
        # Automatically create a "Phase 1" for it
        new_acc = FundedAccount(user_id=current_user.id, firm_id=new_firm.id, name=name, phase="Phase 1", initial_balance=0.0)
        db.session.add(new_acc)
        db.session.commit()
        # Set as active
        current_user.active_account_id = new_acc.id
        db.session.commit()
        flash(f"Prop Firm '{name}' created!", "success")
    return redirect(url_for('settings'))

@app.route('/prop_firm/edit/<int:firm_id>', methods=['POST'])
@login_required
def edit_prop_firm(firm_id):
    firm = PropFirm.query.filter_by(id=firm_id, user_id=current_user.id).first_or_404()
    name = request.form.get('name')
    if name:
        firm.name = name
        # Update all child accounts name too if desired, but maybe keep them separate?
        # Typically the "Name" of the phase is just a label, but let's sync for consistency
        for acc in firm.accounts:
            acc.name = name
        db.session.commit()
        flash("Firm name updated!", "success")
    return redirect(url_for('settings'))

@app.route('/prop_firm/delete/<int:firm_id>', methods=['POST'])
@login_required
def delete_prop_firm(firm_id):
    firm = PropFirm.query.filter_by(id=firm_id, user_id=current_user.id).first_or_404()
    
    # Check if this contains the active account
    active_in_firm = any(acc.id == current_user.active_account_id for acc in firm.accounts)
    
    firm.is_deleted = True
    firm.deleted_at = datetime.utcnow()
    # Also soft delete all child accounts
    for acc in firm.accounts:
        acc.is_deleted = True
        acc.deleted_at = datetime.utcnow()
    
    db.session.commit()
    
    if active_in_firm:
        other_acc = FundedAccount.query.filter_by(user_id=current_user.id, is_deleted=False).first()
        if other_acc:
            current_user.active_account_id = other_acc.id
        else:
            current_user.active_account_id = None
        db.session.commit()
        
    flash("Prop Firm moved to Trash.", "success")
    return redirect(url_for('settings'))

@app.route('/prop_firm/restore/<int:firm_id>', methods=['POST'])
@login_required
def restore_prop_firm(firm_id):
    firm = PropFirm.query.filter_by(id=firm_id, user_id=current_user.id).first_or_404()
    firm.is_deleted = False
    # Also restore all child accounts
    for acc in firm.accounts:
        acc.is_deleted = False
    db.session.commit()
    flash(f"Restored {firm.name}", "success")
    return redirect(url_for('trash'))

@app.route('/prop_firm/permanent_delete/<int:firm_id>', methods=['POST'])
@login_required
def permanent_delete_prop_firm(firm_id):
    firm = PropFirm.query.filter_by(id=firm_id, user_id=current_user.id, is_deleted=True).first_or_404()
    db.session.delete(firm)
    db.session.commit()
    flash("Prop Firm permanently deleted.", "success")
    return redirect(url_for('trash'))

@app.route('/funded_account/add', methods=['POST'])
@login_required
def add_funded_account():
    firm_id = request.form.get('firm_id')
    phase = request.form.get('phase', 'Funded')
    balance = float(request.form.get('balance', 0))
    
    firm = PropFirm.query.filter_by(id=firm_id, user_id=current_user.id).first_or_404()
    
    new_acc = FundedAccount(user_id=current_user.id, firm_id=firm.id, name=firm.name, phase=phase, initial_balance=balance)
    db.session.add(new_acc)
    db.session.commit()
    # Set as active automatically
    current_user.active_account_id = new_acc.id
    db.session.commit()
    flash(f"'{phase}' added to {firm.name}!", "success")
    
    return redirect(url_for('settings'))

@app.route('/funded_account/edit/<int:account_id>', methods=['POST'])
@login_required
def edit_funded_account(account_id):
    account = FundedAccount.query.filter_by(id=account_id, user_id=current_user.id).first_or_404()
    name = request.form.get('name')
    phase = request.form.get('phase')
    balance = request.form.get('balance')
    
    if name:
        account.name = name
    if phase:
        account.phase = phase
    if balance:
        account.initial_balance = float(balance)
        
    db.session.commit()
    flash("Account updated successfully!", "success")
    return redirect(url_for('settings'))

@app.route('/funded_account/delete/<int:account_id>', methods=['POST'])
@login_required
def delete_funded_account(account_id):
    account = FundedAccount.query.filter_by(id=account_id, user_id=current_user.id).first_or_404()
    
    # Don't delete if it's the only account
    count = FundedAccount.query.filter_by(user_id=current_user.id).count()
    if count <= 1:
        flash("You must have at least one account.", "danger")
        return redirect(url_for('settings'))
    
    account.is_deleted = True
    account.deleted_at = datetime.utcnow()
    db.session.commit()
    
    # Reset active account if we deleted it
    if current_user.active_account_id == account_id:
        other_acc = FundedAccount.query.filter_by(user_id=current_user.id, is_deleted=False).first()
        current_user.active_account_id = other_acc.id if other_acc else None
        db.session.commit()
        
    flash("Account moved to Trash.", "success")
    return redirect(url_for('settings'))

@app.route('/funded_account/restore/<int:account_id>', methods=['POST'])
@login_required
def restore_funded_account(account_id):
    account = FundedAccount.query.filter_by(id=account_id, user_id=current_user.id).first_or_404()
    account.is_deleted = False
    # If the parent firm is deleted, we must restore it too or re-link? 
    # Usually we restore the parent if it's deleted.
    if account.firm and account.firm.is_deleted:
        account.firm.is_deleted = False
        flash(f"Restored {account.phase} and its parent firm {account.firm.name}", "success")
    else:
        flash(f"Restored {account.phase}", "success")
        
    db.session.commit()
    return redirect(url_for('trash'))

@app.route('/funded_account/permanent_delete/<int:account_id>', methods=['POST'])
@login_required
def permanent_delete_funded_account(account_id):
    account = FundedAccount.query.filter_by(id=account_id, user_id=current_user.id, is_deleted=True).first_or_404()
    db.session.delete(account)
    db.session.commit()
    flash("Account phase permanently deleted.", "success")
    return redirect(url_for('trash'))

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

def generate_cloudinary_public_id(base_name="trade_screenshot"):
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d_%H-%M")
    day_str = now.strftime("%A")
    random_hex = secrets.token_hex(4)
    return f"{date_str}_{day_str}_{base_name}_{random_hex}"

def get_cloudinary_folder(subfolder="trades", separate_in_user=False):
    """
    Generates a Cloudinary folder path based on user hierarchy:
    If separate_in_user=True: trading_journal/user_{id}/{subfolder}
    If separate_in_user=False: trading_journal/user_{id}/{FirmName}/{PhaseName}/{subfolder}
    """
    base_folder = f"trading_journal/user_{current_user.id}"
    
    if separate_in_user:
        return f"{base_folder}/{subfolder}"
    
    # Attempt to get Firm and Phase for hierarchical sorting
    try:
        # We check for active_account_id which is stored on the User model
        if hasattr(current_user, 'active_account_id') and current_user.active_account_id:
            account = FundedAccount.query.get(current_user.active_account_id)
            if account:
                firm_name = secure_filename(account.firm.name) if (account.firm and account.firm.name) else "Manual"
                phase_name = secure_filename(account.phase) if account.phase else "Funded"
                final_path = f"{base_folder}/{firm_name}/{phase_name}/{subfolder}"
                print(f"DEBUG: Cloudinary uploading to hierarchical path: {final_path}")
                return final_path
    except Exception as e:
        print(f"Cloudinary Folder Resolution Error: {e}")
        
    final_path = f"{base_folder}/{subfolder}"
    print(f"DEBUG: Cloudinary uploading to fallback path: {final_path}")
    return final_path

def get_cloudinary_id(url):
    """Extracts public ID from Cloudinary URL, normalized for comparison"""
    if not url or 'cloudinary' not in url: return None
    match = re.search(r'trading_journal/.*', url)
    if match:
        pid = match.group(0).rsplit('.', 1)[0]
        # Remove transformation strings like /w_1200,f_auto/
        pid = re.sub(r'\/[a-z]_[a-z0-9,]+', '', pid)
        # Remove versioning if present (e.g., /v123456789/)
        pid = re.sub(r'\/v\d+\/', '/', pid)
        return pid.replace('//', '/')
    return None

def delete_from_cloudinary(url):
    if not url or "cloudinary" not in url:
        return
    try:
        public_id = get_cloudinary_id(url)
        if public_id:
            print(f"Deleting Cloudinary asset: {public_id}")
            cloudinary.uploader.destroy(public_id)
    except Exception as e:
        print(f"Error deleting from Cloudinary: {e}")



# -------------------------------------------------------------
# ---------------- ANALYSIS ----------------

@app.route("/analysis", methods=["GET", "POST"])
@login_required
def analysis():
    if request.method == "POST":
        try:
            trade_date = datetime.strptime(request.form.get("trade_date"), "%Y-%m-%d").date()
            symbol = request.form.get("symbol").upper()
            timeframe = request.form.get("timeframe")
            bias = request.form.get("bias")
            notes = request.form.get("analysis_notes")

            # Image Handling
            before_path = None
            if "before_image" in request.files:
                file = request.files["before_image"]
                if file and file.filename != "":
                    # Cloudinary Upload with Auto-Compression & Resize
                    result = cloudinary.uploader.upload(
                        file,
                        folder=get_cloudinary_folder("analysis", separate_in_user=True),
                        public_id=generate_cloudinary_public_id("analysis_before"),
                        transformation={"width": 1200, "crop": "limit", "quality": "auto", "fetch_format": "auto"}
                    )
                    before_path = result.get("secure_url")

            new_analysis = AnalysisHistory(
                user_id=current_user.id,
                trade_date=trade_date,
                symbol=symbol,
                timeframe=timeframe,
                bias=bias,
                before_image=before_path,
                analysis_notes=notes
            )

            db.session.add(new_analysis)
            db.session.commit()
            flash("Analysis saved successfully!", "success")
            return redirect(url_for("analysis_history"))
            
        except Exception as e:
            print(f"Error saving analysis: {e}")
            flash(f"Error saving analysis: {e}", "danger")
            return redirect(url_for("analysis"))

    return render_template("analysis.html", current_date=date.today().strftime('%Y-%m-%d'))





@app.route("/analysis/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_analysis(id):
    analysis = AnalysisHistory.query.get_or_404(id)
    # Security check
    if analysis.user_id != current_user.id:
        abort(403)

    if request.method == "POST":
        try:
            analysis.trade_date = datetime.strptime(request.form.get("trade_date"), "%Y-%m-%d").date()
            analysis.symbol = request.form.get("symbol").upper()
            analysis.timeframe = request.form.get("timeframe")
            analysis.bias = request.form.get("bias")
            analysis.analysis_notes = request.form.get("analysis_notes")

            # Image Update
            if "before_image" in request.files:
                file = request.files["before_image"]
                if file and file.filename != "":
                    # Cloudinary Upload with Auto-Compression & Resize
                    result = cloudinary.uploader.upload(
                        file,
                        folder=get_cloudinary_folder("analysis", separate_in_user=True),
                        public_id=generate_cloudinary_public_id("analysis_before"),
                        transformation={"width": 1200, "crop": "limit", "quality": "auto", "fetch_format": "auto"}
                    )
                    analysis.before_image = result.get("secure_url")

            if "after_image" in request.files:
                file = request.files["after_image"]
                if file and file.filename != "":
                    # Cloudinary Upload with Auto-Compression & Resize
                    result = cloudinary.uploader.upload(
                        file,
                        folder=get_cloudinary_folder("analysis", separate_in_user=True),
                        public_id=generate_cloudinary_public_id("analysis_after"),
                        transformation={"width": 1200, "crop": "limit", "quality": "auto", "fetch_format": "auto"}
                    )
                    analysis.after_image = result.get("secure_url")

            db.session.commit()
            flash("Analysis updated successfully!", "success")
            return redirect(url_for("analysis_history"))
            
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating analysis: {e}", "danger")

    return render_template("edit_analysis.html", analysis=analysis)


@app.route("/analysis/delete/<int:id>", methods=["POST"])
@login_required
def delete_analysis(id):
    analysis = AnalysisHistory.query.get_or_404(id)
    if analysis.user_id != current_user.id:
        abort(403)
    
    try:
        # Cloudinary Cleanup
        if analysis.before_image:
            delete_from_cloudinary(analysis.before_image)
        if analysis.after_image:
            delete_from_cloudinary(analysis.after_image)

        db.session.delete(analysis)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    return render_template("analysis.html")

@app.route("/analysis/history")
@login_required
def analysis_history():
    query = AnalysisHistory.query.filter_by(user_id=current_user.id)
    
    # Filters
    symbol_filter = request.args.get('symbol')
    date_filter = request.args.get('date')
    
    if symbol_filter:
        query = query.filter(AnalysisHistory.symbol == symbol_filter.upper())
    if date_filter:
        query = query.filter(AnalysisHistory.trade_date == datetime.strptime(date_filter, '%Y-%m-%d').date())
        
    records = query.order_by(AnalysisHistory.trade_date.desc()).all()
    return render_template("analysis_history.html", records=records)

@app.route("/analysis/update/<int:id>", methods=["POST"])
@login_required
def update_analysis(id):
    record = AnalysisHistory.query.get_or_404(id)
    if record.user_id != current_user.id:
        abort(403)
        
    try:
        # Update Before Image
        if "before_image" in request.files:
            file = request.files["before_image"]
            if file and file.filename != "":
                # Cloudinary Upload with Auto-Compression & Resize
                result = cloudinary.uploader.upload(
                    file,
                    folder=get_cloudinary_folder("analysis", separate_in_user=True),
                    public_id=generate_cloudinary_public_id("analysis_before"),
                    transformation={"width": 1200, "crop": "limit", "quality": "auto", "fetch_format": "auto"}
                )
                # Delete old image if it exists
                if record.before_image:
                    delete_from_cloudinary(record.before_image)
                record.before_image = result.get("secure_url")

        # Update After Image
        if "after_image" in request.files:
            file = request.files["after_image"]
            if file and file.filename != "":
                # Cloudinary Upload with Auto-Compression & Resize
                result = cloudinary.uploader.upload(
                    file,
                    folder=get_cloudinary_folder("analysis", separate_in_user=True),
                    public_id=generate_cloudinary_public_id("analysis_after"),
                    transformation={"width": 1200, "crop": "limit", "quality": "auto", "fetch_format": "auto"}
                )
                # Delete old image if it exists
                if record.after_image:
                    delete_from_cloudinary(record.after_image)
                record.after_image = result.get("secure_url")

        # Update Text Fields
        if request.form.get("mistakes"):
            record.mistakes = request.form.get("mistakes")
        if request.form.get("lessons"):
            record.lessons = request.form.get("lessons")
        if request.form.get("analysis_notes"): # Allow updating notes too
            record.analysis_notes = request.form.get("analysis_notes")
            
        db.session.commit()
        flash("Analysis updated!", "success")
    except Exception as e:
        print(f"Error updating analysis: {e}")
        flash("Error updating analysis.", "danger")
        
    return redirect(url_for("analysis_history"))

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

@app.route("/analysis/image/delete", methods=["POST"])
@login_required
def delete_analysis_image():
    data = request.json
    analysis = AnalysisHistory.query.get_or_404(data["analysisId"])

    if analysis.user_id != current_user.id:
        abort(403)

    if data["type"] == "before" and analysis.before_image:
        delete_from_cloudinary(analysis.before_image)
        # Also check local file just in case of old data
        try:
            full_path = os.path.join(app.root_path, "../frontend/src", analysis.before_image)
            if os.path.exists(full_path): os.remove(full_path)
        except: pass
        analysis.before_image = None

    if data["type"] == "after" and analysis.after_image:
        delete_from_cloudinary(analysis.after_image)
        # Also check local file just in case of old data
        try:
            full_path = os.path.join(app.root_path, "../frontend/src", analysis.after_image)
            if os.path.exists(full_path): os.remove(full_path)
        except: pass
        analysis.after_image = None

    db.session.commit()
    return jsonify({"success": True})



# -------------------------------------------------------------
# General Cloudinary Upload API (Handles both files and URLs)
@app.route('/api/cloudinary/upload', methods=['POST'])
@login_required
def api_cloudinary_upload():
    try:
        # 1. Handle File Upload
        if 'file' in request.files:
            file = request.files['file']
            if file and file.filename != '' and allowed_file(file.filename):
                result = cloudinary.uploader.upload(
                    file,
                    folder=get_cloudinary_folder("instant_uploads"),
                    public_id=generate_cloudinary_public_id("instant_file"),
                    transformation={"width": 1200, "crop": "limit", "quality": "auto", "fetch_format": "auto"}
                )
                return jsonify({'success': True, 'url': result.get("secure_url"), 'name': file.filename})

        # 2. Handle URL Upload
        data = request.get_json() if request.is_json else request.form
        url = data.get('url')
        if url and url.strip():
            target_url = url.strip()
            
            # --- TradingView URL Fix ---
            # If it's a TV chart link (e.g., /x/ABCD/), convert to direct S3 link
            if "tradingview.com/x/" in target_url:
                # Extract the ID (e.g., qNE5RuDV from .../x/qNE5RuDV/)
                parts = target_url.strip('/').split('/')
                tv_id = parts[-1]
                if tv_id:
                    first_char = tv_id[0].lower()
                    target_url = f"https://s3.tradingview.com/snapshots/{first_char}/{tv_id}.png"

            result = cloudinary.uploader.upload(
                target_url,
                folder=get_cloudinary_folder("instant_uploads"),
                public_id=generate_cloudinary_public_id("instant_url"),
                transformation={"width": 1200, "crop": "limit", "quality": "auto", "fetch_format": "auto"}
            )
            return jsonify({'success': True, 'url': result.get("secure_url"), 'name': "url_upload"})

        return jsonify({'success': False, 'error': 'No valid file or URL provided'}), 400
    except Exception as e:
        print(f"Cloudinary API Upload Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
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

@app.route('/db-test')
def db_test():
    try:
        # Check connection
        db.session.execute(text('SELECT 1'))
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        # Mask password for safety
        parts = uri.split('@')
        masked_uri = parts[-1] if len(parts) > 1 else uri
        return jsonify({
            "status": "connected",
            "database": masked_uri,
            "using_ssl": "sslmode=require" in uri
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/health")

def health():
    return jsonify({
        "status": "up",
        "root_path": app.root_path,
        "template_paths": [str(p) for p in app.jinja_loader.loaders[0].searchpath] if hasattr(app.jinja_loader, 'loaders') else "ChoiceLoader",
        "db_uri": app.config.get('SQLALCHEMY_DATABASE_URI', '').split('@')[-1], # Mask password
        "cwd": os.getcwd()
    }), 200


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
             flash(f"Database error: {str(e)}")
             return redirect(url_for('index'))

        print(f"DEBUG LOGIN: Email={email}, Found={bool(user)}")
        
        if not user:
            msg = "User not found"
            if 'sqlite' in app.config.get('SQLALCHEMY_DATABASE_URI', ''):
                msg += " (Warning: App is using temporary SQLite storage on Vercel. Accounts will be lost between sessions.)"
            flash(msg)
            return redirect(url_for("index"))


        if user:
             print(f"DEBUG HASH: {user.password_hash}")
             print(f"DEBUG CHECK: {user.check_password(password)}")
             
        if user.check_password(password):
            login_attempts[ip] = 0  # Reset on success
            login_user(user)
            
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

        user = User(username=username, name=name, email=email)
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










@app.route('/dashboard')
@login_required
def dashboard():
    # 🔥 Access Check: Journal user only (treat None as journal)


    active_acc = FundedAccount.query.get(current_user.active_account_id)
    if not active_acc:
        return redirect(url_for('settings')) # Fallback

    trades = Trade.query.filter_by(user_id=current_user.id, account_id=active_acc.id, is_deleted=False).order_by(Trade.date.asc()).all()

    total_trades = len(trades)
    net_profit = sum(t.pnl for t in trades)
    
    wins = len([t for t in trades if t.pnl > 0])
    losses = len([t for t in trades if t.pnl < 0])

    win_rate = round((wins / total_trades) * 100, 2) if total_trades else 0
    profit_factor = round(sum(t.pnl for t in trades if t.pnl > 0) / abs(sum(t.pnl for t in trades if t.pnl < 0)), 2) if any(t.pnl < 0 for t in trades) else 0
    
    # Calculate Equity Curve
    equity_labels = []
    equity_data = []
    running_balance = active_acc.initial_balance
    
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
        "current_balance": active_acc.initial_balance + net_profit
    }
    
    # Calculate today's PnL correctly using SQL (Prop-Firm Standard)
    # Using 'trade' table (singular) as confirmed by model definition
    # COALESCE ensures we get 0 instead of None if no trades today
    # Calculate today's PnL correctly using Broker Day Window (03:30 IST)
    start_utc, end_utc = get_broker_day_window()
    
    today_pnl_row = db.session.execute(text("SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE user_id = :uid AND account_id = :aid AND date >= :start AND date < :end AND (is_deleted = FALSE OR is_deleted IS NULL)"), {"uid": current_user.id, "aid": active_acc.id, "start": start_utc, "end": end_utc}).fetchone()
    
    todays_trades = [t for t in trades if t.date and t.date.date() == date.today()] # Simple day check if utc/ist not critical here, but ideally uses window
    # Actually, let's use all_trades for global gross profit to be accurate across pagination if any
    all_user_trades = Trade.query.filter_by(user_id=current_user.id, account_id=active_acc.id, is_deleted=False).all()
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
        sql = "SELECT date(date, '+2 hours') AS broker_day, SUM(pnl) AS pnl, COUNT(*) AS trade_count FROM trades WHERE user_id = :uid AND account_id = :aid AND (is_deleted = FALSE OR is_deleted IS NULL) AND strftime('%Y-%m', datetime(date, '+2 hours')) = :month GROUP BY broker_day ORDER BY broker_day"
    else:
        sql = "SELECT (date + INTERVAL '2 hours')::date AS broker_day, SUM(pnl) AS pnl, COUNT(*) AS trade_count FROM trades WHERE user_id = :uid AND account_id = :aid AND (is_deleted = FALSE OR is_deleted IS NULL) AND TO_CHAR(date + INTERVAL '2 hours', 'YYYY-MM') = :month GROUP BY broker_day ORDER BY broker_day"

    calendar_data_rows = db.session.execute(text(sql), {"uid": current_user.id, "aid": active_acc.id, "month": month_str}).fetchall()
    
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
                         base_balance=active_acc.initial_balance,
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
        sql = "SELECT date(date, '+2 hours') AS broker_day, SUM(pnl) AS pnl, COUNT(*) AS trade_count FROM trades WHERE user_id = :uid AND account_id = :aid AND (is_deleted = FALSE OR is_deleted IS NULL) AND strftime('%Y-%m', datetime(date, '+2 hours')) = :month GROUP BY broker_day ORDER BY broker_day"
    else:
        sql = "SELECT (date + INTERVAL '2 hours')::date AS broker_day, SUM(pnl) AS pnl, COUNT(*) AS trade_count FROM trades WHERE user_id = :uid AND account_id = :aid AND (is_deleted = FALSE OR is_deleted IS NULL) AND TO_CHAR(date + INTERVAL '2 hours', 'YYYY-MM') = :month GROUP BY broker_day ORDER BY broker_day"
    
    calendar_data_rows = db.session.execute(text(sql), {"uid": current_user.id, "aid": current_user.active_account_id, "month": month_str}).fetchall()
    
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
                        # Cloudinary Upload with Auto-Compression & Resize
                        result = cloudinary.uploader.upload(
                            file,
                            folder=get_cloudinary_folder("trades"),
                            public_id=generate_cloudinary_public_id(f"trade_screenshot_{hashlib.md5(file.filename.encode()).hexdigest()[:8]}"),
                            transformation=[
                                {"width": 1200, "crop": "limit"},
                                {"quality": "auto"},
                                {"fetch_format": "auto"}
                            ]
                        )
                        uploaded_map[file.filename] = result.get("secure_url")

        # Apply Order if provided
        screenshot_order = request.form.get('screenshot_order')
        if screenshot_order:
            try:
                order_list = json.loads(screenshot_order)
                for name in order_list:
                    if name in uploaded_map:
                        screenshots.append(uploaded_map[name])
                        del uploaded_map[name]
                    elif name.startswith('http'):
                        screenshots.append(name)
            except:
                pass
        
        # Append remaining uploads
        for path in uploaded_map.values():
            screenshots.append(path)
        
        # 2. Handle Image URL input field
        screenshot_url = request.form.get('screenshot_url')
        if screenshot_url and screenshot_url.strip():
            target_url = screenshot_url.strip()
            # TradingView Fix
            if "tradingview.com/x/" in target_url:
                parts = target_url.strip('/').split('/')
                tv_id = parts[-1]
                if tv_id:
                    first_char = tv_id[0].lower()
                    target_url = f"https://s3.tradingview.com/snapshots/{first_char}/{tv_id}.png"

            try:
                # Cloudinary Upload
                result = cloudinary.uploader.upload(
                    target_url,
                    folder=get_cloudinary_folder("trades"),
                    public_id=generate_cloudinary_public_id("url_screenshot"),
                    transformation={"width": 1200, "crop": "limit", "quality": "auto", "fetch_format": "auto"}
                )
                screenshots.append(result.get("secure_url"))
            except Exception as e:
                print(f"Error uploading URL to Cloudinary in new: {e}")
                screenshots.append(target_url)

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
            account_id=current_user.active_account_id,
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

    view = request.args.get('view', 'active')
    
    # Filters
    symbol = request.args.get('symbol')
    direction = request.args.get('direction')
    tag = None
    date_filter = request.args.get('date')
    emotion_filter = request.args.get('emotion')
    min_rr = request.args.get('min_rr')

    query = Trade.query.filter_by(user_id=current_user.id, account_id=current_user.active_account_id)
    
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
                        # Cloudinary Upload with Auto-Compression & Resize
                        result = cloudinary.uploader.upload(
                            file,
                            folder=get_cloudinary_folder("trades"),
                            public_id=generate_cloudinary_public_id(f"trade_screenshot_{hashlib.md5(file.filename.encode()).hexdigest()[:8]}"),
                            transformation=[
                                {"width": 1200, "crop": "limit"},
                                {"quality": "auto"},
                                {"fetch_format": "auto"}
                            ]
                        )
                        # Map original filename to saved URL for ordering
                        new_upload_map[file.filename] = result.get("secure_url")
        
        screenshot_url = request.form.get('screenshot_url')
        new_url = None
        if screenshot_url and screenshot_url.strip():
            target_url = screenshot_url.strip()
            # TradingView Fix
            if "tradingview.com/x/" in target_url:
                parts = target_url.strip('/').split('/')
                tv_id = parts[-1]
                if tv_id:
                    first_char = tv_id[0].lower()
                    target_url = f"https://s3.tradingview.com/snapshots/{first_char}/{tv_id}.png"

            try:
                # Upload the URL to Cloudinary
                result = cloudinary.uploader.upload(
                    target_url,
                    folder=get_cloudinary_folder("trades"),
                    public_id=generate_cloudinary_public_id("url_screenshot_edit"),
                    transformation={"width": 1200, "crop": "limit", "quality": "auto", "fetch_format": "auto"}
                )
                new_url = result.get("secure_url")
            except Exception as e:
                print(f"Error uploading URL to Cloudinary in edit: {e}")
                new_url = target_url
            
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
                    elif item.startswith('http'):
                        # This is a newly added Cloudinary URL from the instant "Add to Cloud" button
                        final_screenshots.append(item)
                
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
            
        final_ids = {get_cloudinary_id(u) for u in final_screenshots if get_cloudinary_id(u)}
        
        # Any screenshot that was in current_screenshots but its ID is NOT in final_ids should be deleted
        for url in current_screenshots:
            cid = get_cloudinary_id(url)
            if cid and cid not in final_ids:
                delete_from_cloudinary(url)

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

    target_url = url.strip()
    # TradingView Fix
    if "tradingview.com/x/" in target_url:
        parts = target_url.strip('/').split('/')
        tv_id = parts[-1]
        if tv_id:
            first_char = tv_id[0].lower()
            target_url = f"https://s3.tradingview.com/snapshots/{first_char}/{tv_id}.png"

    try:
        # Upload the provided URL to Cloudinary
        result = cloudinary.uploader.upload(
            target_url,
            folder=get_cloudinary_folder(f"trade_{trade_id}"),
            public_id=generate_cloudinary_public_id("url_screenshot_quick"),
            transformation={"width": 1200, "crop": "limit", "quality": "auto", "fetch_format": "auto"}
        )
        cloud_url = result.get("secure_url")
        
        # Load existing
        current_screenshots = []
        if trade.screenshot:
            try: 
                current_screenshots = json.loads(trade.screenshot)
            except: 
                current_screenshots = [trade.screenshot]

        current_screenshots.append(cloud_url)
        trade.screenshot = json.dumps(current_screenshots)
        
        db.session.commit()
        return jsonify({'success': True, 'url': cloud_url})
    except Exception as e:
        print(f"Error in quick URL upload to Cloudinary: {e}")
        return jsonify({'error': str(e)}), 500


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
        # Cloudinary Cleanup
        if trade.screenshot:
            try:
                screenshots = json.loads(trade.screenshot)
                for url in screenshots:
                    delete_from_cloudinary(url)
            except:
                pass

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
    p_target = goals_row[0] if goals_row else 800.0
    m_loss = goals_row[1] if goals_row else 500.0
    
    current_goals = {
        "profit_target": p_target,
        "max_daily_loss": m_loss
    }
    
    return render_template('settings.html', goals=current_goals)

@app.route('/trash')
@login_required
def trash():
    deleted_firms = PropFirm.query.filter_by(user_id=current_user.id, is_deleted=True).order_by(PropFirm.deleted_at.desc()).all()
    # Individual accounts that are deleted but their firm isn't
    deleted_accounts = FundedAccount.query.join(PropFirm).filter(
        FundedAccount.user_id == current_user.id,
        FundedAccount.is_deleted == True,
        PropFirm.is_deleted == False
    ).order_by(FundedAccount.deleted_at.desc()).all()
    
    return render_template('trash.html', firms=deleted_firms, accounts=deleted_accounts)




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
    is_sqlite = 'sqlite' in db.engine.dialect.name
    
    # 1. Daily Breakdown (Table)
    if is_sqlite:
        daily_sql = "SELECT DATE(date) AS day, COUNT(*) AS trades, SUM(pnl) AS pnl FROM trades WHERE user_id = :uid AND account_id = :aid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY day ORDER BY day DESC"
    else:
        daily_sql = "SELECT date(date) AS day, COUNT(*) AS trades, SUM(pnl) AS pnl FROM trades WHERE user_id = :uid AND account_id = :aid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY day ORDER BY day DESC"
    
    daily = db.session.execute(text(daily_sql), {"uid": current_user.id, "aid": current_user.active_account_id}).fetchall()

    # 2. Weekly Breakdown (Table)
    if is_sqlite:
        weekly_sql = "SELECT strftime('%Y-W%W', date) AS week, COUNT(*) AS trades, SUM(pnl) AS pnl FROM trades WHERE user_id = :uid AND account_id = :aid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY week ORDER BY week DESC"
    else:
        weekly_sql = "SELECT TO_CHAR(date, 'IYYY-IW') AS week, COUNT(*) AS trades, SUM(pnl) AS pnl FROM trades WHERE user_id = :uid AND account_id = :aid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY week ORDER BY week DESC"
        
    weekly = db.session.execute(text(weekly_sql), {"uid": current_user.id, "aid": current_user.active_account_id}).fetchall()

    # 3. Monthly Breakdown (Table)
    if is_sqlite:
        monthly_sql = "SELECT strftime('%Y-%m', date) AS month, COUNT(*) AS trades, SUM(pnl) AS pnl FROM trades WHERE user_id = :uid AND account_id = :aid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY month ORDER BY month DESC"
    else:
        monthly_sql = "SELECT TO_CHAR(date, 'YYYY-MM') AS month, COUNT(*) AS trades, SUM(pnl) AS pnl FROM trades WHERE user_id = :uid AND account_id = :aid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY month ORDER BY month DESC"
        
    monthly = db.session.execute(text(monthly_sql), {"uid": current_user.id, "aid": current_user.active_account_id}).fetchall()
    
    # 4. Equity Curve (Trade-by-Trade) & Drawdown
    # Fetch all trades ordered by date to build granular curve
    all_trades = Trade.query.filter_by(user_id=current_user.id, account_id=current_user.active_account_id, is_deleted=False).order_by(Trade.date.asc()).all()
    
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
        equity_sql = "SELECT date(date, '-6 days', 'weekday 1') as week_start, strftime('%Y-W%W', date) as week_label, SUM(pnl) as pnl FROM trades WHERE user_id = :uid AND account_id = :aid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY week_start, week_label ORDER BY week_start ASC"
    else:
        equity_sql = "SELECT DATE_TRUNC('week', date)::date as week_start, TO_CHAR(date, 'IYYY-IW') as week_label, SUM(pnl) as pnl FROM trades WHERE user_id = :uid AND account_id = :aid AND (is_deleted = FALSE OR is_deleted IS NULL) GROUP BY week_start, week_label ORDER BY week_start ASC"
    
    weekly_equity_rows = db.session.execute(text(equity_sql), {"uid": current_user.id, "aid": current_user.active_account_id}).fetchall()

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

    # 12. Session Win Rates (Updated)
    sessions = {
        "Asian": {"wins": 0, "total": 0, "win_rate": 0, "pnl": 0.0},
        "London": {"wins": 0, "total": 0, "win_rate": 0, "pnl": 0.0},
        "New York": {"wins": 0, "total": 0, "win_rate": 0, "pnl": 0.0}
    }
    
    for t in all_trades:
        if t.session and t.session in sessions:
            sessions[t.session]["total"] += 1
            sessions[t.session]["pnl"] += (t.pnl or 0.0)
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
                is_pg = 'postgres' in db.engine.dialect.name.lower()
                bool_type = "BOOLEAN DEFAULT FALSE" if is_pg else "BOOLEAN DEFAULT 0"
                dt_type = "TIMESTAMP" if is_pg else "DATETIME"

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
                    ("is_deleted", bool_type), ("tags", "TEXT"), ("discipline", "INTEGER"),
                    ("timeframe", "VARCHAR(20)"), ("deleted_at", dt_type),
                    ("rr", "FLOAT"), ("duration", "INTEGER") # Ensure Duration is here
                ]
                for col, dtype in columns:
                    add_column("trades", f"{col} {dtype}")



                # Prop Firm & Account columns
                firm_cols = [("is_deleted", bool_type), ("deleted_at", dt_type)]
                for col, dtype in firm_cols:
                    add_column("prop_firms", f"{col} {dtype}")
                    add_column("funded_accounts", f"{col} {dtype}")

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
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Database initialization failed: {e}")
# ---------------------------------------------------------

if __name__ == '__main__':
    app.run(debug=True)

# Trigger reload

# Trigger reload 2
