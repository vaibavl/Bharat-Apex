"""Reset local Hindsight memory to the clean seeded baseline.

Removes every local memory bank (including leftover test banks such as
test_bank_cust_in_999) and re-seeds the three demo customers.
Run from the project root:  python scripts/reset_memory.py
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.hindsight import engine as hs_engine  # noqa: E402

path = hs_engine.STORAGE_FILE
if os.path.exists(path):
    os.remove(path)
    print(f"Removed {path}")

from src.hindsight.client_wrapper import HindsightManager  # noqa: E402

mgr = HindsightManager()  # re-seeds on construction
print("Local memory reset. Banks:", sorted(mgr.local_engine.banks.keys()) if hasattr(mgr.local_engine, "banks") else "seeded")
