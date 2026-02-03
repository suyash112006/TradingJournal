from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from flask_bcrypt import Bcrypt
from datetime import datetime

db = SQLAlchemy()
bcrypt = Bcrypt()

class User(db.Model, UserMixin):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    name = db.Column(db.String(100))
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default="user")  # user / admin
    account_type = db.Column(db.String(20), default="journal")  # journal / backtest
    
    # Trading Settings
    account_name = db.Column(db.String(100), default="My Trading Account")
    initial_balance = db.Column(db.Float, default=0.0)
    monthly_goal = db.Column(db.Float, default=1000.0)
    daily_loss_limit = db.Column(db.Float, default=200.0)

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)

class Task(db.Model):
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.String(255), nullable=False)
    is_completed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('tasks', lazy=True))

class Trade(db.Model):
    __tablename__ = "trades"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    symbol = db.Column(db.String(20), nullable=False)
    direction = db.Column(db.String(10), nullable=False)
    strategy = db.Column(db.String(50))
    quantity = db.Column(db.Float, default=1.0)

    entry_price = db.Column(db.Float, nullable=False)
    exit_price = db.Column(db.Float, nullable=False)
    stop_loss = db.Column(db.Float)
    take_profit = db.Column(db.Float)

    pnl = db.Column(db.Float, default=0.0)
    result = db.Column(db.String(10))
    notes = db.Column(db.Text)
    screenshot = db.Column(db.Text)  # Stores JSON list of paths/URLs
    session = db.Column(db.String(20))

    date = db.Column(db.DateTime, default=datetime.utcnow)
    is_deleted = db.Column(db.Boolean, default=False)
    deleted_at = db.Column(db.DateTime) # Track when it was deleted
    tags = db.Column(db.Text)
    discipline = db.Column(db.Integer)
    rr = db.Column(db.Float)
    duration = db.Column(db.Integer) # Duration in seconds
    
    # New SaaS Fields
    emotion = db.Column(db.String(50))
    timeframe = db.Column(db.String(20))

class RiskSettings(db.Model):
    __tablename__ = "risk_settings"
    id = db.Column(db.Integer, primary_key=True)
    profit_target = db.Column(db.Float, default=800.0)
    max_daily_loss = db.Column(db.Float, default=500.0)

class AnalysisHistory(db.Model):
    __tablename__ = "analysis_history"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)

    trade_date = db.Column(db.Date, nullable=False)
    symbol = db.Column(db.String(20), nullable=False)

    timeframe = db.Column(db.String(10))
    bias = db.Column(db.Text)

    before_image = db.Column(db.String(255))
    after_image = db.Column(db.String(255))

    analysis_notes = db.Column(db.Text)
    mistakes = db.Column(db.Text)
    lessons = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
