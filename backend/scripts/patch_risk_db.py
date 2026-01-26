import sqlite3
import os

DB_PATH = 'journal.db'

def patch():
    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found!")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    print("Checking for risk_settings table...")
    # Create table
    try:
        c.execute('''
            CREATE TABLE IF NOT EXISTS risk_settings (
                id INTEGER PRIMARY KEY,
                profit_target REAL DEFAULT 800.0,
                max_daily_loss REAL DEFAULT 500.0
            )
        ''')
        print("Table 'risk_settings' ensured.")
    except Exception as e:
        print(f"Error creating table: {e}")
        conn.close()
        return
    
    # Check if we need to insert default
    c.execute('SELECT count(*) FROM risk_settings')
    count = c.fetchone()[0]
    
    if count == 0:
        print("Seeding default risk settings (Target: $800, Max Loss: $500)...")
        c.execute('INSERT INTO risk_settings (profit_target, max_daily_loss) VALUES (800.0, 500.0)')
    else:
        print(f"Risk settings already exist ({count} row(s)).")
        # Optional: Print current settings
        c.execute('SELECT * FROM risk_settings')
        print(f"Current settings: {c.fetchone()}")
        
    conn.commit()
    conn.close()
    print("Patch completed successfully.")

if __name__ == '__main__':
    patch()
