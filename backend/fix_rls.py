import os
import sys
from sqlalchemy import text

# Add backend to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app import app, db

def enable_rls():
    with app.app_context():
        print("Enabling RLS on tables...")
        tables_to_secure = ['funded_accounts', 'prop_firms', 'media', 'users', 'tasks', 'trades', 'risk_settings', 'analysis_history']
        
        for table in tables_to_secure:
            try:
                # Enable RLS
                db.session.execute(text(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;"))
                print(f"Successfully enabled RLS for {table}")
            except Exception as e:
                print(f"Warning/Error for {table}: {e}")
                db.session.rollback()
        
        db.session.commit()
        print("Done!")

if __name__ == '__main__':
    enable_rls()
