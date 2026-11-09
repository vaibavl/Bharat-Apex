from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta
import uuid
from ..synthetic_data.data_loader import get_customer_by_id, get_transactions_by_customer, get_all_policies

USD_INR_RATE = 94.11

# ---------------------------------------------------------------- constants
NPCI_UPI_PER_TXN_LIMIT = 100000.0     # ₹1,00,000 per transaction (NPCI)
NPCI_UPI_DAILY_LIMIT = 200000.0       # ₹2,00,000 per rolling day (this bank)
NPCI_UPI_DAILY_MAX_TXNS = 20          # 20 transfers per rolling day
FD_MIN_BOOKING_INR = 1000.0           # minimum FD booking amount (₹1,000)

# Synthetic market data (demo instruments; prices in INR, T+1 settlement)
MARKET_INSTRUMENTS: Dict[str, Dict[str, Any]] = {
    "HDFCBANK.NS":    {"name": "HDFC Bank",            "price": 1940.00, "asset_class": "Equity",
                       "alias": ["hdfc bank", "hdfcbank", "hdfc"]},
    "RELIANCE.NS":    {"name": "Reliance Industries",  "price": 1610.00, "asset_class": "Equity",
                       "alias": ["reliance", "ril"]},
    "TCS.NS":         {"name": "Tata Consultancy Services", "price": 3150.00, "asset_class": "Equity",
                       "alias": ["tcs", "tata consultancy", "tata consultancy services"]},
    "INFY.NS":        {"name": "Infosys",              "price": 1720.00, "asset_class": "Equity",
                       "alias": ["infosys", "infy"]},
    "ICICIBANK.NS":   {"name": "ICICI Bank",           "price": 1280.00, "asset_class": "Equity",
                       "alias": ["icici bank", "icicibank", "icici"]},
    "SBIN.NS":        {"name": "State Bank of India",  "price": 890.00,  "asset_class": "Equity",
                       "alias": ["state bank", "state bank of india", "sbin", "sbi"]},
    "ITC.NS":         {"name": "ITC Ltd",              "price": 490.00,  "asset_class": "Equity",
                       "alias": ["itc"]},
    "TATAMOTORS.NS":  {"name": "Tata Motors",          "price": 980.00,  "asset_class": "Equity",
                       "alias": ["tata motors", "tatamotors"]},
    "WIPRO.NS":       {"name": "Wipro",                "price": 560.00,  "asset_class": "Equity",
                       "alias": ["wipro"]},
    "NIFTY50_IDX_DIRECT": {"name": "Nifty 50 Index Fund", "price": 245.50, "asset_class": "Mutual Fund",
                       "alias": ["nifty50", "nifty 50", "nifty index fund", "index fund", "nifty mutual fund"]},
}


def _inr(amount) -> str:
    """Indian digit grouping for display: ₹12,34,567.89 (no locale dependency)."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    neg = amount < 0
    s = f"{abs(round(amount, 2)):.2f}"
    whole, frac = s.split(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    if whole:
        groups.insert(0, whole)
    return ("-" if neg else "") + "₹" + ",".join(groups) + "." + frac


def resolve_instrument(query: str) -> Optional[Dict[str, Any]]:
    """Match a customer phrase / ticker to a tradable instrument (case-insensitive)."""
    q = " ".join(str(query or "").lower().split())
    if not q:
        return None
    q = q.replace(".", " ").strip()  # normalize "hdfcbank ns" style
    for ticker, inst in MARKET_INSTRUMENTS.items():
        aliases = [ticker.lower(), inst["name"].lower(), *inst["alias"]]
        for a in aliases:
            a_norm = " ".join(a.replace(".", " ").split())
            if a_norm and a_norm in q:
                return {"ticker": ticker, **inst}
    return None

class CoreBankingService:
    """
    Enterprise Indian & Cross-Border Core Banking & Ledger API.
    Handles dual-currency accounts (INR ₹ & USD $), real-time balance mutations,
    UPI settlements, card locks, fee waivers, FIRC inward wire hold releases, and chargebacks.
    """

    def __init__(self):
        self.card_overrides: Dict[str, str] = {}
        self.card_intl_overrides: Dict[str, bool] = {}
        self.disputes: List[Dict[str, Any]] = []
        self.waiver_history: Dict[str, List[Dict[str, Any]]] = {
            "CUST-IN-102": [
                {
                    "date": "2026-10-05T10:20:00Z",
                    "amount_inr": 350.00,
                    "type": "NACH Mandate Return Fee",
                    "status": "Approved One-Time Courtesy per RBI Circular"
                }
            ]
        }
        self.travel_notices: List[Dict[str, Any]] = []
        self.wire_clearances: Dict[str, str] = {}
        # Runtime balance modifications (e.g. holds released, waivers credited)
        self.account_hold_overrides: Dict[str, float] = {}
        self.account_balance_adjustments: Dict[str, float] = {}
        # Executed (ledger-moving) actions: UPI transfers, FD bookings, stock purchases
        self.runtime_transactions: Dict[str, List[Dict[str, Any]]] = {}
        self.fd_accounts: Dict[str, List[Dict[str, Any]]] = {}
        self.investment_additions: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.upi_daily_tracker: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------- helpers
    def _customer(self, customer_id: str) -> Optional[Dict[str, Any]]:
        return get_customer_by_id(customer_id)

    def _primary_inr_account(self, cust: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for acc in cust.get("accounts", []):
            if acc.get("currency") == "INR":
                return acc
        accounts = cust.get("accounts") or []
        return accounts[0] if accounts else None

    def _adjusted_available(self, cust: Dict[str, Any], acc: Dict[str, Any]) -> float:
        """Live available balance = seat amount minus every debit applied at runtime."""
        bal = float(acc.get("available_balance", 0.0) or 0.0)
        bal += float(self.account_balance_adjustments.get(acc["account_id"], 0.0) or 0.0)
        return bal

    def _debit_account(self, acc: Dict[str, Any], amount: float) -> None:
        self.account_balance_adjustments[acc["account_id"]] = \
            self.account_balance_adjustments.get(acc["account_id"], 0.0) - float(amount)

    def _log_runtime_txn(self, customer_id: str, txn: Dict[str, Any]) -> None:
        self.runtime_transactions.setdefault(customer_id, []).insert(0, txn)

    def _stamp_txn(self, customer_id: str, account_id: str) -> Dict[str, Any]:
        policies = get_all_policies()
        fx_rate = policies.get("fx_rate_usd_to_inr", USD_INR_RATE)
        return {"customer_id": customer_id, "account_id": account_id,
                "date": datetime.now(timezone.utc).isoformat(), "currency": "INR",
                "fx_rate": fx_rate}

    def _new_ref(self, prefix: str, length: int = 10) -> str:
        return f"{prefix}{uuid.uuid4().hex[:length].upper()}"

    def _upi_usage_today(self, customer_id: str) -> Dict[str, Any]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        usage = self.upi_daily_tracker.get(customer_id)
        if not usage or usage.get("date") != today:
            return {"date": today, "count": 0, "total_inr": 0.0}
        return usage

    def _merge_investments(self, cust: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Seed portfolio merged with FD bookings and stock purchases made this session."""
        customer_id = cust["id"]
        additions = self.investment_additions.get(customer_id, {})
        merged: List[Dict[str, Any]] = []
        for inv in cust.get("investments", []):
            entry = dict(inv)
            add = additions.get(str(entry.get("ticker", "")))
            if add:
                entry["units"] = round(float(entry.get("units", 0) or 0) + add["units"], 2)
                entry["current_value_inr"] = round(
                    float(entry.get("current_value_inr", 0) or 0) + add["current_value_inr"], 2)
                entry.setdefault("asset_class", add.get("asset_class", "Equity"))
            merged.append(entry)
        for ticker, add in additions.items():
            if not any(str(i.get("ticker", "")) == ticker for i in merged):
                merged.append({"asset_class": add.get("asset_class", "Equity"), "ticker": ticker,
                               "units": add["units"],
                               "current_value_inr": round(add["current_value_inr"], 2)})
        # newest positions first (today's bookings at the top)
        rank = {"Fixed Deposit": 0, "Mutual Fund": 1, "Equity": 2}
        return sorted(merged, key=lambda i: rank.get(i.get("asset_class"), 9))

    def fd_rate_for(self, tenure_months: int, tier: str = "") -> float:
        """POL-RATES-FD-009 rate card: 6.75% (6m) → 7.65% (3y tax-saver), +0.50% seniors."""
        months = int(tenure_months)
        if months <= 6:
            rate = 6.75
        elif months <= 12:
            rate = 6.85
        elif months <= 24:
            rate = 7.10
        else:
            rate = 7.65          # >3 years: max tenure tax-saver bucket
        if "senior" in str(tier or "").lower():
            rate += 0.50
        return round(rate, 2)
        # Executed (ledger-moving) actions: UPI transfers, FD bookings, stock purchases
        self.runtime_transactions: Dict[str, List[Dict[str, Any]]] = {}
        self.fd_accounts: Dict[str, List[Dict[str, Any]]] = {}
        self.investment_additions: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.upi_daily_tracker: Dict[str, Dict[str, Any]] = {}

    def get_account_summary(self, customer_id: str) -> Dict[str, Any]:
        cust = get_customer_by_id(customer_id)
        if not cust:
            return {"error": "Customer not found"}

        policies = get_all_policies()
        fx_rate = policies.get("fx_rate_usd_to_inr", USD_INR_RATE)

        enriched_accounts = []
        total_inr = 0.0
        total_usd = 0.0

        for acc in cust.get("accounts", []):
            acc_copy = dict(acc)
            acc_id = acc_copy["account_id"]
            
            # Apply runtime hold overrides and balance adjustments
            if acc_id in self.account_hold_overrides:
                acc_copy["hold_amount"] = self.account_hold_overrides[acc_id]
                if acc_copy["hold_amount"] == 0:
                    acc_copy["hold_reason"] = "All security holds resolved"

            if acc_id in self.account_balance_adjustments:
                acc_copy["available_balance"] += self.account_balance_adjustments[acc_id]
                acc_copy["balance"] += self.account_balance_adjustments[acc_id]

            bal = acc_copy["available_balance"]
            curr = acc_copy["currency"]

            if curr == "INR":
                acc_copy["balance_inr"] = bal
                acc_copy["balance_usd"] = round(bal / fx_rate, 2)
                total_inr += bal
                total_usd += bal / fx_rate
            else:
                acc_copy["balance_usd"] = bal
                acc_copy["balance_inr"] = round(bal * fx_rate, 2)
                total_usd += bal
                total_inr += bal * fx_rate

            enriched_accounts.append(acc_copy)

        # Session-created Fixed Deposit accounts (booked via chat) sit next to
        # the seeded ones in the same wallet list.
        for fd in self.fd_accounts.get(customer_id, []):
            fd_copy = dict(fd)
            policies_fx = policies.get("fx_rate_usd_to_inr", USD_INR_RATE)
            fd_copy["balance_inr"] = fd_copy["balance"]
            fd_copy["balance_usd"] = round(fd_copy["balance"] / policies_fx, 2)
            total_inr += fd_copy["balance"]
            total_usd += fd_copy["balance"] / policies_fx
            enriched_accounts.append(fd_copy)

        # Apply card status and international overrides
        cards = []
        for c in cust.get("cards", []):
            card_copy = dict(c)
            if c["card_id"] in self.card_overrides:
                card_copy["status"] = self.card_overrides[c["card_id"]]
            if c["card_id"] in self.card_intl_overrides:
                card_copy["international_enabled"] = self.card_intl_overrides[c["card_id"]]
            cards.append(card_copy)

        # Holdings merge FD bookings + stock purchases with the seed portfolio,
        # and the financial card gets the LIVE balances after every executed action.
        merged_investments = self._merge_investments(cust)
        primary = cust.get("accounts", [{}])[0]
        primary_available = self._adjusted_available(cust, primary) if primary else 0.0
        inv_total = round(sum(float(i.get("current_value_inr", 0) or 0) for i in merged_investments), 2)
        financials = dict(cust.get("financials") or {})
        financials["available_bank_balance_inr"] = round(primary_available, 2)
        financials["total_investment_value_inr"] = inv_total
        financials["net_worth_inr"] = round(primary_available + inv_total, 2)

        return {
            "customer_id": cust["id"],
            "name": cust["name"],
            "tier": cust["tier"],
            "city": cust.get("city", "Hyderabad"),
            "kyc_status": cust["kyc_status"],
            "upi_id": cust.get("upi_id", "N/A"),
            "accounts": enriched_accounts,
            "total_liquid_inr": round(total_inr, 2),
            "total_liquid_usd": round(total_usd, 2),
            "cards": cards,
            "investments": merged_investments,
            "financials": financials,
            "bank_details": cust.get("bank_details", {}),
            "risk_score": cust["risk_score"],
            "branch": "MG Road Corporate Branch, Bengaluru",
            "ifsc": policies.get("ifsc_code", "APEX0004921"),
            "fx_rate": fx_rate
        }

    def get_transactions(self, customer_id: str, limit: int = 8) -> List[Dict[str, Any]]:
        txns = get_transactions_by_customer(customer_id)
        # Runtime ledger rows (UPI / FD / stock actions) are newest first.
        runtime = [dict(t) for t in self.runtime_transactions.get(customer_id, [])]
        result = runtime + [dict(t) for t in txns]
        for t in result:
            if t["transaction_id"] in self.wire_clearances:
                t["status"] = self.wire_clearances[t["transaction_id"]]

        # Prepend runtime disputes
        for d in self.disputes:
            if d["customer_id"] == customer_id:
                result.insert(0, {
                    "transaction_id": d["dispute_id"],
                    "customer_id": customer_id,
                    "account_id": "ACC-DISPUTE",
                    "date": d["date_filed"],
                    "description": f"Provisional Credit: {d['reason'][:30]}...",
                    "category": "Grievance Adjustment",
                    "amount_inr": 899.00,
                    "amount_usd": 10.39,
                    "currency": "INR",
                    "status": "Provisional Credit Posted",
                    "payment_mode": "RBI Grievance Reversal",
                    "utr_ref": f"REV{d['dispute_id'][-6:]}",
                    "disputable": False
                })

        return result[:limit]

    def freeze_card(self, customer_id: str, card_id: str, reason: str = "Customer security request") -> Dict[str, Any]:
        cust = get_customer_by_id(customer_id)
        if not cust:
            return {"success": False, "message": "Customer account not found"}

        self.card_overrides[card_id] = "Frozen - Protection Active"
        return {
            "success": True,
            "action": "FREEZE_CARD",
            "card_id": card_id,
            "status": "Frozen - Protection Active",
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": f"Card {card_id} has been blocked immediately per RBI electronic security protocol. Pre-scheduled UPI AutoPay remains active, but all new ATM/POS/E-Com authorizations are blocked."
        }

    def unfreeze_card(self, customer_id: str, card_id: str) -> Dict[str, Any]:
        self.card_overrides[card_id] = "Active"
        return {
            "success": True,
            "action": "UNFREEZE_CARD",
            "card_id": card_id,
            "status": "Active",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": f"Card {card_id} has been successfully reactivated and unblocked. Contactless tap-to-pay, chip, and online payments are active."
        }

    def toggle_card_freeze(self, customer_id: str, card_id: str) -> Dict[str, Any]:
        current_status = self.card_overrides.get(card_id)
        if not current_status:
            cust = get_customer_by_id(customer_id)
            for c in (cust.get("cards", []) if cust else []):
                if c["card_id"] == card_id:
                    current_status = c.get("status", "Active")
                    break
        is_frozen = "frozen" in str(current_status or "").lower()
        if is_frozen:
            return self.unfreeze_card(customer_id, card_id)
        else:
            return self.freeze_card(customer_id, card_id, reason="User toggled card security lock")

    def toggle_card_international(self, customer_id: str, card_id: str) -> Dict[str, Any]:
        cust = get_customer_by_id(customer_id)
        current_intl = self.card_intl_overrides.get(card_id)
        if current_intl is None:
            for c in (cust.get("cards", []) if cust else []):
                if c["card_id"] == card_id:
                    current_intl = bool(c.get("international_enabled", False))
                    break
        new_intl = not bool(current_intl)
        self.card_intl_overrides[card_id] = new_intl
        return {
            "success": True,
            "action": "CARD_INTERNATIONAL_TOGGLED",
            "card_id": card_id,
            "international_enabled": new_intl,
            "message": f"International usage {'enabled' if new_intl else 'disabled'} for card {card_id}."
        }

    def check_fee_waiver_eligibility(self, customer_id: str) -> Dict[str, Any]:
        cust = get_customer_by_id(customer_id)
        if not cust:
            return {"eligible": False, "reason": "Customer record not found"}

        past_waivers = self.waiver_history.get(customer_id, [])
        if past_waivers:
            last = past_waivers[-1]
            return {
                "eligible": False,
                "customer_tier": cust["tier"],
                "reason": f"A prior courtesy fee reversal (₹{last['amount_inr']} on {last['date'][:10]}) was already granted within the 12-month rolling period. Policy POL-RBI-FEE-001 requires Regional Branch Manager sign-off.",
                "requires_escalation": True,
                "past_waiver_count": len(past_waivers)
            }

        return {
            "eligible": True,
            "customer_tier": cust["tier"],
            "reason": "Account maintains high CIBIL and clean standing with zero penalty reversals in the past 12 months. Eligible for automated courtesy credit.",
            "requires_escalation": False,
            "past_waiver_count": 0
        }

    def process_fee_waiver(self, customer_id: str, fee_amount_inr: float, reason: str) -> Dict[str, Any]:
        eligibility = self.check_fee_waiver_eligibility(customer_id)
        if not eligibility["eligible"]:
            return {
                "success": False,
                "message": eligibility["reason"],
                "requires_escalation": True
            }

        record = {
            "date": datetime.now(timezone.utc).isoformat(),
            "amount_inr": fee_amount_inr,
            "type": "Courtesy Reversal",
            "status": "Approved - Credited to Savings",
            "reason": reason
        }
        if customer_id not in self.waiver_history:
            self.waiver_history[customer_id] = []
        self.waiver_history[customer_id].append(record)

        # Apply real-time balance mutation: release hold and increase available balance
        cust = get_customer_by_id(customer_id)
        if cust and cust.get("accounts"):
            primary_acc_id = cust["accounts"][0]["account_id"]
            self.account_hold_overrides[primary_acc_id] = 0.0
            self.account_balance_adjustments[primary_acc_id] = fee_amount_inr

        return {
            "success": True,
            "action": "CREDIT_FEE_WAIVER",
            "amount_credited_inr": fee_amount_inr,
            "timestamp": record["date"],
            "message": f"Courtesy reversal of ₹{fee_amount_inr:.2f} has been approved and credited back to your account balance. Hold released."
        }

    def initiate_dispute(self, customer_id: str, transaction_id: str, reason: str) -> Dict[str, Any]:
        cust = get_customer_by_id(customer_id)
        tier = cust.get("tier", "") if cust else ""
        provisional_hrs = "24 hours (Zero Liability SLA)" if "Premier" in tier else "48 hours (RBI Charter)"

        dispute_record = {
            "dispute_id": f"DSP-RBI-{len(self.disputes) + 7041}",
            "customer_id": customer_id,
            "transaction_id": transaction_id,
            "reason": reason,
            "date_filed": datetime.now(timezone.utc).isoformat(),
            "status": "Provisional Credit Posted per RBI Grievance Policy",
            "timeline": provisional_hrs
        }
        self.disputes.append(dispute_record)

        return {
            "success": True,
            "dispute_id": dispute_record["dispute_id"],
            "status": dispute_record["status"],
            "timeline": provisional_hrs,
            "message": f"Formal dispute grievance {dispute_record['dispute_id']} registered for {transaction_id}. Per RBI Customer Protection Charter, provisional credit will be credited within {provisional_hrs}."
        }

    def expedite_firc_wire_hold(self, customer_id: str, transaction_id: str, purpose_code: str = "P0802") -> Dict[str, Any]:
        self.wire_clearances[transaction_id] = "FIRC Released - Funds Liquid"
        # Mutate account hold: remove hold and credit funds to available balance
        if customer_id == "CUST-IN-101":
            self.account_hold_overrides["ACC-NRE-98214"] = 0.0
            self.account_balance_adjustments["ACC-NRE-98214"] = 430000.0

        return {
            "success": True,
            "action": "FIRC_HOLD_RELEASED",
            "transaction_id": transaction_id,
            "purpose_code": purpose_code,
            "status": "FIRC Released - Funds Liquid",
            "message": f"Electronic FIRC purpose code [{purpose_code} - Software & IT Advisory] verified. Security hold on {transaction_id} ($5,000 USD / ₹4,30,000 INR) released; funds are now liquid in your NRE checking balance."
        }

    # ------------------------------------------------- EXECUTED money actions
    def send_upi(self, customer_id: str, amount_inr: float, payee_vpa: str,
                 note: str = "") -> Dict[str, Any]:
        """EXECUTE an instant NPCI UPI P2P transfer: limit-checked, debits the
        primary INR account, appends a settled ledger row, returns the UTR."""
        cust = self._customer(customer_id)
        if not cust:
            return {"success": False, "action": "UPI_TRANSFER", "error": "Customer account not found"}
        try:
            amount = round(float(amount_inr), 2)
        except (TypeError, ValueError):
            return {"success": False, "action": "UPI_TRANSFER", "error": "Invalid UPI amount"}

        if amount <= 0:
            return {"success": False, "action": "UPI_TRANSFER", "error": "UPI amount must be greater than ₹0"}
        if amount > NPCI_UPI_PER_TXN_LIMIT:
            return {"success": False, "action": "UPI_TRANSFER",
                    "error": f"NPCI per-transaction cap is {NPCI_UPI_PER_TXN_LIMIT:,.0f}. "
                             f"For {amount:,.0f} please use NEFT/RTGS instead."}
        vpa = str(payee_vpa or "").strip()
        if "@" not in vpa or len(vpa) < 3:
            return {"success": False, "action": "UPI_TRANSFER",
                    "error": "A valid payee VPA (name@bank) is required."}
        usage = self._upi_usage_today(customer_id)
        if usage["count"] >= NPCI_UPI_DAILY_MAX_TXNS or \
                usage["total_inr"] + amount > NPCI_UPI_DAILY_LIMIT:
            return {"success": False, "action": "UPI_TRANSFER",
                    "error": f"Daily UPI cap reached ({usage['count']} txns, "
                             f"₹{usage['total_inr']:,.2f} of the ₹{NPCI_UPI_DAILY_LIMIT:,.0f} limit). "
                             "Try again tomorrow or use NEFT/RTGS."}
        acc = self._primary_inr_account(cust)
        if not acc:
            return {"success": False, "action": "UPI_TRANSFER", "error": "No linked INR account found"}
        if self._adjusted_available(cust, acc) < amount:
            return {"success": False, "action": "UPI_TRANSFER",
                    "error": f"Insufficient available balance for ₹{amount:,.2f} "
                             f"(available ₹{self._adjusted_available(cust, acc):,.2f})"}

        # Execute the debit and record the ledger row
        self._debit_account(acc, amount)
        usage["count"] += 1
        usage["total_inr"] = round(usage["total_inr"] + amount, 2)
        self.upi_daily_tracker[customer_id] = usage

        fx = get_all_policies().get("fx_rate_usd_to_inr", USD_INR_RATE)
        utr = self._new_ref("UPI")
        note_txt = f" — {note}" if note else ""
        txn = {
            **self._stamp_txn(customer_id, acc["account_id"]),
            "transaction_id": self._new_ref("TXN-UPI-"),
            "description": f"UPI Payment to {vpa}{note_txt}",
            "category": "UPI P2P Transfer",
            "amount_inr": -amount,
            "amount_usd": -round(amount / fx, 2),
            "status": "Settled",
            "payment_mode": "UPI VPA (Instant)",
            "utr_ref": utr,
            "disputable": True,
            "notes": "Debited instantly via ClientCare. Charge ₹0.00."
        }
        self._log_runtime_txn(customer_id, txn)
        new_usage = self._upi_usage_today(customer_id)
        remaining = round(max(0.0, NPCI_UPI_DAILY_LIMIT - new_usage["total_inr"]), 2)
        new_balance = round(self._adjusted_available(cust, acc), 2)
        return {
            "success": True,
            "action": "UPI_TRANSFER",
            "transaction_id": txn["transaction_id"],
            "utr_ref": utr,
            "amount_inr": amount,
            "amount_display": _inr(amount),
            "payee_vpa": vpa,
            "note": note or None,
            "status": "Settled",
            "debited_from": acc["account_id"],
            "new_available_balance_inr": new_balance,
            "new_available_balance_display": _inr(new_balance),
            "daily_txns_used": new_usage["count"],
            "daily_used_inr": new_usage["total_inr"],
            "daily_remaining_inr": remaining,
            "message": f"UPI transfer of {_inr(amount)} to {vpa} settled instantly. "
                        f"UTR {utr}. Remaining daily UPI quota {_inr(remaining)}."
        }

    def open_fixed_deposit(self, customer_id: str, amount_inr: float,
                           tenure_months: int = 12) -> Dict[str, Any]:
        """EXECUTE an FD booking: debits the funding account, opens a live FD
        deposit account, adds it to the wallet + holdings, returns the certificate."""
        cust = self._customer(customer_id)
        if not cust:
            return {"success": False, "action": "FIXED_DEPOSIT_BOOKED", "error": "Customer account not found"}
        try:
            amount = round(float(amount_inr), 2)
            tenure = int(float(tenure_months))
        except (TypeError, ValueError):
            return {"success": False, "action": "FIXED_DEPOSIT_BOOKED",
                    "error": "FD amount and tenure must be numbers"}
        if amount < FD_MIN_BOOKING_INR:
            return {"success": False, "action": "FIXED_DEPOSIT_BOOKED",
                    "error": f"Minimum FD booking is ₹{FD_MIN_BOOKING_INR:,.0f}"}
        if not 6 <= tenure <= 120:
            return {"success": False, "action": "FIXED_DEPOSIT_BOOKED",
                    "error": "FD tenure must be between 6 and 120 months"}

        acc = self._primary_inr_account(cust)
        if not acc:
            return {"success": False, "action": "FIXED_DEPOSIT_BOOKED", "error": "No linked INR account found"}
        if self._adjusted_available(cust, acc) < amount:
            return {"success": False, "action": "FIXED_DEPOSIT_BOOKED",
                    "error": f"Insufficient available balance for ₹{amount:,.2f} "
                             f"(available ₹{self._adjusted_available(cust, acc):,.2f})"}

        rate = self.fd_rate_for(tenure, cust.get("tier", ""))
        maturity_value = round(amount * ((1 + rate / 100 / 4) ** (tenure / 3)), 2)
        maturity_date = datetime.now(timezone.utc) + timedelta(days=30 * tenure)

        fd_number = self._new_ref("FD")
        fd_account_id = f"ACC-FD-{uuid.uuid4().hex[:5].upper()}"
        fd_account = {
            "account_id": fd_account_id,
            "fd_number": fd_number,
            "type": f"Fixed Deposit — {tenure} months @ {rate}% p.a.",
            "currency": "INR",
            "currency_symbol": "₹",
            "balance": amount,
            "available_balance": amount,
            "hold_amount": 0.0,
            "fd_rate_percent": rate,
            "tenure_months": tenure,
            "maturity_date": maturity_date.strftime("%Y-%m-%d"),
            "maturity_value_inr": maturity_value,
        }
        self.fd_accounts.setdefault(customer_id, []).insert(0, fd_account)

        self._debit_account(acc, amount)
        ticker = f"FD_{tenure}M_{rate}"
        additions = self.investment_additions.setdefault(customer_id, {})
        add = additions.setdefault(ticker, {"asset_class": "Fixed Deposit", "units": 0.0,
                                            "current_value_inr": 0.0})
        add["units"] += 1
        add["current_value_inr"] += amount

        txn = {
            **self._stamp_txn(customer_id, acc["account_id"]),
            "transaction_id": self._new_ref("TXN-FD-"),
            "description": f"Fixed Deposit booked — {tenure} months @ {rate}% p.a.",
            "category": "Deposits & Savings",
            "amount_inr": -amount,
            "amount_usd": -round(amount / get_all_policies().get("fx_rate_usd_to_inr", USD_INR_RATE), 2),
            "status": "Booked — Funds Moved to Deposit",
            "payment_mode": "FD Booking (Digital)",
            "utr_ref": fd_number,
            "disputable": False,
            "notes": f"Maturity {fd_account['maturity_date']}: ₹{maturity_value:,.2f}"
        }
        self._log_runtime_txn(customer_id, txn)
        new_balance = round(self._adjusted_available(cust, acc), 2)
        return {
            "success": True,
            "action": "FIXED_DEPOSIT_BOOKED",
            "fd_number": fd_number,
            "fd_account_id": fd_account_id,
            "amount_inr": amount,
            "amount_display": _inr(amount),
            "tenure_months": tenure,
            "rate_percent": rate,
            "maturity_date": fd_account["maturity_date"],
            "maturity_value_inr": maturity_value,
            "maturity_value_display": _inr(maturity_value),
            "status": "Interest Compounding Quarterly",
            "debited_from": acc["account_id"],
            "new_available_balance_inr": new_balance,
            "new_available_balance_display": _inr(new_balance),
            "message": f"FD of {_inr(amount)} booked for {tenure} months @ {rate}% p.a. "
                        f"A/c {fd_account_id}, certificate {fd_number}. "
                        f"Matures {fd_account['maturity_date']} at {_inr(maturity_value)}."
        }

    def buy_stock(self, customer_id: str, instrument: str,
                  quantity: Optional[float] = None,
                  amount_inr: Optional[float] = None) -> Dict[str, Any]:
        """EXECUTE a stock / index-fund buy: books the order, debits the payer
        account, credits the Demat holding (investment additions) at market price."""
        cust = self._customer(customer_id)
        if not cust:
            return {"success": False, "action": "STOCK_PURCHASED", "error": "Customer account not found"}
        resolved = resolve_instrument(instrument)
        if not resolved:
            return {"success": False, "action": "STOCK_PURCHASED",
                    "error": f"Instrument '{instrument}' is not on the broker board "
                             "(try HDFC Bank, Reliance, TCS, Infosys, ICICI, SBI, ITC, "
                             "Tata Motors, Wipro or a Nifty 50 index fund)"}
        ticker, name = resolved["ticker"], resolved["name"]
        price = float(resolved["price"])
        asset_class = resolved.get("asset_class", "Equity")

        if quantity is None and amount_inr is None:
            return {"success": False, "action": "STOCK_PURCHASED",
                    "error": "Specify either a quantity or an amount to invest"}
        qty = quantity
        if qty is None:
            try:
                qty = float(amount_inr)
            except (TypeError, ValueError):
                return {"success": False, "action": "STOCK_PURCHASED", "error": "Invalid invest amount"}
            qty = (qty // price) if asset_class == "Equity" else round(qty / price, 2)
        if asset_class == "Equity":
            import math               # whole-share purchases only
            qty = math.floor(float(qty))
            if qty <= 0:
                return {"success": False, "action": "STOCK_PURCHASED",
                        "error": f"Whole shares only — at ₹{price} you listed below 1 "
                                 "share; add more funds or pick a cheaper stock"}
        else:
            qty = round(float(qty), 2)
        if qty <= 0:
            return {"success": False, "action": "STOCK_PURCHASED",
                    "error": "Purchase quantity must be greater than zero"}

        cost = round(qty * price, 2)
        acc = self._primary_inr_account(cust)
        if not acc:
            return {"success": False, "action": "STOCK_PURCHASED", "error": "No linked INR account found"}
        if self._adjusted_available(cust, acc) < cost:
            return {"success": False, "action": "STOCK_PURCHASED",
                    "error": f"Insufficient available balance for ₹{cost:,.2f} "
                             f"(available ₹{self._adjusted_available(cust, acc):,.2f})"}

        order_id = self._new_ref("ORD")
        self._debit_account(acc, cost)
        additions = self.investment_additions.setdefault(customer_id, {})
        add = additions.setdefault(ticker, {"asset_class": asset_class, "units": 0.0,
                                            "current_value_inr": 0.0})
        add["units"] += qty
        add["current_value_inr"] += cost

        txn = {
            **self._stamp_txn(customer_id, acc["account_id"]),
            "transaction_id": self._new_ref("TXN-EQ-"),
            "description": f"Buy {qty:g} x {name} ({ticker}) @ ₹{price:,.2f}",
            "category": "Investments & Wealth",
            "amount_inr": -cost,
            "amount_usd": -round(cost / get_all_policies().get("fx_rate_usd_to_inr", USD_INR_RATE), 2),
            "status": "Settled (Demat Credit T+1)",
            "payment_mode": "Apex Demat & Trading",
            "utr_ref": order_id,
            "disputable": False,
            "notes": "Zero brokerage. Market order executed at synthetic board price."
        }
        self._log_runtime_txn(customer_id, txn)

        holdings = next((i for i in self._merge_investments(cust)
                         if str(i.get("ticker")) == ticker), {})
        new_balance = round(self._adjusted_available(cust, acc), 2)
        return {
            "success": True,
            "action": "STOCK_PURCHASED",
            "order_id": order_id,
            "ticker": ticker,
            "name": name,
            "asset_class": asset_class,
            "quantity": qty,
            "price_per_unit_inr": price,
            "price_per_unit_display": _inr(price),
            "total_cost_inr": cost,
            "total_cost_display": _inr(cost),
            "holding_units_after": holdings.get("units"),
            "holding_value_after_inr": holdings.get("current_value_inr"),
            "holding_value_after_display": _inr(holdings.get("current_value_inr", 0)),
            "status": "Settled (Demat Credit T+1)",
            "debited_from": acc["account_id"],
            "new_available_balance_inr": new_balance,
            "new_available_balance_display": _inr(new_balance),
            "message": f"Bought {qty:g} x {name} at {_inr(price)} = {_inr(cost)} "
                        f"(zero brokerage). Order {order_id}, Demat credit T+1. "
                        f"Position now {holdings.get('units')} units, {_inr(holdings.get('current_value_inr', 0))}."
        }

    def convert_currency(self, amount: float, from_curr: str = "USD", to_curr: str = "INR") -> Dict[str, Any]:
        fx_rate = USD_INR_RATE
        if from_curr.upper() == "USD" and to_curr.upper() == "INR":
            converted = amount * fx_rate
            return {
                "input_amount": amount,
                "from_currency": "USD",
                "to_currency": "INR",
                "exchange_rate": fx_rate,
                "converted_amount": round(converted, 2),
                "formatted": f"${amount:,.2f} USD = ₹{converted:,.2f} INR (Zero Markup Interbank Rate @ {fx_rate})"
            }
        elif from_curr.upper() == "INR" and to_curr.upper() == "USD":
            converted = amount / fx_rate
            return {
                "input_amount": amount,
                "from_currency": "INR",
                "to_currency": "USD",
                "exchange_rate": fx_rate,
                "converted_amount": round(converted, 2),
                "formatted": f"₹{amount:,.2f} INR = ${converted:,.2f} USD (Rate: ₹{fx_rate}/USD)"
            }
        return {"error": "Unsupported currency pair"}

    @staticmethod
    def _class_color(category: str) -> str:
        cat = category.lower()
        if "fixed deposit" in cat or "fd" in cat:
            return "#34c274"
        elif "gold" in cat:
            return "#e8a33d"
        elif "mutual" in cat or "index" in cat:
            return "#6366f1"
        elif "equity" in cat or "stock" in cat:
            return "#ff4d00"
        elif "forex" in cat or "usd" in cat:
            return "#06b6d4"
        return "#8a8a93"

    @staticmethod
    def _instrument_name(ticker: str) -> str:
        for t, inst in MARKET_INSTRUMENTS.items():
            if t == ticker or t.lower() == ticker.lower():
                return inst["name"]
        clean = ticker.replace("_", " ").title()
        return clean

    @staticmethod
    def _generate_persona_wealth_insights(customer_id: str, summary: Dict[str, Any]) -> List[Dict[str, str]]:
        if customer_id == "CUST-IN-101":
            return [
                {
                    "title": "NRE Tax Exemption (Sec 10(4))",
                    "tag": "Tax Exemption",
                    "desc": "100% tax-free interest accrual on all NRE savings and high-yield fixed deposits in India under Section 10(4) of the Income Tax Act.",
                    "icon": "fa-shield-halved"
                },
                {
                    "title": "FEMA & LRS Reserve Positioning",
                    "tag": "Compliance",
                    "desc": "Holding $45,000 in Global USD Reserve provides natural currency hedging with zero domestic conversion drag at RBI ref ₹94.11.",
                    "icon": "fa-globe"
                },
                {
                    "title": "Inward Wire Expedited Routing",
                    "tag": "Remittance",
                    "desc": "Automated Purpose Code P0802 declaration eliminates compliance documentation hold for Silicon Valley Bank consulting receipts.",
                    "icon": "fa-bolt"
                }
            ]
        elif customer_id == "CUST-IN-102":
            return [
                {
                    "title": "MSME Working Capital Sweep",
                    "tag": "Treasury",
                    "desc": "Current account collections earn 0% interest. Sweep surplus Soundbox funds into 7.10% liquid FDs to generate daily interest.",
                    "icon": "fa-vault"
                },
                {
                    "title": "Weekend Batch Cashflow Buffer",
                    "tag": "Liquidity",
                    "desc": "Soundbox collections of ₹3,85,000 clear on Monday 06:00 IST. Maintain a ₹1,00,000 instant overdraft buffer for emergency supplier payments.",
                    "icon": "fa-arrows-rotate"
                },
                {
                    "title": "Commercial CIBIL Rating: 748",
                    "tag": "Credit Health",
                    "desc": "Maintains clean vendor payment track. Qualifies for 25 bps concession on commercial term loans and letter of credit issuance.",
                    "icon": "fa-chart-line"
                }
            ]
        else:
            return [
                {
                    "title": "SIP Compounding & Wealth Creation",
                    "tag": "Growth",
                    "desc": "Parag Parikh Flexi Cap Fund is returning +18.6% CAGR. Continuing a disciplined monthly SIP of ₹5,000 builds strong long-term corpus.",
                    "icon": "fa-arrow-trend-up"
                },
                {
                    "title": "Section 80C Tax Savings Optimization",
                    "tag": "Tax Optimization",
                    "desc": "Combining Govt PPF (7.1% tax-free) with ELSS tax-saver mutual funds saves up to ₹46,800 in annual income tax under Section 80C.",
                    "icon": "fa-piggy-bank"
                },
                {
                    "title": "Zero-Markup Travel Forex Shield",
                    "tag": "Forex",
                    "desc": "Multi-currency travel wallet with ₹0 markup on international e-commerce and POS saves 3.5% vs standard commercial credit cards.",
                    "icon": "fa-credit-card"
                }
            ]

    def get_investment_analysis(self, customer_id: str) -> Dict[str, Any]:
        summary = self.get_account_summary(customer_id)
        if "error" in summary:
            return summary

        investments = summary.get("investments", [])
        accounts = summary.get("accounts", [])
        fx_rate = summary.get("fx_rate", USD_INR_RATE)

        total_bank_bal = sum(a.get("balance_inr", 0) for a in accounts if not a.get("type", "").startswith("High-Yield"))
        total_inv_val = sum(float(i.get("current_value_inr", 0) or 0) for i in investments)

        usd_acc = next((a for a in accounts if a.get("currency") == "USD"), None)
        usd_reserves_inr = (usd_acc.get("balance_inr", 0)) if usd_acc else 0.0

        net_worth = total_bank_bal + total_inv_val

        # Asset Allocation breakdown
        by_class: Dict[str, float] = {}
        for i in investments:
            ac = i.get("asset_class", "Other")
            by_class[ac] = by_class.get(ac, 0.0) + float(i.get("current_value_inr", 0) or 0)

        if usd_reserves_inr > 0:
            by_class["Forex USD Reserves"] = usd_reserves_inr

        total_portfolio = sum(by_class.values()) or 1.0
        allocation = [
            {
                "category": cat,
                "value_inr": val,
                "value_usd": round(val / fx_rate, 2),
                "percentage": round((val / total_portfolio) * 100, 1),
                "color": self._class_color(cat)
            }
            for cat, val in sorted(by_class.items(), key=lambda x: -x[1])
        ]

        # Enrich holdings with realistic market metrics
        holdings_detail = []
        for h in investments:
            ac = h.get("asset_class", "Equity")
            cur_val = float(h.get("current_value_inr", 0) or 0)
            units = float(h.get("units", 1) or 1)
            unit_price = round(cur_val / units, 2) if units > 0 else cur_val

            if "Fixed Deposit" in ac:
                unrealized_pct = 7.65 if customer_id == "CUST-IN-101" else (7.10 if customer_id == "CUST-IN-102" else 6.85)
                book_value = round(cur_val / (1 + unrealized_pct / 100), 2)
                unrealized_inr = round(cur_val - book_value, 2)
                cagr = f"{unrealized_pct}% p.a."
                risk = "Guaranteed (DICGC Insured)"
            elif "Gold" in ac:
                unrealized_pct = 14.2
                book_value = round(cur_val / 1.142, 2)
                unrealized_inr = round(cur_val - book_value, 2)
                cagr = "+14.2% (1Y)"
                risk = "Sovereign Gold (RBI Backed)"
            elif "Mutual Fund" in ac or "PPF" in ac:
                unrealized_pct = 18.6
                book_value = round(cur_val / 1.186, 2)
                unrealized_inr = round(cur_val - book_value, 2)
                cagr = "+18.6% CAGR"
                risk = "Moderate (Large-Cap / Hybrid Index)"
            else:
                unrealized_pct = 12.8
                book_value = round(cur_val / 1.128, 2)
                unrealized_inr = round(cur_val - book_value, 2)
                cagr = "+12.8% (YTD)"
                risk = "High Growth (Direct Demat)"

            holdings_detail.append({
                "ticker": h.get("ticker", ac),
                "name": self._instrument_name(h.get("ticker", ac)),
                "asset_class": ac,
                "units": units,
                "unit_price_inr": unit_price,
                "current_value_inr": cur_val,
                "current_value_usd": round(cur_val / fx_rate, 2),
                "book_value_inr": book_value,
                "unrealized_inr": unrealized_inr,
                "unrealized_pct": unrealized_pct,
                "cagr_yield": cagr,
                "risk_profile": risk
            })

        insights = self._generate_persona_wealth_insights(customer_id, summary)

        tradable = [
            {"ticker": k, "name": v["name"], "price": v["price"], "asset_class": v["asset_class"]}
            for k, v in MARKET_INSTRUMENTS.items()
        ]

        rates = [
            {"tenure": "6 Months", "rate": self.fd_rate_for(6, summary.get("tier", "")), "tag": "Short-Term Liquid"},
            {"tenure": "1 Year", "rate": self.fd_rate_for(12, summary.get("tier", "")), "tag": "Standard Growth"},
            {"tenure": "2 Years", "rate": self.fd_rate_for(24, summary.get("tier", "")), "tag": "Compounding"},
            {"tenure": "3-5 Years (Tax Saver)", "rate": self.fd_rate_for(36, summary.get("tier", "")), "tag": "Sec 80C Compliant"}
        ]

        total_book = sum(h["book_value_inr"] for h in holdings_detail) or total_inv_val
        total_gain = sum(h["unrealized_inr"] for h in holdings_detail)
        total_gain_pct = round((total_gain / total_book) * 100, 2) if total_book > 0 else 0.0

        return {
            "customer_id": customer_id,
            "name": summary["name"],
            "tier": summary["tier"],
            "net_worth_inr": round(net_worth, 2),
            "net_worth_usd": round(net_worth / fx_rate, 2),
            "liquid_bank_inr": round(total_bank_bal, 2),
            "investments_inr": round(total_inv_val, 2),
            "usd_reserves_usd": (usd_acc.get("balance_usd", 0)) if usd_acc else 0.0,
            "total_unrealized_gain_inr": round(total_gain, 2),
            "total_unrealized_gain_pct": total_gain_pct,
            "portfolio_xirr": "+14.85% p.a.",
            "diversification_score": 92 if len(allocation) >= 3 else 78,
            "risk_rating": "Moderate Aggressive (Growth Focus)" if customer_id == "CUST-IN-101" else ("Conservative Commercial" if customer_id == "CUST-IN-102" else "Balanced Digital Saver"),
            "allocation": allocation,
            "holdings": holdings_detail,
            "insights": insights,
            "tradable_instruments": tradable,
            "fd_rates": rates,
            "fx_rate": fx_rate
        }

core_banking = CoreBankingService()
