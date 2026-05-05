import os
from dotenv import load_dotenv

# Load environment variables from .env file (override existing to prevent conflicts)
load_dotenv(override=True)

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "super-secret-key")
    
    # Render provides 'postgres://' which SQLAlchemy 1.4+ deprecated. We must fix it to 'postgresql://'
    uri = os.environ.get("DATABASE_URL", "sqlite:///trading_journal.db")
    if uri and uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    
    # Updated PostgreSQL URL for production fallback if needed
    POSTGRES_FALLBACK = "postgresql://postgresql_tpve_user:NHdE6FK5hGwg5bDR8PDOhqfH9RgkKo2r@dpg-d5rqbc8gjchc739aln70-a.oregon-postgres.render.com/postgresql_tpve"
        
    SQLALCHEMY_DATABASE_URI = uri
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    PERMANENT_SESSION_LIFETIME = 1800  # 30 minutes in seconds

    # Email (Gmail example - fill these with real values to send mail)
    MAIL_SERVER = "smtp.gmail.com"
    MAIL_PORT = 587
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "noreply@tradejournal.com")
    UPLOAD_FOLDER = os.path.join("static", "uploads")
    
    # Cloudinary Configuration
    CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME")
    CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY")
    CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET")

