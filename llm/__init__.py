"""Gemini layer and claims validator for Pocketwatch.

Loads .env on package import so GEMINI_API_KEY is available regardless of
entry point (FastAPI app, pytest, or a standalone script) — previously only
api/main.py called load_dotenv(), so anything importing llm/ directly (tests,
scripts/check_evidence.py, a bare `python -c`) saw os.getenv("GEMINI_API_KEY")
return None and silently fell back to the heuristic/template path.
"""
from dotenv import load_dotenv

load_dotenv()
