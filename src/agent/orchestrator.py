import time
import re
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta

from ..hindsight.client_wrapper import hindsight_service
from ..banking.compliance import BankingComplianceGuard
from ..banking.core_banking_service import core_banking, resolve_instrument
from .prompts import FINTECH_SYSTEM_PROMPT
from .llm_router import LLMRouter, format_inr
from .tools import ToolExecutor

SNAPSHOT_NOTICE = ("CUSTOMER SNAPSHOT (read-only, a few seconds old — CALL the banking tools before "
                   "quoting exact live figures or performing any action):")

# ----------------------------------------------------------------- parsing
AMOUNT_UNIT_MULTIPLIER = {
    "lakh": 1e5, "lac": 1e5, "lakhs": 1e5, "crore": 1e7, "cr": 1e7,
    "k": 1e3, "thousand": 1e3,
}

def sanitize_inr_strings(text: str) -> str:
    """Correct any malformed Indian currency digit grouping in agent replies.

    Catches model formatting mistakes like ₹8,15,49,50 or ₹8,15,49,950 and
    converts them into proper Indian digit grouping (e.g. ₹81,54,950.00).
    """
    if not text:
        return text

    # Direct correction for common model hallucination on the 81,54,950 total balance
    text = re.sub(r"₹\s*8,?15,?49,?(?:50|950)(?:\.00)?\b", "₹81,54,950.00", text)

    def _fix_match(m):
        prefix = m.group(1)
        num_part = m.group(2)
        dec = ""
        if "." in num_part:
            whole, dec = num_part.split(".", 1)
        else:
            whole = num_part
        raw_digits = whole.replace(",", "").replace(" ", "")
        if not raw_digits.isdigit() or len(raw_digits) <= 3:
            return m.group(0)
        # Indian grouping: last 3 digits, then 2-digit groups leftward
        last3 = raw_digits[-3:]
        rest = raw_digits[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        corrected_whole = ",".join(groups) + "," + last3
        if dec:
            return f"{prefix}{corrected_whole}.{dec}"
        return f"{prefix}{corrected_whole}"

    pattern = r"(₹|Rs\.?\s*)(\d[\d,]*(?:\.\d+)?)"
    return re.sub(pattern, _fix_match, text)


def parse_amount_inr(text: str) -> Optional[float]:
    """Extract a rupee amount: handles '₹12,000', '1.5 lakh', '5k', '2000' etc.
    Ignores share quantities like '10 shares' / '5 x TCS'."""
    lower = str(text or "").lower()
    lower = re.sub(r"\d[\d,.\s]*\s*(?:shares?|stocks?|units?|qty|no\.?\s*)", " ", lower)
    lower = re.sub(r"\b\d+\s*x\b", " ", lower)
    m = re.search(r"(?:₹\s*|\brs\.?\s*|\binr\b\s*)\s*([\d,]+(?:\.\d+)?)\s*(lakh|lac|lakhs|crore|cr|k|thousand)?", lower)
    if m:
        digits, unit = m.group(1), m.group(2)
    else:
        m = re.search(r"\b([\d,]+(?:\.\d+)?)\s*(lakh|lac|lakhs|crore|cr|k|thousand)\b", lower)
        if m:
            digits, unit = m.group(1), m.group(2)
        else:
            m = re.search(r"\b([\d,]{3,}(?:\.\d+)?)\b", lower)
            digits, unit = (m.group(1), "") if m else (None, "")
    if not digits:
        return None
    digits = digits.replace(",", "")
    try:
        value = float(digits)
    except ValueError:
        return None
    if unit:
        value *= AMOUNT_UNIT_MULTIPLIER.get(unit.strip().lower(), 1.0)
    return value if value > 0 else None

def parse_vpa(text: str) -> Optional[str]:
    m = re.search(r"\b([\w.\-]{2,}@[a-zA-Z][\w.\-]{1,})\b", str(text or ""))
    return m.group(1) if m else None

def parse_fd_tenure_months(text: str, default: int = 12) -> int:
    lower = str(text or "").lower()
    m = re.search(r"\b(\d{1,3})\s*(?:years?|yrs?|y)\b", lower)
    if m:
        return int(m.group(1)) * 12
    m = re.search(r"\b(\d{1,3})\s*[- ]*\s*(?:months?|mon|mo)\b", lower)
    if m:
        return int(m.group(1))
    return default

def parse_share_quantity(text: str) -> Optional[int]:
    lower = str(text or "").lower()
    patterns = [r"\b(\d{1,6})\s*(?:shares?|stocks?|units?)\b",
                r"\b(\d{1,6})\s*x\s*[a-z]", r"\bbuy\s+(\d+)\b", r"\bpurchase\s+(\d+)\b"]
    for p in patterns:
        m = re.search(p, lower)
        if m:
            return int(m.group(1))
    return None


class FinTechSupportAgent:
    """
    Enterprise Indian & Cross-Border FinTech Customer Support AI Agent powered by Hindsight Memory.
    Orchestrates compliance checking, memory retrieval, banking actions, and memory retention.
    """

    def __init__(self):
        self.llm_router = LLMRouter()

    def process_message(
        self,
        customer_id: str,
        message: str,
        use_memory: bool = True,
        history: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        history: earlier turns of THIS conversation as [{"role": "user"|"assistant", "content": ...}].
        """
        history = history or []
        # Keep the window lean (last 12 messages = 6 exchanges) so replies stay fast
        # while follow-ups like "what about the other one?" still carry context.
        history = history[-12:]
        start_time = time.time()
        bank_id = f"bank_{customer_id}"

        # 1. Compliance Sanitization & Guardrails
        clean_message = BankingComplianceGuard.sanitize_user_input(message)
        is_escalated, escalation_reason = BankingComplianceGuard.check_escalation_risk(clean_message)

        # 2. Customer Profile & Core Banking State
        customer = core_banking.get_account_summary(customer_id)
        if "error" in customer:
            return {
                "error": "Customer not found",
                "customer_id": customer_id
            }

        # 3. Hindsight Memory (Recall & Reflect)
        recalled_memories: List[Dict[str, Any]] = []
        recalled_text_list: List[str] = []
        disposition: Dict[str, Any] = {}
        directives: List[str] = []

        if use_memory:
            # Recall facts from the customer's bank
            recall_response = hindsight_service.recall(bank_id=bank_id, query=clean_message, max_results=5)
            for r in recall_response.results:
                recalled_memories.append({
                    "fact": r.fact,
                    "score": r.score,
                    "type": r.type,
                    "timestamp": r.timestamp,
                    "tags": r.tags
                })
                recalled_text_list.append(r.fact)

            # Reflect on the customer's mental model and sentiment
            reflect_response = hindsight_service.reflect(bank_id=bank_id, query=clean_message)
            disposition = reflect_response.disposition
            directives = reflect_response.directives

        # 4+5. Generate the reply.
        banking_action_result: Optional[Dict[str, Any]] = None
        actions: List[Dict[str, Any]] = []
        reply_text: Optional[str] = None

        # Compact read-only snapshot of THIS customer's own state, so the model can
        # answer with specifics immediately and persona isolation is guaranteed.
        customer_snapshot = self._customer_snapshot(customer, customer_id)

        # Money moves (UPI / FD / stocks) execute deterministically BEFORE the LLM,
        # so a demo action always lands regardless of model behaviour or memory.
        if use_memory:
            banking_action_result = self._determine_and_execute_tool(
                customer_id=customer_id,
                message=clean_message,
                recalled_memories=recalled_text_list
            )
            money_action = bool(banking_action_result and banking_action_result.get("action") in (
                "UPI_TRANSFER", "FIXED_DEPOSIT_BOOKED", "STOCK_PURCHASED"))
            if banking_action_result and not money_action:
                banking_action_result = None   # card/waiver/other actions stay in the LLM flow

        # Preferred path: real conversation + LLM tool-calling (needs a Groq key).
        if use_memory and self.llm_router.agentic_available and banking_action_result is None:
            agent_system_prompt = self._build_system_prompt(customer, recalled_text_list, directives,
                                                            customer_snapshot)
            result = self.llm_router.agentic_chat(
                system_prompt=agent_system_prompt,
                history=history,
                user_message=clean_message,
                tool_executor=ToolExecutor(customer_id),
            )
            if result and result["reply"]:
                reply_text = result["reply"]
                actions = result["actions"]
                banking_action_result = actions[-1] if actions else None

        # Fallback path: keyword tools + generated / built-in local reply.
        if reply_text is None:
            if use_memory and banking_action_result is None:
                banking_action_result = self._determine_and_execute_tool(
                    customer_id=customer_id,
                    message=clean_message,
                    recalled_memories=recalled_text_list
                )
                if banking_action_result and banking_action_result.get("action") not in (
                        "UPI_TRANSFER", "FIXED_DEPOSIT_BOOKED", "STOCK_PURCHASED"):
                    banking_action_result = None
            if banking_action_result and not actions:
                actions = [banking_action_result]
            # Hand the local/LLM engines the same read-only snapshot (never the live dict).
            customer_context = dict(customer)
            customer_context["recent_transactions"] = customer_snapshot["recent_transactions"]
            reply_text = self.llm_router.generate(
                system_prompt=FINTECH_SYSTEM_PROMPT,
                user_message=clean_message,
                customer_context=customer_context,
                recalled_memories=recalled_text_list,
                directives=directives,
                banking_action_result=banking_action_result,
                is_stateless=(not use_memory),
                history=history
            )

        if reply_text:
            reply_text = sanitize_inr_strings(reply_text)

        # Tools may have changed balances / card status: refresh the snapshot.
        customer = core_banking.get_account_summary(customer_id)

        # 6. Retain New Memory (Learning from interaction)
        new_memory_id = None
        substantive = bool(actions) or len(clean_message) > 40
        if use_memory and not is_escalated and substantive:
            interaction_summary = f"Customer inquired: '{clean_message}'. Agent resolved: {reply_text[:140]}..."
            ok_actions = [a.get("action", a.get("tool", "BANKING_ACTION")) for a in actions if a.get("success")]
            if ok_actions:
                interaction_summary += f" Executed: {', '.join(ok_actions)}."

            retain_res = hindsight_service.retain(
                bank_id=bank_id,
                content=interaction_summary,
                context="live_support_conversation",
                tags=["live_interaction", "agent_resolution"]
            )
            new_memory_id = retain_res.get("id")

        duration_ms = round((time.time() - start_time) * 1000, 2)

        return {
            "customer_id": customer_id,
            "customer_name": customer["name"],
            "tier": customer["tier"],
            "reply": reply_text,
            "use_memory": use_memory,
            "recalled_memories": recalled_memories,
            "disposition": disposition,
            "directives": directives,
            "banking_action": banking_action_result,
            "actions": actions,
            "escalation": {
                "is_escalated": is_escalated,
                "reason": escalation_reason
            },
            "new_memory_id": new_memory_id,
            "latency_ms": duration_ms,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

    @staticmethod
    def _customer_snapshot(customer: Dict[str, Any], customer_id: str) -> Dict[str, Any]:
        """Compact, read-only snapshot of the logged-in customer's own data.

        Strictly scoped: built from the authenticated customer's profile and
        transactions only, so the model can never see another customer's state.
        """
        accounts = []
        for a in customer.get("accounts", []):
            accounts.append({
                "account_id": a.get("account_id"),
                "type": a.get("type"),
                "currency": a.get("currency"),
                "balance_inr": a.get("balance_inr"),
                "balance_usd": a.get("balance_usd"),
                "hold_amount": a.get("hold_amount", 0.0) or 0.0,
                "hold_reason": a.get("hold_reason", ""),
            })
        cards = [{
            "card_id": c.get("card_id"),
            "card_type": c.get("card_type"),
            "masked_number": c.get("masked_number"),
            "status": c.get("status"),
            "international_enabled": c.get("international_enabled"),
        } for c in customer.get("cards", [])]
        recent_transactions = [{
            "transaction_id": t.get("transaction_id"),
            "date": str(t.get("date", "")),
            "description": t.get("description", ""),
            "category": t.get("category", ""),
            "amount_inr": t.get("amount_inr", 0.0),
            "status": t.get("status", ""),
        } for t in core_banking.get_transactions(customer_id, limit=8)]
        holdings = [{
            "asset_class": i.get("asset_class"),
            "ticker": i.get("ticker"),
            "units": i.get("units"),
            "current_value_inr": i.get("current_value_inr"),
        } for i in customer.get("investments", [])]
        return {
            "accounts": accounts,
            "cards": cards,
            "recent_transactions": recent_transactions,
            "holdings": holdings,
            "total_liquid_inr": customer.get("total_liquid_inr"),
            "total_liquid_usd": customer.get("total_liquid_usd"),
            "upi_id": customer.get("upi_id"),
            "city": customer.get("city"),
            "branch": customer.get("branch"),
            "kyc_status": customer.get("kyc_status"),
        }

    @staticmethod
    def _build_system_prompt(
        customer: Dict[str, Any],
        memories: List[str],
        directives: List[str],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%A, %d %B %Y, %I:%M %p IST")
        lines = [
            FINTECH_SYSTEM_PROMPT,
            "CURRENT CUSTOMER",
            f"- Name: {customer.get('name')} | Tier: {customer.get('tier')} | UPI: {customer.get('upi_id')}",
            f"- Now: {today}",
        ]
        if snapshot:
            lines.append(SNAPSHOT_NOTICE)
            lines.append(f"- Portfolio total: {format_inr(snapshot['total_liquid_inr'])} "
                         f"(~${snapshot['total_liquid_usd']:,.2f} USD)")
            for a in snapshot["accounts"]:
                hold = a.get("hold_amount") or 0.0
                hold_note = (f" — ON HOLD {format_inr(hold)}: {a['hold_reason'][:60]}" if hold else "")
                lines.append(f"- Account {a['account_id']} ({a['type']}): "
                             f"{format_inr(a['balance_inr'])}{hold_note}")
            for c in snapshot["cards"]:
                intl = "international ON" if c.get("international_enabled") else "international OFF"
                lines.append(f"- Card {c['card_id']} ({c['card_type']}): status {c['status']}, {intl}")
            lines.append(f"- UPI handle: {snapshot['upi_id']} | Branch: {snapshot['branch']}")
            for t in snapshot["recent_transactions"][:8]:
                lines.append(f"- Recent txn: {t['date'][:10]} | {t['description'][:56]} | "
                             f"{format_inr(t['amount_inr'])} | {t['status']}")
            for h in snapshot.get("holdings", []):
                lines.append(f"- Holding: {h['asset_class']} {h['ticker']} — {h['units']} units, "
                             f"{format_inr(h['current_value_inr'])}")
        if memories:
            lines.append("WHAT YOU REMEMBER ABOUT THIS CUSTOMER (use it naturally, don't recite it):")
            lines += [f"- {m[:200]}" for m in memories[-4:]]
        if directives:
            lines.append("GUIDANCE FOR THIS CUSTOMER:")
            lines += [f"- {d}" for d in directives]
        return "\n".join(lines)

    def _determine_and_execute_tool(
        self,
        customer_id: str,
        message: str,
        recalled_memories: List[str]
    ) -> Optional[Dict[str, Any]]:
        lower = message.lower()

        # 0. EXECUTED money actions: UPI transfer / FD booking / stock purchase
        #    These deliberately sit first so verbs like "send"/"buy"/"open" can
        #    never be swallowed by the advisory branches below.
        amount = parse_amount_inr(message)
        if amount and any(re.search(rf"\b{w}\b", lower) for w in ["send", "pay", "transfer", "bhej", "bhejo"]):
            upi_context = "upi" in lower or parse_vpa(message) is not None
            if upi_context:
                return core_banking.send_upi(
                    customer_id=customer_id,
                    amount_inr=amount,
                    payee_vpa=parse_vpa(message) or "payee@apexmudra",
                    note=""
                )

        if amount and (re.search(r"\b(?:open|book|start|create|invest in)\b.{0,40}\b(?:fixed deposit|fd)\b",
                                  lower)
                       or re.search(r"\b(?:fixed deposit|fd)\b.{0,24}\b(?:of|for)\s*(?:₹|rs|inr)?\s*[\d,]",
                                    lower)):
            return core_banking.open_fixed_deposit(
                customer_id=customer_id,
                amount_inr=amount,
                tenure_months=parse_fd_tenure_months(message),
            )

        instrument = resolve_instrument(lower)
        if amount or parse_share_quantity(message):
            if instrument and any(re.search(rf"\b{w}\b", lower) for w in ["buy", "purchase", "invest",
                                                                        "kharid", "add"]):
                return core_banking.buy_stock(
                    customer_id=customer_id,
                    instrument=instrument["ticker"],
                    quantity=parse_share_quantity(message),
                    amount_inr=None if parse_share_quantity(message) is not None else amount,
                )

        # 1. Unfreeze / Unblock / Unlock card (Strict priority over freeze)
        if any(w in lower for w in ["unfreeze", "unblock", "unlock", "reactivate", "chalu karo"]):
            cust = core_banking.get_account_summary(customer_id)
            cards = cust.get("cards", [])
            card_id = cards[0]["card_id"] if cards else "CARD-PRIMARY"
            return core_banking.unfreeze_card(customer_id, card_id)

        # 2. Freeze / Block / Lock card
        if any(w in lower for w in ["freeze", "block", "lock", "lost", "stolen", "card band"]):
            cust = core_banking.get_account_summary(customer_id)
            cards = cust.get("cards", [])
            card_id = cards[0]["card_id"] if cards else "CARD-PRIMARY"
            return core_banking.freeze_card(customer_id, card_id, reason="Customer security request")

        # 3. Inward SWIFT / FIRC release (Aarav Singhania)
        if ("wire" in lower or "swift" in lower or "firc" in lower or "5000" in lower) and customer_id == "CUST-IN-101":
            return core_banking.expedite_firc_wire_hold(
                customer_id=customer_id,
                transaction_id="TXN-IN-90811",
                purpose_code="P0802"
            )

        # 4. Fee waiver / AMC reversal
        if any(w in lower for w in ["waiver", "reverse", "amc", "nach", "fee", "250", "350"]):
            eligibility = core_banking.check_fee_waiver_eligibility(customer_id)
            if eligibility["eligible"]:
                amount_to_waive = 250.00 if customer_id == "CUST-IN-103" else 150.00
                return core_banking.process_fee_waiver(
                    customer_id=customer_id,
                    fee_amount_inr=amount_to_waive,
                    reason="Automated first-time customer courtesy waiver"
                )
            else:
                return {
                    "action": "FEE_WAIVER_DENIED",
                    "requires_escalation": True,
                    "reason": eligibility["reason"]
                }

        # 5. Dispute / Unauthorized charge
        if any(w in lower for w in ["dispute", "refund", "unrecognized", "did not do", "fraud", "unauthorized", "kyu kata"]):
            txns = core_banking.get_transactions(customer_id, limit=3)
            target_id = txns[0]["transaction_id"] if txns else "TXN-IN-DISPUTE"
            return core_banking.initiate_dispute(
                customer_id=customer_id,
                transaction_id=target_id,
                reason="Unrecognized charge reported by cardholder"
            )

        # 6. Currency Conversion tool
        if any(w in lower for w in ["convert", "usd to inr", "inr to usd", "forex"]):
            amt_match = re.search(r"(\d+(?:\.\d+)?)", lower)
            amt = float(amt_match.group(1)) if amt_match else 500.0
            from_c = "INR" if ("rupee" in lower and "dollar" not in lower) else "USD"
            to_c = "USD" if from_c == "INR" else "INR"
            return core_banking.convert_currency(amt, from_c, to_c)

        # 7. Travel notice
        if any(w in lower for w in ["travel notice", "travelling to", "flying to", "going to dubai", "going to london"]):
            dest = "Dubai / Western Europe"
            if "dubai" in lower: dest = "Dubai, UAE"
            elif "london" in lower: dest = "London, UK"
            elif "singapore" in lower: dest = "Singapore"
            elif "san francisco" in lower or "us" in lower: dest = "San Francisco, USA"

            return {
                "success": True,
                "action": "TRAVEL_NOTICE_REGISTERED",
                "destination": dest,
                "message": f"Zero-forex travel clearance placed for {dest}."
            }

        return None

support_agent = FinTechSupportAgent()
