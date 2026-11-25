# Vercel entry point: every path is routed here (vercel.json) and served by app.py's handler.
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import H  # noqa: E402


class handler(H):  # Vercel looks for a top-level "handler"
    pass
