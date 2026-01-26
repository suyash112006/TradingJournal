import sys
import os

# Add current directory to path so imports work
sys.path.append(os.getcwd())

from app import app, db
from models import User

def list_users():
    with app.app_context():
        users = User.query.all()
        print(f"found {len(users)} users:")
        for u in users:
            print(f"ID: {u.id} | Email: '{u.email}' | Username: '{u.username}'")

if __name__ == "__main__":
    list_users()
