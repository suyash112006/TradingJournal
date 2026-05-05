import cloudinary
import cloudinary.uploader
import os
from dotenv import load_dotenv

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

try:
    print(f"Cloud Name: {cloudinary.config().cloud_name}")
    print(f"API Key: {cloudinary.config().api_key}")
    # Don't print secret for safety, just check if it's there
    print(f"API Secret set: {bool(cloudinary.config().api_secret)}")
    
    # Try a simple ping or unsigned upload if possible? No, let's just try to get account info
    # Actually, let's try to upload a tiny transparent pixel
    import base64
    pixel = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
    result = cloudinary.uploader.upload(f"data:image/png;base64,{pixel}", folder="test")
    print("Upload successful!")
    print(result.get("secure_url"))
except Exception as e:
    print(f"Error: {e}")
