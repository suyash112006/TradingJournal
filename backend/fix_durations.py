from app import app, db, Trade

with app.app_context():
    trades = Trade.query.filter(Trade.duration == None).all()
    count = 0
    for t in trades:
        t.duration = 2700 # 45 minutes * 60
        count += 1
    
    db.session.commit()
    print(f"Updated {count} trades with default duration.")
