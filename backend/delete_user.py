import sys
import os

# Add current directory to path so imports work
sys.path.append(os.getcwd())

from app import app, db
from models import User

def delete_target_users():
    with app.app_context():
        # Delete anyone starting with 'suyashzope' to be safe vs typos
        target_pattern = "suyashzope%"
        users = User.query.filter(User.email.like(target_pattern)).all()
        
        if not users:
            print("❌ No users found matching 'suyashzope%'")
            return

        for u in users:
            print(f"Deleting ID: {u.id} | Email: {u.email}")
            db.session.delete(u)
        
        db.session.commit()
        print(f"✅ Successfully deleted {len(users)} users.")

if __name__ == "__main__":
    delete_target_users()
