import sys
import os
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from app import app, db
from models import PropFirm, FundedAccount, Trade
from sqlalchemy import or_

def cleanup_deleted_flags():
    with app.app_context():
        print("Cleaning up is_deleted flags...")
        
        # PropFirms
        firms = PropFirm.query.filter(PropFirm.is_deleted == None).all()
        for f in firms:
            f.is_deleted = False
        print(f"Updated {len(firms)} PropFirms")
        
        # Accounts
        accs = FundedAccount.query.filter(FundedAccount.is_deleted == None).all()
        for a in accs:
            a.is_deleted = False
        print(f"Updated {len(accs)} FundedAccounts")
        
        # Trades
        trades = Trade.query.filter(Trade.is_deleted == None).all()
        for t in trades:
            t.is_deleted = False
        print(f"Updated {len(trades)} Trades")
        
        db.session.commit()
        print("Done.")

if __name__ == "__main__":
    cleanup_deleted_flags()
