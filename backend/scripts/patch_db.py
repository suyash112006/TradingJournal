import sqlite3
import os

db_path = 'journal.db'

print(f"Checking database at: {os.path.abspath(db_path)}")

if not os.path.exists(db_path):
    print("No database found. You can just run 'python app.py' to create a new one with the correct schema.")
else:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    columns = [
        ('monthly_goal', 'FLOAT DEFAULT 1000.0'),
        ('daily_loss_limit', 'FLOAT DEFAULT 200.0')
    ]

    for col_name, col_type in columns:
        try:
            print(f"Attempting to add column: {col_name}...")
            c.execute(f"ALTER TABLE user ADD COLUMN {col_name} {col_type}")
            print(f" -> Success: Added {col_name}")
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                print(f" -> Skipped: {col_name} already exists.")
            else:
                print(f" -> Error adding {col_name}: {e}")

    conn.commit()
    conn.close()
    print("\nDatabase patch completed.")

print("You can now run 'python app.py'")
