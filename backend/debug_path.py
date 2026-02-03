from app import app, get_user_mt5_base
import os

with app.app_context():
    print(f"App Instance Path: {app.instance_path}")
    print(f"User 1 Base: {get_user_mt5_base(1)}")
