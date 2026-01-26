from app import app, db
from sqlalchemy import text

def add_column(conn, table_name, column_def):
    try:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_def}"))
        print(f"Added column: {column_def}")
    except Exception as e:
        # Check if error is because column already exists
        if "duplicate column name" in str(e).lower():
            print(f"Column already exists: {column_def}")
        else:
            print(f"Could not add column {column_def}: {e}")

with app.app_context():
    with db.engine.connect() as conn:
        print("Starting migration...")
        add_column(conn, "trade", "stop_loss FLOAT")
        add_column(conn, "trade", "take_profit FLOAT")
        add_column(conn, "trade", "screenshot VARCHAR(255)")
        add_column(conn, "trade", "strategy VARCHAR(50)")
        conn.commit()
        print("Migration completed.")
