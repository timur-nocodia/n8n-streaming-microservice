import jwt
import time
import os
from dotenv import load_dotenv

load_dotenv()

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise ValueError("JWT_SECRET not found in environment")

token = jwt.encode(
    {
        "role": "n8n",
        "exp": time.time() + 300
    },
    JWT_SECRET,
    "HS256"
)

print(token)