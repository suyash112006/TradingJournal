import sys
import os

# Add current directory to path so imports work
sys.path.append(os.getcwd())

from app import app, db
from models import User

def reset_pass():
    with app.app_context():
        # targeted fix based on db dump
        email = 'suyashzope7@gmail.com'
        user = User.query.filter_by(email=email).first()
        if user:
            user.set_password('123456')
            db.session.commit()
            print(f"✅ Password for {email} reset to: 123456")
        else:
            print(f"❌ User {email} not found.")

if __name__ == "__main__":
    reset_pass()
