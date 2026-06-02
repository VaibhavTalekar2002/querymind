import os
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

GROQ_MODEL_PRIMARY = os.getenv(
    "GROQ_MODEL_PRIMARY",
    "llama-3.3-70b-versatile"
)

GROQ_MODEL_FALLBACK = "llama-3.1-8b-instant"