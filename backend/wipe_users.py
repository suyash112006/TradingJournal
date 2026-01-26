import sys
import os

# Add current directory to path so imports work
sys.path.append(os.getcwd())

from app import app, db
from models import User

def wipe_users():
    with app.app_context():
        num_deleted = db.session.query(User).delete()
        db.session.commit()
        print(f"✅ Successfully deleted {num_deleted} users.")

if __name__ == "__main__":
    wipe_users()
