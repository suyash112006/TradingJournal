import os
import urllib.parse
from dotenv import load_dotenv

# Explicitly load .env from the backend directory to prevent path issues
env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(dotenv_path=env_path, override=True)

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "super-secret-key")
    
    # Render/Supabase provides 'postgres://' which SQLAlchemy 1.4+ deprecated. We must fix it to 'postgresql://'
    # Try various environment variables common in Vercel integrations
    raw_uri = os.environ.get("DATABASE_URL") or \
              os.environ.get("STORAGE_URL") or \
              os.environ.get("POSTGRES_URL") or \
              "sqlite:///trading_journal.db"
    
    if raw_uri and "://" in raw_uri:
        # Fix protocol
        if raw_uri.startswith("postgres://"):
            raw_uri = raw_uri.replace("postgres://", "postgresql://", 1)
        
    # Handle unencoded '@' in password
    if raw_uri and raw_uri.count('@') > 1:
        try:
            last_at = raw_uri.rfind('@')
            protocol_end = raw_uri.find('://') + 3
            first_colon = raw_uri.find(':', protocol_end)
            if first_colon != -1 and first_colon < last_at:
                user_part = raw_uri[:first_colon+1]
                pass_part = raw_uri[first_colon+1:last_at]
                host_part = raw_uri[last_at:]
                import urllib.parse
                raw_uri = f"{user_part}{urllib.parse.quote(pass_part)}{host_part}"
        except Exception:
            pass

    # Strip pgbouncer=true as it's not supported by psycopg2 but often included by Supabase/Prisma
    if raw_uri and "pgbouncer=true" in raw_uri:
        raw_uri = raw_uri.replace("pgbouncer=true", "")
        # Clean up potential double ampersands or trailing question marks
        raw_uri = raw_uri.replace("&&", "&").replace("?&", "?").rstrip("&? ")

    SQLALCHEMY_DATABASE_URI = raw_uri

    # Print masked URI for debugging in Vercel logs
    _masked = raw_uri.split('@')[-1] if '@' in raw_uri else raw_uri
    print(f"INFO: Database URI Configured: {_masked}")

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    PERMANENT_SESSION_LIFETIME = 1800  # 30 minutes in seconds

    # Email (Gmail example - fill these with real values to send mail)
    MAIL_SERVER = "smtp.gmail.com"
    MAIL_PORT = 587
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "noreply@tradejournal.com")
    # Handle read-only filesystem on Vercel
    if os.environ.get("VERCEL"):
        UPLOAD_FOLDER = "/tmp/uploads"
    else:
        UPLOAD_FOLDER = os.path.join("static", "uploads")

    
    # Cloudinary Configuration
    CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME")
    CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY")
    CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET")

