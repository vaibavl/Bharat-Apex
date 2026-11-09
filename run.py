import os
import sys
import uvicorn
from dotenv import load_dotenv

# Windows console may default to a legacy codec (e.g. cp1252) that can't
# encode the emoji/banner characters below. Force UTF-8 output if possible.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Automatically load environment variables from .env if present
load_dotenv()

# Ensure the project root directory is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

def main():
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "127.0.0.1")

    print("=" * 75)
    print("  🏦 BHARATAPEX AI | CLIENTCARE CONVERSATIONAL BANKING TERMINAL")
    print("  Powered by Vectorize Hindsight Memory & Google Gemini Reasoning")
    print("=" * 75)
    print(f"  • Web Dashboard:     http://{host}:{port}")
    print(f"  • Interactive Docs:  http://{host}:{port}/docs")
    print("  • Hindsight Memory:  Active (Multi-Tenant Bank Isolation)")
    print("  • Personas Loaded:   Aarav (NRI Wealth), Rajesh (MSME), Priya (Retail)")
    print("  • Compliance Layer:  RBI Master Directions / FEMA / DPDP Act 2023")
    print("  • Theme Modes:       Corporate Dark & Executive Bright (One-Click)")
    print("=" * 75)
    print("  Press Ctrl+C to stop the server.\n")

    uvicorn.run("src.api.app:app", host=host, port=port, reload=False)

if __name__ == "__main__":
    main()
