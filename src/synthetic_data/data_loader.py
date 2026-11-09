"""
Synthetic FinTech Data Loader.
Loads customer profiles, transactions, and banking policies from JSON files.
"""

import json
import os
from typing import Dict, Any, List, Optional

DATA_DIR = os.path.dirname(os.path.abspath(__file__))


def load_json(filename: str) -> Any:
    """Load and parse a JSON file from the synthetic_data directory."""
    path = os.path.join(DATA_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_all_customers() -> List[Dict[str, Any]]:
    """Return the full list of synthetic customer profiles."""
    return load_json("customers.json")


def get_customer_by_id(customer_id: str) -> Optional[Dict[str, Any]]:
    """Look up a single customer by their ID (case-insensitive)."""
    customers = get_all_customers()
    for cust in customers:
        if cust["id"].lower() == customer_id.lower():
            return cust
    return None


def get_transactions_by_customer(customer_id: str) -> List[Dict[str, Any]]:
    """Return all transactions belonging to a specific customer."""
    txns = load_json("transactions.json")
    return [t for t in txns if t["customer_id"].lower() == customer_id.lower()]


def get_all_policies() -> Dict[str, Any]:
    """Return the full set of banking policies and compliance rules."""
    return load_json("policies.json")
