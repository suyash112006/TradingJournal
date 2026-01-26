
from app import app, db
from sqlalchemy import text

def force_migrate():
    with app.app_context():
        try:
            with db.engine.connect() as conn:
                # 1. Attempt to add deleted_at
                try:
                    conn.execute(text("ALTER TABLE trade ADD COLUMN deleted_at DATETIME"))
                    print("✅ Added 'deleted_at' column.")
                except Exception as e:
                    if "duplicate column" in str(e).lower():
                        print("ℹ️ 'deleted_at' column already exists.")
                    else:
                        print(f"⚠️ Error adding 'deleted_at': {e}")
                
                conn.commit()
                print("Migration check complete.")
        except Exception as e:
            print(f"❌ Migration failed: {e}")

if __name__ == "__main__":
    force_migrate()
