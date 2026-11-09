"""Banking tools the chatbot can call. Every tool is bound to ONE authenticated
customer, so the model can never act on (or see) another customer's data."""

import json
from typing import Any, Dict, List

from ..banking.core_banking_service import core_banking, USD_INR_RATE

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "get_account_summary",
        "description": "Get the customer's live accounts, balances, holds, total portfolio value and registered cards.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_recent_transactions",
        "description": "List the customer's most recent transactions with IDs, amounts, status and UTR reference.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "How many to return (1-10)", "default": 5}}}}},
    {"type": "function", "function": {
        "name": "block_card",
        "description": "Immediately freeze/block the customer's card (lost, stolen, or customer request).",
        "parameters": {"type": "object", "properties": {
            "card_id": {"type": "string", "description": "Optional card_id. Defaults to the primary card."}}}}},
    {"type": "function", "function": {
        "name": "unblock_card",
        "description": "Reactivate / unblock the customer's frozen card.",
        "parameters": {"type": "object", "properties": {
            "card_id": {"type": "string", "description": "Optional card_id. Defaults to the primary card."}}}}},
    {"type": "function", "function": {
        "name": "request_fee_waiver",
        "description": "Check policy POL-RBI-FEE-001 and, if eligible, credit a courtesy fee waiver. If not eligible, returns the reason and that escalation is needed.",
        "parameters": {"type": "object", "properties": {
            "fee_amount_inr": {"type": "number", "description": "Fee amount in INR"},
            "fee_type": {"type": "string", "description": "e.g. NACH return fee, card AMC"}},
            "required": ["fee_amount_inr"]}}},
    {"type": "function", "function": {
        "name": "raise_dispute",
        "description": "File a formal RBI zero-liability dispute for an unauthorized or unrecognised transaction.",
        "parameters": {"type": "object", "properties": {
            "transaction_id": {"type": "string", "description": "Transaction ID from get_recent_transactions"},
            "reason": {"type": "string"}},
            "required": ["transaction_id", "reason"]}}},
    {"type": "function", "function": {
        "name": "release_swift_hold",
        "description": "Release the FIRC compliance hold on an inward SWIFT wire using the customer's standing purpose code.",
        "parameters": {"type": "object", "properties": {
            "transaction_id": {"type": "string"},
            "purpose_code": {"type": "string", "description": "RBI purpose code, e.g. P0802"}},
            "required": ["transaction_id"]}}},
    {"type": "function", "function": {
        "name": "convert_currency",
        "description": "Convert between USD and INR at the bank's interbank rate.",
        "parameters": {"type": "object", "properties": {
            "amount": {"type": "number"},
            "from_currency": {"type": "string", "enum": ["USD", "INR"]},
            "to_currency": {"type": "string", "enum": ["USD", "INR"]}},
            "required": ["amount", "from_currency", "to_currency"]}}},
    {"type": "function", "function": {
        "name": "register_travel_notice",
        "description": "Register an international travel notice so the card is not declined abroad.",
        "parameters": {"type": "object", "properties": {
            "destination": {"type": "string"}},
            "required": ["destination"]}}},
    {"type": "function", "function": {
        "name": "send_upi_money",
        "description": "EXECUTE an instant UPI P2P transfer; debits the account and returns a UTR. "
                       "Caps: ₹1,00,000/txn, ₹2,00,000 & 20/day.",
        "parameters": {"type": "object", "properties": {
            "amount_inr": {"type": "number", "description": "Amount to send in INR"},
            "payee_vpa": {"type": "string", "description": "Payee UPI address, e.g. name@apexmudra"},
            "note": {"type": "string", "description": "Optional remark"}},
            "required": ["amount_inr", "payee_vpa"]}}},
    {"type": "function", "function": {
        "name": "open_fixed_deposit",
        "description": "EXECUTE an FD booking from the customer's account at the POL-RATES-FD-009 "
                       "rate card (6.75-7.65% p.a., +0.50% seniors).",
        "parameters": {"type": "object", "properties": {
            "amount_inr": {"type": "number", "description": "Amount in INR (min ₹1,000)"},
            "tenure_months": {"type": "integer", "description": "6-120 months; default 12"}},
            "required": ["amount_inr"]}}},
    {"type": "function", "function": {
        "name": "buy_stocks",
        "description": "EXECUTE an equity/mutual-fund purchase at ₹0 brokerage. Pass quantity or "
                       "amount to invest. Board: HDFC Bank, Reliance, TCS, Infosys, ICICI, SBI, "
                       "ITC, Tata Motors, Wipro, Nifty 50 index fund.",
        "parameters": {"type": "object", "properties": {
            "instrument": {"type": "string", "description": "Name or ticker, e.g. 'HDFC Bank' or TCS.NS"},
            "quantity": {"type": "number", "description": "Whole shares (equities)"},
            "amount_inr": {"type": "number", "description": "INR to invest if no quantity given"}},
            "required": ["instrument"]}}},
]


class ToolExecutor:
    def __init__(self, customer_id: str):
        self.customer_id = customer_id

    # ------------------------------------------------------------ helpers
    def _cards(self) -> List[Dict[str, Any]]:
        return core_banking.get_account_summary(self.customer_id).get("cards", [])

    def _resolve_card(self, card_id: str = None):
        cards = self._cards()
        if not cards:
            return None
        if card_id:
            for c in cards:
                if c["card_id"].lower() == card_id.lower():
                    return c["card_id"]
        return cards[0]["card_id"]

    # -------------------------------------------------------------- run
    def run(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            fn = getattr(self, f"_t_{name}", None)
            if fn is None:
                return {"success": False, "error": f"Unknown tool {name}"}
            return fn(**(args or {}))
        except TypeError as e:
            return {"success": False, "error": f"Bad arguments: {e}"}
        except Exception as e:  # never let a tool crash the chat
            return {"success": False, "error": str(e)}

    def _t_get_account_summary(self):
        s = core_banking.get_account_summary(self.customer_id)
        return {k: s[k] for k in ("name", "tier", "upi_id", "branch", "ifsc", "accounts",
                                   "total_liquid_inr", "total_liquid_usd", "cards", "investments",
                                   "financials", "fx_rate")}

    def _t_get_recent_transactions(self, limit: int = 5):
        limit = max(1, min(int(limit), 10))
        return {"transactions": core_banking.get_transactions(self.customer_id, limit=limit)}

    def _t_block_card(self, card_id: str = None):
        cid = self._resolve_card(card_id)
        if not cid:
            return {"success": False, "error": "No card on file"}
        r = core_banking.freeze_card(self.customer_id, cid, reason="Customer security request")
        r["action"] = "FREEZE_CARD"
        return r

    def _t_unblock_card(self, card_id: str = None):
        cid = self._resolve_card(card_id)
        if not cid:
            return {"success": False, "error": "No card on file"}
        return core_banking.unfreeze_card(self.customer_id, cid)

    def _t_request_fee_waiver(self, fee_amount_inr: float, fee_type: str = "fee"):
        elig = core_banking.check_fee_waiver_eligibility(self.customer_id)
        if not elig["eligible"]:
            return {"success": False, "action": "FEE_WAIVER_DENIED", "requires_escalation": True,
                    "reason": elig["reason"]}
        return core_banking.process_fee_waiver(
            self.customer_id, float(fee_amount_inr), f"Courtesy waiver: {fee_type}")

    def _t_raise_dispute(self, transaction_id: str, reason: str):
        mine = {t["transaction_id"] for t in core_banking.get_transactions(self.customer_id, limit=50)}
        if transaction_id not in mine:
            return {"success": False, "error": "That transaction ID is not on this customer's account."}
        return core_banking.initiate_dispute(self.customer_id, transaction_id, reason)

    def _t_release_swift_hold(self, transaction_id: str, purpose_code: str = "P0802"):
        txns = {t["transaction_id"]: t for t in core_banking.get_transactions(self.customer_id, limit=50)}
        t = txns.get(transaction_id)
        if not t or "Hold" not in t.get("status", "") or self.customer_id != "CUST-IN-101":
            return {"success": False,
                    "error": "No inward wire under compliance hold found for this customer/transaction."}
        return core_banking.expedite_firc_wire_hold(self.customer_id, transaction_id, purpose_code)

    def _t_convert_currency(self, amount: float, from_currency: str, to_currency: str):
        return core_banking.convert_currency(float(amount), from_currency, to_currency)

    def _t_register_travel_notice(self, destination: str):
        return {"success": True, "action": "TRAVEL_NOTICE_REGISTERED", "destination": destination,
                "message": f"Travel notice registered for {destination}. Card whitelisted for POS/ATM there."}

    def _t_send_upi_money(self, amount_inr: float, payee_vpa: str, note: str = ""):
        return core_banking.send_upi(
            self.customer_id, float(amount_inr), str(payee_vpa), str(note or ""))

    def _t_open_fixed_deposit(self, amount_inr: float, tenure_months: int = 12):
        return core_banking.open_fixed_deposit(self.customer_id, float(amount_inr), int(tenure_months))

    def _t_buy_stocks(self, instrument: str, quantity: float = None, amount_inr: float = None):
        return core_banking.buy_stock(
            self.customer_id, str(instrument),
            quantity=quantity, amount_inr=amount_inr)


def result_to_json(result: Dict[str, Any]) -> str:
    return json.dumps(result, default=str, ensure_ascii=False)
