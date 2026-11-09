"""End-to-end simulation of the BharatApex dashboard for every customer.

Run the server first (python run.py), then:

    python scripts/simulate_customer_flows.py

For every entry in src/synthetic_data/customers.json it:
  * logs in through the real HTTP API and verifies the session
  * loads /api/overview and validates accounts + transactions
  * reproduces the front-end cash-flow chart calculation
  * verifies UPI ledger rows and Fixed Deposit investments where present
  * exercises the chat with UPI, FD and cash-flow questions
  * removes the conversations it created

For a deterministic sample of customers (every Nth) it additionally EXECUTES
real money actions through the chat and verifies the ledger moved:

    SIM_ACTION_SAMPLE=10 python scripts/simulate_customer_flows.py
    # (default: 10 customers; set to 0 to skip the executed-action checks)

The report is written to data/simulation_report.json.
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.synthetic_data.data_loader import get_all_customers  # noqa: E402

BASE_URL = os.getenv("SIM_BASE_URL", "http://127.0.0.1:8000")
REPORT_PATH = os.path.join(PROJECT_ROOT, "data", "simulation_report.json")
ACTION_SAMPLE = int(os.getenv("SIM_ACTION_SAMPLE", "10"))
# Optional: restrict the run to specific customer IDs, e.g. 102,104,105 to retry one-off
# latency blips without redoing all 100 customers. Empty = run everyone.
ONLY_CUSTOMERS = {c.strip().removeprefix("CUST-IN-")
                  for c in os.getenv("SIM_CUSTOMERS", "").split(",") if c.strip()}

# The executed-action basket: UPI 2,000 + FD 5,000 + 1 ITC share (490) = 7,490 INR.
ACTION_BASKET_DEBIT = 7490.0


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_overview(customer: dict, overview: dict) -> list:
    """Return a list of human-readable problems for one customer's overview."""
    problems = []
    profile = overview.get("profile") or {}
    txns = overview.get("transactions") or []

    if profile.get("error"):
        problems.append(f"profile error: {profile['error']}")
    if not profile.get("name"):
        problems.append("profile has no name")
    if profile.get("customer_id") != customer["id"]:
        problems.append("profile customer_id does not match login")

    accounts = profile.get("accounts") or []
    if not accounts:
        problems.append("no accounts returned")
    for acc in accounts:
        for key in ("account_id", "type", "currency", "balance", "available_balance"):
            if key not in acc:
                problems.append(f"account {acc.get('account_id')} missing {key}")
        for key in ("balance", "available_balance", "hold_amount"):
            if key in acc and not _is_number(acc[key]):
                problems.append(f"account {acc.get('account_id')} has non-numeric {key}")
        if not _is_number(acc.get("balance_inr")):
            problems.append(f"account {acc.get('account_id')} missing numeric balance_inr")
        if not _is_number(acc.get("balance_usd")):
            problems.append(f"account {acc.get('account_id')} missing numeric balance_usd")

    for t in txns:
        for key in ("transaction_id", "date", "description", "amount_inr", "status", "payment_mode"):
            if key not in t:
                problems.append(f"transaction {t.get('transaction_id')} missing {key}")
        if not _is_number(t.get("amount_inr")):
            problems.append(f"transaction {t.get('transaction_id')} has non-numeric amount_inr")
        if not str(t.get("date", "")).startswith("2026-"):
            problems.append(f"transaction {t.get('transaction_id')} has unexpected date")

    # Reproduce app.js renderCashflow(): last 16 txns, reverse, bar height %.
    chart_txns = txns[:16][::-1]
    amounts = [abs(t.get("amount_inr", 0) or 0) for t in chart_txns]
    max_amount = max(amounts, default=0)
    if txns and max_amount <= 0:
        problems.append("cash-flow chart has no non-zero amounts")
    for amt in amounts:
        height = max(6, round((amt / max(max_amount, 1)) * 100))
        if not (6 <= height <= 100):
            problems.append("cash-flow bar height out of range")

    # UPI data should surface whenever the seed ledger has UPI rows.
    expected_upi = [
        t for t in customer.get("_all_transactions", [])
        if "UPI" in str(t.get("payment_mode", ""))
    ]
    if expected_upi:
        returned_upi = [t for t in txns if "UPI" in str(t.get("payment_mode", ""))]
        if not returned_upi:
            problems.append("UPI transactions exist in seed data but none returned by /api/overview")

    # Existing Fixed Deposit holdings should be present in the profile payload.
    seed_fds = [i for i in customer.get("investments", []) if i.get("asset_class") == "Fixed Deposit"]
    if seed_fds:
        api_fds = [i for i in (profile.get("investments") or [])
                   if i.get("asset_class") == "Fixed Deposit"]
        if not api_fds:
            problems.append("Fixed Deposit investment exists in seed data but is missing from API")

    return problems


def run_action_checks(client: "httpx.Client", row: dict) -> None:
    """EXECUTE a fixed money-action basket via chat and verify the ledger moved.

    Basket: UPI ₹2,000 + FD ₹5,000 (6 months) + 1 ITC share @ ₹490 = ₹7,490 debit.
    """
    result = {"ok": True, "balance_delta_ok": False, "ledger_rows": False,
              "actions": {}, "problems": []}

    def _fail(msg: str) -> None:
        result["ok"] = False
        result["problems"].append(msg)

    before = client.get("/api/overview").raise_for_status().json()
    base_bal = float(((before.get("profile") or {}).get("accounts") or [{}])[0]
                     .get("balance_inr", 0) or 0)
    base_txn_count = len(before.get("transactions") or [])
    base_holdings = len(((before.get("profile") or {}).get("investments") or []))

    conv = None
    for label, msg in [
        ("upi", "Send ₹2,000 to sim.payee@apexmudra via UPI — simulation basket"),
        ("fd", "Open a 6-month fixed deposit of ₹5,000"),
        ("stock", "Buy 1 share of ITC"),
    ]:
        payload = {"message": msg}
        if conv:
            payload["conversation_id"] = conv
        chat = client.post("/api/chat", json=payload)
        if chat.status_code != 200:
            result["actions"][label] = {"ok": False, "status": chat.status_code}
            _fail(f"{label} action chat HTTP {chat.status_code}")
            continue
        data = chat.json()
        conv = data["conversation"]["id"]
        acts = data.get("actions") or []
        executed = [(a.get("action", a.get("tool")), a.get("success")) for a in acts]
        ok = any(s for _, s in executed)
        result["actions"][label] = {"ok": ok, "executed": executed,
                                    "reply_excerpt": (data.get("reply") or "")[:120]}
        if not ok:
            _fail(f"{label} action was not executed (actions={executed})")

    after = client.get("/api/overview").raise_for_status().json()
    new_bal = float(((after.get("profile") or {}).get("accounts") or [{}])[0]
                    .get("balance_inr", 0) or 0)
    delta = round(new_bal - base_bal, 2)
    result["balance_before_inr"] = base_bal
    result["balance_after_inr"] = new_bal
    result["balance_delta_inr"] = delta
    result["balance_delta_ok"] = delta == -ACTION_BASKET_DEBIT
    if not result["balance_delta_ok"]:
        _fail(f"balance delta {delta} != expected -{ACTION_BASKET_DEBIT}")

    ledger = after.get("transactions") or []
    markers = ("UPI Payment to sim.payee@apexmudra", "Fixed Deposit booked", "ITC")
    top3 = " || ".join(str(t.get("description", "")) for t in ledger[:3])
    # The overview caps the returned ledger at 8 rows, so a row-count delta is only
    # meaningful while under the cap. The executed descriptions at the top of the
    # ledger are the deterministic assertion that the basket settled.
    result["ledger_rows"] = all(m in top3 for m in markers)
    result["new_ledger_rows"] = len(ledger) - base_txn_count
    if not result["ledger_rows"]:
        _fail(f"expected 3 new ledger rows with executed desc; top3: {top3[:200]}")

    new_holdings = len(((after.get("profile") or {}).get("investments") or []))
    result["holdings_before"] = base_holdings
    result["holdings_after"] = new_holdings
    if new_holdings < base_holdings:
        _fail("holdings count shrank after actions")

    if conv:
        client.delete(f"/api/conversations/{conv}")

    row["action_checks"] = result
    row["problems"].extend(result["problems"])


def main() -> int:
    customers = get_all_customers()
    by_id = {c["id"]: c for c in customers}

    # Attach the seed transactions so checks can compare API vs source data.
    txns = json.load(open(os.path.join(PROJECT_ROOT, "src", "synthetic_data", "transactions.json"),
                          encoding="utf-8"))
    for t in txns:
        by_id[t["customer_id"]].setdefault("_all_transactions", []).append(t)

    client = httpx.Client(base_url=BASE_URL, timeout=120.0, follow_redirects=True)
    health = client.get("/api/health")
    health.raise_for_status()
    health_data = health.json()

    openapi = client.get("/openapi.json").json()
    paths = sorted(openapi.get("paths", {}).keys())

    results = []
    chat_prompts = [
        ("upi", "What is my UPI ID and what are my UPI transfer limits?"),
        ("fd", "What are the latest FD interest rates? I want to open a fixed deposit."),
        ("cashflow", "Show me my recent transactions and cash flow."),
    ]

    for idx, customer in enumerate(customers, start=1):
        if ONLY_CUSTOMERS and customer["id"].removeprefix("CUST-IN-") not in ONLY_CUSTOMERS:
            continue
        user = customer["login_credentials"]["username"]
        password = customer["login_credentials"]["password"]
        row = {"customer_id": customer["id"], "name": customer["name"], "username": user,
               "login": False, "overview": False, "chat": {}, "problems": []}
        # Deterministic sample for the (ledger-moving) executed-action checks.
        every = max(1, len(customers) // ACTION_SAMPLE) if ACTION_SAMPLE else 0
        do_actions = bool(every) and ((idx - 1) % every == 0) and len(
            [r for r in results if r.get("action_checks")]) < ACTION_SAMPLE

        try:
            login = client.post("/api/auth/login", json={"username": user, "password": password})
            if login.status_code != 200:
                row["problems"].append(f"login HTTP {login.status_code}: {login.text[:160]}")
                results.append(row)
                continue
            login_data = login.json()
            if login_data.get("customer_id") != customer["id"]:
                row["problems"].append("login returned a different customer_id")
            row["login"] = True

            me = client.get("/api/auth/me")
            if me.status_code != 200 or me.json().get("customer_id") != customer["id"]:
                row["problems"].append("session did not restore the logged-in customer")

            overview_res = client.get("/api/overview")
            if overview_res.status_code != 200:
                row["problems"].append(f"overview HTTP {overview_res.status_code}")
            else:
                overview = overview_res.json()
                row["overview"] = True
                row["txn_count"] = len(overview.get("transactions") or [])
                row["account_count"] = len((overview.get("profile") or {}).get("accounts") or [])
                row["upi_txn_count"] = len([
                    t for t in (overview.get("transactions") or [])
                    if "UPI" in str(t.get("payment_mode", ""))
                ])
                row["fd_holding_count"] = len([
                    i for i in ((overview.get("profile") or {}).get("investments") or [])
                    if i.get("asset_class") == "Fixed Deposit"
                ])
                row["problems"].extend(check_overview(customer, overview))

            conversation_id = None
            for label, prompt in chat_prompts:
                payload = {"message": prompt}
                if conversation_id:
                    payload["conversation_id"] = conversation_id
                chat = client.post("/api/chat", json=payload)
                if chat.status_code != 200:
                    row["chat"][label] = {"ok": False, "status": chat.status_code,
                                          "detail": chat.text[:160]}
                    row["problems"].append(f"{label} chat HTTP {chat.status_code}")
                    continue
                chat_data = chat.json()
                conversation_id = chat_data["conversation"]["id"]
                reply = chat_data.get("reply") or ""
                ok = bool(reply.strip())
                row["chat"][label] = {
                    "ok": ok,
                    "latency_ms": chat_data.get("latency_ms"),
                    "actions": [a.get("action", a.get("tool")) for a in chat_data.get("actions", [])],
                    "reply_excerpt": reply[:180],
                }
                if not ok:
                    row["problems"].append(f"{label} chat returned an empty reply")

            if conversation_id:
                client.delete(f"/api/conversations/{conversation_id}")

            # EXECUTED money actions: UPI + FD + stock through chat (sampled).
            if do_actions:
                run_action_checks(client, row)

            # Fail fast if a bug is customer-specific.
            client.post("/api/auth/logout")
        except Exception as exc:  # noqa: BLE001 - report, don't abort the whole run
            row["problems"].append(f"exception: {exc}")
        finally:
            results.append(row)

        if idx % 10 == 0:
            print(f"  checked {idx}/{len(customers)} customers...")

    # Security regression: a wrong password must be rejected.
    bad = client.post("/api/auth/login", json={"username": "aarav_8214", "password": "definitely-wrong"})
    bad_login_ok = bad.status_code == 401

    client.close()

    failed = [r for r in results if r["problems"]]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "health": health_data,
        "api_paths": paths,
        "summary": {
            "customers": len(customers),
            "login_ok": sum(1 for r in results if r["login"]),
            "overview_ok": sum(1 for r in results if r["overview"]),
            "chat_calls": sum(len(r["chat"]) for r in results),
            "chat_ok": sum(1 for r in results for c in r["chat"].values() if c.get("ok")),
            "failed_customers": len(failed),
            "wrong_password_rejected": bad_login_ok,
            "customers_with_fd_holding": sum(1 for r in results if r.get("fd_holding_count")),
            "customers_with_upi_rows": sum(1 for r in results if r.get("upi_txn_count")),
            "action_checks": sum(1 for r in results if r.get("action_checks")),
            "action_checks_ok": sum(1 for r in results
                                    if (r.get("action_checks") or {}).get("ok")),
        },
        "bad_login_status": bad.status_code,
        "failures": failed,
        "results": results,
        "feature_endpoint_check": {
            "upi_transfer_endpoint": any("upi" in p.lower() and "transfer" in p.lower() for p in paths),
            "fd_open_endpoint": any(("fd" in p.lower() or "deposit" in p.lower())
                                    and ("open" in p.lower() or "create" in p.lower())
                                    for p in paths),
            "note": "UPI transfers, FD bookings and stock purchases EXECUTE through the chat "
                    "engine (deterministic core-banking execution + LLM tool calls); there are "
                    "no dedicated REST endpoints and none are needed.",
        },
    }

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    s = report["summary"]
    print("\n" + "=" * 64)
    print("SIMULATION SUMMARY")
    print("=" * 64)
    print(f"Customers from customers.json : {s['customers']}")
    print(f"Login succeeded               : {s['login_ok']}/{s['customers']}")
    print(f"Overview loaded               : {s['overview_ok']}/{s['customers']}")
    print(f"Chat calls succeeded          : {s['chat_ok']}/{s['chat_calls']}")
    print(f"Customers with failures       : {s['failed_customers']}")
    print(f"Wrong password rejected       : {s['wrong_password_rejected']}")
    print(f"Action baskets executed       : {s['action_checks_ok']}/{s['action_checks']}")
    print(f"Customers showing FD holdings : {s['customers_with_fd_holding']}")
    print(f"Customers showing UPI rows    : {s['customers_with_upi_rows']}")
    print(f"UPI transfer endpoint present : {report['feature_endpoint_check']['upi_transfer_endpoint']}")
    print(f"FD opening endpoint present   : {report['feature_endpoint_check']['fd_open_endpoint']}")
    print(f"Report                        : {REPORT_PATH}")
    if failed:
        print("\nFirst failures:")
        for r in failed[:10]:
            print(f"  {r['customer_id']}: {'; '.join(r['problems'][:3])}")
    return 0 if (s["failed_customers"] == 0 and bad_login_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
