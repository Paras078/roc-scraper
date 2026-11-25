# Vercel entry point: every path is rewritten here (vercel.json) and served by app.py's handler.
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import H as handler  # noqa: E402
