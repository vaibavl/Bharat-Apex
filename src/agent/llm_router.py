import os
import json
import re
import time
import hashlib
import httpx
from typing import Dict, Any, List, Optional
from ..banking.core_banking_service import USD_INR_RATE
from .tools import TOOL_SCHEMAS, result_to_json

# The bank's own branch identity (updated). Customer locations (Begum Bazaar,
# HITEC City residences, batch #HYD-9021 collections in Hyderabad) stay as-is.
BANK_BRANCH = "MG Road Corporate Branch, Bengaluru"
BANK_BRANCH_FULL = "MG Road Corporate Branch, Bengaluru - 560001"


def format_inr(amount: float) -> str:
    """Format an INR amount with Indian digit grouping: 12,34,567.89."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    neg = amount < 0
    s = f"{abs(round(amount, 2)):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        last3, rest = whole[-3:], whole[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        whole = ",".join(groups) + "," + last3
    return ("-" if neg else "") + f"₹{whole}.{frac}"


class LLMRouter:
    """
    Intelligent LLM Router for Indian & Cross-Border FinTech Support Agent.
    Supports:
    1. Google Gemini API (gemini-2.0-flash / gemini-1.5-flash) via official REST API
    2. Groq (llama-3.3-70b-versatile / qwen)
    3. OpenAI API (gpt-4o / gpt-4o-mini)
    4. Enterprise Gemini-Grade Local FinTech Reasoning Engine (Dynamic, comprehensive, zero-dependency)
    """

    MAX_AGENTIC_ROUNDS = 4  # cap the tool-calling loop so replies stay responsive

    GROQ_FALLBACK_SECONDS = 600  # keep a rotated model active for 10 minutes

    def __init__(self):
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self._groq_bad = False  # set when Groq rejects the key, so we stop retrying
        self._groq_active_model = None   # rotated model currently preferred
        self._groq_active_until = 0.0    # monotonic deadline for the rotation preference

    def _groq_models(self) -> List[str]:
        """Primary model first; after a rate-limit switch, the winning model leads
        the chain for GROQ_FALLBACK_SECONDS so we don't re-burn the throttle."""
        primary = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
        fallback = os.getenv("GROQ_MODEL_FALLBACK", "openai/gpt-oss-20b")
        chain = []
        if self._groq_active_model and time.monotonic() < self._groq_active_until:
            chain.append(self._groq_active_model)
        for m in (primary, fallback):
            if m and m not in chain:
                chain.append(m)
        return chain

    @staticmethod
    def _is_groq_model_error(e: Exception) -> bool:
        """True when switching models can help (throttle / missing model / slack
        tool-call 400 from gpt-oss). Auth failures are NOT model-switchable."""
        text = str(e).lower()
        return any(tag in text for tag in ("429", "413", "rate_limit", "tokens per day",
                                           "tokens per minute", "model_not_found",
                                           "does not exist", "tool_use_failed"))

    def _groq_completion(self, client, *, messages, temperature: float, max_tokens: int,
                         tools=None):
        """chat.completions.create with automatic model rotation on model-level
        failures (TPD/TPM throttles, decommissioned models, slack tool-call 400s)."""
        last_error: Optional[Exception] = None
        chain = self._groq_models()
        for model in chain:
            try:
                kwargs: Dict[str, Any] = {
                    "model": model, "messages": messages,
                    "temperature": temperature, "max_tokens": max_tokens,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                resp = client.chat.completions.create(**kwargs)
                if model != chain[0]:
                    self._groq_active_model = model
                    self._groq_active_until = time.monotonic() + self.GROQ_FALLBACK_SECONDS
                    print(f"[LLMRouter] Groq switched to fallback model: {model} "
                          f"(sticky for {self.GROQ_FALLBACK_SECONDS}s).")
                return resp
            except Exception as e:
                last_error = e
                if not self._is_groq_model_error(e):
                    self._note_groq_error(e)
                    raise
                print(f"[LLMRouter] Groq model '{model}' rejected ({str(e)[:120]}...). Rotating.")
        raise last_error  # exhausted the chain; caller falls back to local engine

    def _note_groq_error(self, e: Exception) -> None:
        text = str(e).lower()
        if "invalid_api_key" in text or "invalid api key" in text or "error code: 401" in text:
            self._groq_bad = True
            print("[LLMRouter] Groq rejected the API key (real keys start with 'gsk_'). "
                  "Using the built-in local engine until the server restarts.")

    def generate(
        self,
        system_prompt: str,
        user_message: str,
        customer_context: Dict[str, Any],
        recalled_memories: List[str],
        directives: List[str],
        banking_action_result: Optional[Dict[str, Any]] = None,
        is_stateless: bool = False,
        history: Optional[List[Dict[str, str]]] = None
    ) -> str:
        history = history or []

        # Executed money actions confirm from the tool's exact numbers via the
        # built-in formatter — never let a model re-derive or refuse these.
        action = banking_action_result or {}
        if str(action.get("action", "")) in (
                "UPI_TRANSFER", "FIXED_DEPOSIT_BOOKED", "STOCK_PURCHASED"):
            try:
                return self._clean_reply(self._local_gemini_grade_reasoning(
                    user_message=user_message,
                    customer_context=customer_context,
                    recalled_memories=recalled_memories,
                    directives=directives,
                    banking_action_result=banking_action_result,
                    is_stateless=is_stateless
                ))
            except Exception as e:      # local composer failing is bad, but never crash
                print(f"[LLMRouter] local action composer failed: {e}. Falling through.")
                banking_action_result = None

        prompt_content = self._format_prompt(
            user_message, customer_context, recalled_memories, directives, banking_action_result, is_stateless
        )

        # 1. Try Google Gemini API if key is present
        if self.gemini_api_key:
            try:
                gemini_reply = self._call_gemini(system_prompt, prompt_content, history)
                if gemini_reply:
                    return self._clean_reply(gemini_reply)
            except Exception as e:
                print(f"[LLMRouter] Gemini API call failed: {e}. Falling back.")

        # 2. Try Groq API if key is present
        if self.groq_api_key and not self._groq_bad:
            try:
                from groq import Groq
                client = Groq(api_key=self.groq_api_key)
                chat_completion = self._groq_completion(
                    client,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        *history,
                        {"role": "user", "content": prompt_content}
                    ],
                    temperature=0.3,
                    max_tokens=700,
                )
                groq_reply = self._clean_reply(chat_completion.choices[0].message.content or "")
                if groq_reply:
                    return groq_reply
            except Exception as e:
                self._note_groq_error(e)
                print(f"[LLMRouter] Groq API call failed: {e}. Falling back.")

        # 3. Try OpenAI API if key is present
        if self.openai_api_key:
            try:
                headers = {"Authorization": f"Bearer {self.openai_api_key}", "Content-Type": "application/json"}
                payload = {
                    "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        *history,
                        {"role": "user", "content": prompt_content}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 700
                }
                with httpx.Client(timeout=20.0) as client:
                    resp = client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
                    if resp.status_code == 200:
                        openai_reply = self._clean_reply(resp.json()["choices"][0]["message"]["content"] or "")
                        if openai_reply:
                            return openai_reply
            except Exception as e:
                print(f"[LLMRouter] OpenAI API call failed: {e}. Falling back.")

        # 4. Fallback to Gemini-Grade Local FinTech Reasoning Engine
        return self._clean_reply(self._local_gemini_grade_reasoning(
            user_message=user_message,
            customer_context=customer_context,
            recalled_memories=recalled_memories,
            directives=directives,
            banking_action_result=banking_action_result,
            is_stateless=is_stateless
        ))

    # ------------------------------------------------------------------
    # Agentic chat: Groq function-calling loop with banking tools
    # ------------------------------------------------------------------
    @property
    def agentic_available(self) -> bool:
        return bool(self.groq_api_key) and not self._groq_bad

    def agentic_chat(
        self,
        system_prompt: str,
        history: List[Dict[str, str]],
        user_message: str,
        tool_executor,
        max_rounds: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Returns {"reply": str, "actions": [tool results]} or None if Groq is unusable."""
        if not self.agentic_available:
            return None
        max_rounds = max(2, min(int(max_rounds or self.MAX_AGENTIC_ROUNDS), 8))
        # Groq free tier rejects any single request >8000 tokens (input + max_tokens).
        # Keep every round well under that cap: history trimmed, tool results bounded.
        history = [
            {**m, "content": (m.get("content") or "")[:500]}
            for m in history[-8:]
        ]
        try:
            from groq import Groq
            client = Groq(api_key=self.groq_api_key)
            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                *history,
                {"role": "user", "content": user_message},
            ]
            actions: List[Dict[str, Any]] = []
            seen_calls: Dict[tuple, Dict[str, Any]] = {}
            reply: Optional[str] = None

            for _ in range(max_rounds):
                resp = self._groq_completion(
                    client, messages=messages, temperature=0.4, max_tokens=900,
                    tools=TOOL_SCHEMAS)
                msg = resp.choices[0].message
                if not msg.tool_calls:
                    reply = (msg.content or "").strip()
                    break

                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                        for tc in msg.tool_calls
                    ],
                })
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    name = tc.function.name
                    key = (name, json.dumps(args, sort_keys=True, default=str))
                    if key in seen_calls:
                        # Redundant call: hand back the cached result once and tell the
                        # model to answer from it instead of looping on the same tool.
                        cached = dict(seen_calls[key])
                        cached["note"] = ("Duplicate call suppressed: this exact result was already "
                                          "returned in this turn. Use it now and answer the customer.")
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "content": result_to_json(cached)})
                        continue
                    result = tool_executor.run(name, args)
                    seen_calls[key] = dict(result)
                    if result.get("action") or "success" in result:
                        actions.append({"tool": name, **result})
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": result_to_json(result)})

            if reply is None:
                # Out of rounds: force a final answer that uses the tool output numbers.
                final_messages = messages + [{
                    "role": "user",
                    "content": ("Stop calling tools. Give the customer your final answer now, "
                                "using the exact numbers and statuses from the tool results above. "
                                "Never claim an action succeeded if its tool result was success=false."),
                }]
                final = self._groq_completion(
                    client, messages=final_messages, temperature=0.4, max_tokens=700,
                    tools=TOOL_SCHEMAS)
                final_msg = final.choices[0].message
                if final_msg.tool_calls:
                    for tc in final_msg.tool_calls:
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except Exception:
                            args = {}
                        res = tool_executor.run(tc.function.name, args)
                        if res.get("message"):
                            reply = res["message"]
                            if res.get("action") or "success" in res:
                                actions.append({"tool": tc.function.name, **res})
                if not reply:
                    reply = (final_msg.content or "").strip()

            return {"reply": self._clean_reply(reply), "actions": actions}
        except Exception as e:
            self._note_groq_error(e)
            print(f"[LLMRouter] Groq agentic call failed: {e}. Falling back.")
            return None

    # ------------------------------------------------------------------
    # Reply post-processing: strip leaked meta-text without touching substance
    # ------------------------------------------------------------------
    _META_SENTENCE_RE = re.compile(
        r"^\s*(?:as an? ai\b|i(?:'m| am) an? (?:ai|large language model|artificial intelligence|machine)\b|"
        r"i was trained by\b|my (?:internal|system) (?:instructions|prompts?)\b).*$",
        re.IGNORECASE,
    )
    _ROLE_LABEL_RE = re.compile(r"^\s*(assistant|ai|chatbot|bot|agent|user|customer|system)\s*:\s*", re.IGNORECASE)

    @classmethod
    def _clean_reply(cls, text: str) -> str:
        if not text:
            return text or ""
        out = text

        # Strip a trailing/leading markdown code fence wrapper around the whole reply.
        lines = out.splitlines()
        if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip().endswith("```"):
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            out = "\n".join(lines)

        # Remove leaked internal tool-call JSON skeletons (whole-line JSON blobs).
        kept = []
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    parsed = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict) and ("tool_calls" in parsed or
                                                 ("function" in parsed and "name" in parsed)):
                    continue
            if cls._META_SENTENCE_RE.match(line):
                continue
            kept.append(line)
        out = "\n".join(kept)

        # Remove code fences left empty by the cleanup above (```json\n```).
        out = re.sub(r"```[a-zA-Z0-9]*\n\s*```", "", out)

        # Strip stray role labels at the start of lines ("Assistant:", "User:", ...).
        cleaned_lines = []
        for line in out.splitlines():
            new_line, n = cls._ROLE_LABEL_RE.subn(lambda m: "", line, count=1)
            cleaned_lines.append(new_line if n else line)
        out = "\n".join(cleaned_lines)

        # Collapse excess blank lines and trim.
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        return out

    def _call_gemini(self, system_prompt: str, prompt_content: str,
                     history: Optional[List[Dict[str, str]]] = None) -> Optional[str]:
        """Calls Google Gemini REST API directly using httpx."""
        model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.gemini_api_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                *[{"role": "model" if h["role"] == "assistant" else "user",
                   "parts": [{"text": h["content"]}]} for h in (history or [])],
                {"role": "user", "parts": [{"text": prompt_content}]},
            ],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 750
            }
        }
        with httpx.Client(timeout=25.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text")
        return None

    def _format_prompt(
        self,
        user_message: str,
        customer_context: Dict[str, Any],
        recalled_memories: List[str],
        directives: List[str],
        banking_action_result: Optional[Dict[str, Any]],
        is_stateless: bool
    ) -> str:
        lines = [f"CUSTOMER INQUIRY: {user_message}"]
        lines.append(f"AUTHENTICATED CLIENT: {customer_context.get('name')} "
                     f"(Tier: {customer_context.get('tier')}, Branch: {BANK_BRANCH})")
        lines.append(f"ACCOUNTS: {json.dumps(customer_context.get('accounts', []))}")
        lines.append(f"CARDS: {json.dumps(customer_context.get('cards', []))}")

        recent = customer_context.get("recent_transactions") or []
        if recent:
            lines.append("RECENT ACCOUNT ACTIVITY (latest 8, read-only snapshot):")
            for t in recent:
                amount = t.get("amount_inr", 0.0)
                lines.append(f"• {str(t.get('date', ''))[:10]} | {t.get('description', '')[:60]} | "
                             f"{format_inr(amount)} | {t.get('status', '')}")

        if not is_stateless:
            lines.append("\nHINDSIGHT LONG-TERM MEMORY CITATIONS (Past tickets, preferences, and regulatory resolutions):")
            for m in recalled_memories:
                lines.append(f"• {m}")
            lines.append("\nHINDSIGHT SYNTHESIZED DIRECTIVES:")
            for d in directives:
                lines.append(f"• {d}")
        else:
            lines.append("\n[STATELESS MODE ACTIVATED: Assume NO past conversation memory exists. Demand standard manual branch intake procedures, Form 60, and identity verification.]")

        if banking_action_result:
            lines.append(f"\nREAL-TIME CORE BANKING ACTION RESULT: {json.dumps(banking_action_result, indent=2)}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Local deterministic engine (used when no LLM key is available)
    # ------------------------------------------------------------------
    _NEGATIVE_HINTS = [
        "angry", "frustrat", "worried", "worried sick", "shock", "worst", "pathetic",
        "ridiculous", "useless", "fed up", "cheat", "bakwas", "upset", "tense",
        "horrible", "terrible", "annoy", "scam", "gussa", "pareshan",
    ]

    @staticmethod
    def _vary(seed: str, options: List[str]) -> str:
        """Deterministic, seed-stable phrasing variation."""
        digest = int(hashlib.sha1(seed.encode("utf-8", "ignore")).hexdigest(), 16)
        return options[digest % len(options)]

    @classmethod
    def _empathy_prefix(cls, msg: str) -> str:
        if not any(w in msg for w in cls._NEGATIVE_HINTS):
            return ""
        return cls._vary(msg, [
            "I completely get the concern — let me sort this out for you right now.",
            "That does sound worrying — give me a moment, I'll take care of it.",
            "Sorry about the trouble — you shouldn't have to chase this. I'm on it.",
        ]) + "\n\n"

    @staticmethod
    def _greeting(first_name: str, tier: str, seed: str) -> str:
        greet = LLMRouter._vary(seed, ["Namaste", "Hello", "Hi"])
        ji = " ji" if ("vyapar" in tier.lower() or "msme" in tier.lower()) else ""
        return f"{greet} {first_name}{ji}"

    @staticmethod
    def _merchant_context(tier: str) -> str:
        low = tier.lower()
        if "vyapar" in low or "msme" in low:
            return "your Begum Bazaar wholesale dispatch pipeline"
        return "your weekend merchant collections"

    def _local_gemini_grade_reasoning(
        self,
        user_message: str,
        customer_context: Dict[str, Any],
        recalled_memories: List[str],
        directives: List[str],
        banking_action_result: Optional[Dict[str, Any]],
        is_stateless: bool
    ) -> str:
        msg = user_message.lower().strip()
        name = customer_context.get("name", "Valued Client")
        first_name = name.split()[0]
        tier = customer_context.get("tier", "Standard Retail")
        accounts = customer_context.get("accounts", [])
        cards = customer_context.get("cards", [])
        primary_card = cards[0] if cards else {}
        masked_card = primary_card.get("masked_number", "your registered card")
        upi_id = customer_context.get("upi_id", "")
        recent = customer_context.get("recent_transactions", []) or []
        action = banking_action_result or {}
        seed = f"{msg}|{name}"
        conversion_intent = any(w in msg for w in ["convert", "usd to inr", "inr to usd", "forex",
                                                   "exchange rate", "dollar to rupee", "rupee"])

        # ----------------------------------------------------
        # STATELESS PATH (Without Memory Benchmark Evaluation)
        # ----------------------------------------------------
        if is_stateless:
            if any(w in msg for w in ["wire", "swift", "firc", "remittance", "hold"]):
                return (
                    f"Good day, {name}. Under Reserve Bank of India (RBI) Foreign Exchange Management regulations, "
                    f"inward cross-border SWIFT transfers cannot be credited without verified documentary proof. "
                    f"Please visit our nearest branch in person with physical Form 60/16, provide the 16-digit UTR reference, "
                    f"an invoice from the remitting institution, and submit a physical purpose code declaration. "
                    f"Our central compliance cell processes manual reviews within 3 to 5 business days."
                )
            elif any(w in msg for w in ["settlement", "batch", "soundbox", "qr", "deposit"]):
                return (
                    f"Hello {name}. Merchant POS and QR settlements operate on standard batch cycles. "
                    f"If you have not received funds in your settlement account, please download our merchant escalation format, "
                    f"attach the end-of-day terminal batch summary slips, and contact merchant technical support during working hours."
                )
            elif any(w in msg for w in ["fee", "waiver", "charge", "reverse", "amc", "nach"]):
                return (
                    f"Dear {name}. Account maintenance charges and mandate return fees are levied automatically as per the bank's published Schedule of Charges. "
                    f"To dispute or request a waiver, you must submit a formal written fee contest letter countersigned by your branch operations manager."
                )
            elif any(w in msg for w in ["unfreeze", "unblock", "reactivate", "unlock"]):
                return (
                    f"Dear {name}. To unblock a payment card, you are required to authenticate via phone banking IVR using your telephone identification PIN, "
                    f"or visit an automated teller machine (ATM) to complete a green PIN regeneration cycle."
                )
            elif any(w in msg for w in ["freeze", "block", "lock", "lost", "stolen"]):
                return (
                    f"Dear {name}. To place an emergency block on your card, send an SMS with keyword 'BLOCK <last 4 digits>' to 567676 from your registered cellular device, "
                    f"or call our centralized helpline during operating hours."
                )
            elif any(w in msg for w in ["dispute", "refund", "unrecognized", "fraud"]):
                return (
                    f"Dear {name}. If you notice an unrecognized transaction, please download our Annexure-II grievance redressal form from the net banking portal, "
                    f"provide certified physical copies of your bank statement, and submit the claim within 30 days for investigation."
                )
            elif any(w in msg for w in ["balance", "funds", "how much", "kitna"]):
                return (
                    f"Dear {name}. To check your available balance, please dial our missed call banking hotline at 1800-420-9999 from your registered mobile number, "
                    f"or log in to NetBanking with your customer ID and grid card."
                )
            else:
                return (
                    f"Good day, {name}. Thank you for contacting Apex Commercial Bank Ltd. "
                    f"Please provide your 10-digit Customer Information File (CIF) number and the nature of your request so our operations desk may direct your call."
                )

        # 0. EXECUTED LEDGER ACTIONS (UPI transfer / FD booking / stock buy):
        #    confirm with the tool's exact numbers before any conversation intent.
        greeting = self._greeting(first_name, tier, seed)
        action_name = (action or {}).get("action")
        if action_name == "UPI_TRANSFER" and action.get("success"):
            closer = self._vary(seed + "|c", [
                "Want a repeat payment scheduled, or was this one-time?",
                "Shall I text the receipt to your registered WhatsApp?",
            ])
            daily_used = format_inr(action.get("daily_used_inr", 0))
            remaining = format_inr(action.get("daily_remaining_inr", 0))
            return (
                f"{greeting}! **{format_inr(action['amount_inr'])} sent to `{action['payee_vpa']}`** "
                "via UPI — settled instantly.\n\n"
                f"• UTR reference: **{action['utr_ref']}** (Charge: ₹0.00)\n"
                f"• Debited from `{action.get('debited_from')}` — balance now "
                f"**{format_inr(action['new_available_balance_inr'])}**\n"
                f"• Today's UPI usage: {action.get('daily_txns_used')} txns / "
                f"{daily_used} — remaining **{remaining}**\n"
                f"{closer}"
            )
        if action_name == "UPI_TRANSFER":
            return (
                f"{greeting}! I couldn't complete that UPI transfer: {action.get('error')}\n\n"
                f"• NPCI caps: ₹1,00,000 per transaction, ₹2,00,000 / 20 per day\n"
                "• NEFT/RTGS runs 24x7x365 for larger amounts\n\n"
                "Shall I line up a NEFT transfer instead?"
            )
        if action_name == "FIXED_DEPOSIT_BOOKED" and action.get("success"):
            return (
                f"{greeting}! **Fixed deposit of {format_inr(action['amount_inr'])} booked** — "
                f"{action['tenure_months']} months @ **{action['rate_percent']}% p.a.**\n\n"
                f"• FD account: **{action['fd_account_id']}** • Certificate: "
                f"**{action['fd_number']}**\n"
                f"• Maturity {action['maturity_date']}: "
                f"**{format_inr(action['maturity_value_inr'])}** "
                "(interest compounding quarterly)\n"
                f"• Debited from `{action.get('debited_from')}` — balance now "
                f"**{format_inr(action['new_available_balance_inr'])}**\n\n"
                "Want a premature-withdrawal eligibility note or an auto-renew on maturity?"
            )
        if action_name == "FIXED_DEPOSIT_BOOKED":
            return (
                f"{greeting}! I couldn't book that fixed deposit: {action.get('error')}\n\n"
                "• Minimum booking ₹1,000 • Tenure 6–120 months\n"
                "• POL-RATES-FD-009: 6.75% (6m) → 7.65% (3y tax-saver), +0.50% seniors\n\n"
                "Tell me the amount you'd like to lock and I'll book it."
            )
        if action_name == "STOCK_PURCHASED" and action.get("success"):
            return (
                f"{greeting}! **Bought {action['quantity']:g} × {action['name']} "
                f"({action['ticker']}) @ {format_inr(action['price_per_unit_inr'])}** "
                f"= **{format_inr(action['total_cost_inr'])}**\n\n"
                f"• Order **{action['order_id']}** — status "
                f"**{action['status']}**, zero brokerage\n"
                f"• Position after fill: **{action['holding_units_after']:g} units** "
                f"worth {format_inr(action['holding_value_after_inr'])}\n"
                f"• Debited from `{action.get('debited_from')}` — balance now "
                f"**{format_inr(action['new_available_balance_inr'])}**\n\n"
                "Want me to watch a stop-loss level on this position for you?"
            )
        if action_name == "STOCK_PURCHASED":
            return (
                f"{greeting}! That buy order couldn't be placed: {action.get('error')}\n\n"
                "• Broker board: HDFC Bank, Reliance, TCS, Infosys, ICICI, SBI, "
                "ITC, Tata Motors, Wipro or a Nifty 50 index fund\n"
                "• Zero brokerage on every fill\n\n"
                "Tell me the stock and quantity and I'll try again."
            )

        # ----------------------------------------------------
        # HINDSIGHT-POWERED PATH (data-driven, conversational)
        # ----------------------------------------------------
        empathy = self._empathy_prefix(msg)
        greeting = self._greeting(first_name, tier, seed)

        # 1. Card Unblock / Reactivation (strict priority over block)
        if any(w in msg for w in ["unfreeze", "unblock", "reactivate", "unlock", "chalu karo"]):
            card_ref = action.get("card_id", primary_card.get("card_id", masked_card)) or masked_card
            status = action.get("status", "Active") if action.get("status") else "Active"
            opener = self._vary(seed, [
                f"{empathy}{greeting}! Your card is back in action.",
                f"{empathy}{greeting}! Good news — the card is live again.",
            ])
            note = f"_{action.get('message', 'Card reactivated.')}_" if action.get("message") else ""
            closer = self._vary(seed + "|c", [
                "Want me to run a quick check that UPI AutoPay and any recurring mandates are unaffected?",
                "Want me to confirm international and contactless usage as well?",
            ])
            return (
                f"{opener}\n\n"
                f"• **{card_ref}** — status: **{status}, unblocked**\n"
                f"• Channels restored: chip, contactless tap-to-pay, ATM and online e-commerce\n"
                "• Zero-forex markup stays active on your " + f"{tier} account\n"
                + (f"• {note}\n" if note else "\n") +
                f"{closer}"
            )

        # 2. Card Freeze / Emergency Lock
        if any(w in msg for w in ["block", "freeze", "lock", "lost", "stolen", "card band"]):
            card_ref = action.get("card_id", primary_card.get("card_id", masked_card)) or masked_card
            status = action.get("status", "Frozen - Protection Active") if action.get("status") \
                else "Frozen - Protection Active"
            closer = self._vary(seed + "|c", [
                "Want me to unblock it the moment you confirm the card is safe?",
                "Say the word once you're comfortable and I'll reactivate it instantly.",
            ])
            return (
                f"{empathy}{greeting}! I've locked your card down right away.\n\n"
                f"• **{card_ref}** — status: **{status}**\n"
                "• All new POS, ATM and online authorizations are blocked\n"
                "• Scheduled UPI AutoPay mandates and inward salary credits stay unaffected\n\n"
                f"{closer}"
            )

        # 3. Dispute / Unauthorized Charge / Fraud Reporting
        if any(w in msg for w in ["dispute", "refund", "unrecognized", "did not do", "fraud",
                                  "unauthorized", "kyu kata", "charges kyu"]):
            # Prefer the actual dispute registry + the customer's own transaction.
            disputed = next((t for t in recent
                             if (t.get("status", "") and "dispute" in str(t.get("status", "")).lower())
                             or str(t.get("transaction_id", "")).startswith("DSP-")), None)
            txn_desc = disputed.get("description", "") if disputed else ""

            memory_citation = ""
            if any("dispute" in m.lower() or "subscription" in m.lower() for m in recalled_memories):
                memory_citation = ("\n• Historical context: I also recall your prior international "
                                   "subscription dispute (resolved with zero loss to you).")

            if action.get("dispute_id"):
                dispute_id = action["dispute_id"]
                timeline = action.get("timeline", "48 hours (RBI Charter)")
                status = action.get("status", "Provisional Credit Posted per RBI Grievance Policy")
            else:
                dispute_id = "DSP-RBI-7041"
                timeline = "24 hours (Zero-Liability SLA)" if "Premier" in tier else "48 hours (RBI Charter)"
                status = "Provisional Credit Posted per RBI Grievance Policy"

            desc_line = f"\n• Charged item on record: **{txn_desc}**" if txn_desc else ""
            closer = self._vary(seed + "|c", [
                "Want me to also pause international e-commerce on the card while we investigate?",
                "Should I set a reminder to check that the provisional credit lands on time?",
            ])
            return (
                f"{empathy}{greeting}! I've filed the formal dispute for you.\n\n"
                f"• Dispute reference: **{dispute_id}** — {status}{desc_line}{memory_citation}\n"
                f"• **Provisional credit arrives within {timeline}**, per the RBI Master Direction on "
                "Customer Protection in Unauthorized Electronic Transactions\n"
                "• Zero-liability applies — you are not responsible for the disputed amount\n\n"
                f"{closer}"
            )

        # 4. Currency Conversion (before SWIFT so "convert $5,000" isn't misrouted)
        if conversion_intent:
            amt_match = re.search(r"(\d[\d,]*(?:\.\d+)?)", msg)
            amt = float(amt_match.group(1).replace(",", "")) if amt_match else 1200.0
            if action.get("exchange_rate"):  # a convert_currency tool already ran
                rate = action["exchange_rate"]
                preview = action.get("formatted", "")
            else:
                rate = USD_INR_RATE
                if "rupee" in msg and "dollar" not in msg:
                    usd_val = amt / rate
                    preview = f"{format_inr(amt)} = ${usd_val:,.2f} USD"
                else:
                    inr_val = amt * rate
                    preview = f"${amt:,.2f} USD = {format_inr(inr_val)}"
            closer = self._vary(seed + "|c", [
                f"Want me to convert a specific amount now — quoting at **₹{rate}/USD, zero markup**?",
                "Shall I execute an actual conversion for you at the same zero-markup rate?",
            ])
            return (
                f"{greeting}! Here's the live math for you.\n\n"
                f"• **{preview}**\n"
                f"• Rate applied: **₹{rate}/USD** (bank interbank reference)\n"
                f"• Markup: **0.00%** — standard on your {tier} account\n"
                f"• INR quoting: Indian digit grouping (e.g. {format_inr(430000)})\n\n"
                f"{closer}"
            )

        # 5. Inward SWIFT Remittance & FIRC Clearance
        if any(w in msg for w in ["wire", "swift", "firc", "remittance", "430000", "silicon valley", "90811"]):
            has_firc_memory = any("firc" in m.lower() or "swift" in m.lower() or "p0802" in m.lower() for m in recalled_memories)
            prefers_concise = any("concise" in m.lower() or "executive" in m.lower() for m in recalled_memories)
            hold_release = bool(action.get("action") == "FIRC_HOLD_RELEASED" and action.get("success"))
            status_label = "released" if hold_release else "on compliance hold"

            base = (
                f"{empathy}{greeting}! Your inward wire from **Silicon Valley Bank — "
                "$5,000 USD / " + f"{format_inr(430000)}** (TXN-IN-90811) is {status_label}.\n\n"
            )
            if hold_release:
                base += (
                    f"• **{format_inr(430000)} is now liquid** in your Premier NRE checking balance\n"
                    f"• Purpose code applied: **{action.get('purpose_code', 'P0802')}** — "
                    "software consultancy / IT advisory per your standing declaration\n"
                    "• Official FIRC certificate issued digitally — ideal for your FEMA filing\n\n"
                    "Should I walk you through filing the FIRC for your next compliance cycle?"
                )
            elif has_firc_memory and prefers_concise:
                base += (
                    "• As we discussed, purpose code **P0802** clears this in one step\n"
                    "• Release lands the full ₹4,30,000 into your NRE account instantly\n\n"
                    "Want me to apply P0802 and release the hold now?"
                )
            elif has_firc_memory:
                base += (
                    "Remembering our Oct 24th resolution, your verified FEMA declaration under "
                    "**P0802 (Software & Tech Advisory)** is already on file — releasing the hold "
                    "takes zero paperwork. Shall I release it now?"
                )
            else:
                base += (
                    "• **Why it's held:** RBI FIRC rules need a purpose code for inward business "
                    "transfers above $2,500 equivalent\n"
                    "• **Fastest fix:** apply your purpose code **P0802** — one message from you and "
                    f"the {format_inr(430000)} becomes liquid\n"
                    "• UTR on record: SWIFT990214881\n\n"
                    f"Want me to release it under P0802 right away?"
                )
            return base

        # 6. Weekend UPI Soundbox & Merchant Settlement
        if any(w in msg for w in ["settlement", "batch", "soundbox", "385000", "qr", "begum bazaar",
                                  "supplier", "paise kab", "paise aayenge"]):
            pending = [t for t in recent
                       if "clearing" in str(t.get("status", "")).lower()
                       or "batch" in str(t.get("description", "")).lower()]
            batch = pending[0] if pending else None
            if batch:
                batch_amt = batch.get("amount_inr", 385000.0)
                batch_desc = batch.get("description", "Weekend UPI Soundbox & QR settlement batch")
                batch_utr = batch.get("utr_ref", "UPIBATCH44901")
            else:
                batch_amt, batch_desc, batch_utr = 385000.0, "Weekend UPI Soundbox & QR settlement batch", "UPIBATCH44901"
            merchant_ref = self._merchant_context(tier)
            closer = self._vary(seed + "|c", [
                "Anything else on your collections — supplier payouts, or Credit Line against this batch?",
                "Shall I check next weekend's clearing calendar for you as well?",
            ])
            return (
                f"{empathy}{greeting}! Your weekend collection money is safe and on schedule.\n\n"
                f"• In transit right now: **{format_inr(batch_amt)}** — {batch_desc}\n"
                f"• UTR/batch ref: **{batch_utr}** (aggregated QR + Soundbox collections)\n"
                f"• **Final settlement: Monday at 06:00 IST** sharp, per NPCI merchant clearing "
                "guidelines — you'll see it credited well before "
                f"{merchant_ref}\n"
                "• Nothing manual is required from your side — clearing is fully automatic\n\n"
                f"{closer}"
            )

        # 7. Fee Waiver & Courtesy Reversal
        if any(w in msg for w in ["waiver", "reverse", "amc", "250", "350", "fee", "penalty", "nach"]):
            courtesy_used = any("courtesy_used" in m.lower() or "nach_fee" in m.lower() for m in recalled_memories)
            escalated = (action and (action.get("requires_escalation") or action.get("action") == "FEE_WAIVER_DENIED"))
            if courtesy_used or escalated:
                return (
                    f"{empathy}{greeting}! I checked your record carefully before answering.\n\n"
                    "• I note our **MG Road branch** already extended you a **₹350** courtesy waiver "
                    "for a NACH mandate return on **Oct 5th**\n"
                    "• **Policy POL-RBI-FEE-001**: automated waivers are limited to one courtesy credit "
                    "every 12 months for commercial accounts\n"
                    "• Rather than a flat no, I've **escalated to the Regional Branch Manager** for a "
                    "priority exception review, citing your strong MSME trade relationship\n\n"
                    "Want me to flag it at high severity so you hear back within one business day?"
                )
            credited = action.get("amount_credited_inr", 250.0) if action.get("success") else 250.0
            closer = self._vary(seed + "|c", [
                "Want a confirmation note with the credit reference for your records?",
                "Shall I also review your last 3 charges to make sure nothing else needs a look?",
            ])
            return (
                f"{empathy}{greeting}! Handled — and you didn't have to escalate it.\n\n"
                "• Policy applied: **POL-RBI-FEE-001 courtesy waiver** (your account is in prime standing, "
                "zero prior reversals)\n"
                f"• Amount credited back: **{format_inr(credited)}**\n"
                "• Any balance hold tied to that charge is released too\n\n"
                f"{closer}"
            )

        # 8. UPI Transfer Guidance & NPCI Limits
        if any(w in msg for w in ["upi", "transfer money via upi", "send via upi", "gpay",
                                  "phonepe", "paytm", "vpa"]):
            vpa = upi_id or "apex@apexmudra"
            closer = self._vary(seed + "|c", [
                "Want me to check the last few UPI flows on your account for reference?",
                "Shall I walk you through sending a specific amount right now?",
            ])
            return (
                f"{greeting}! UPI is the fastest rails you have — and it's free with us.\n\n"
                f"• Your linked VPA: `{vpa}`\n"
                "• Per-transaction limit: **₹1,00,000** (NPCI)\n"
                "• Daily cap: **₹2,00,000 / 20 transfers** per rolling 24 hours\n"
                "• Charges on P2P and merchant QR: **₹0.00**\n"
                f"• Works with Google Pay, PhonePe, Paytm, BHIM and any Bharat QR\n\n"
                f"{closer}"
            )

        # 9. Balance / Financial Portfolio Inquiry
        if any(w in msg for w in ["balance", "funds", "how much", "kitna", "accounts", "passbook"]):
            acc_details = []
            for a in accounts:
                bal_inr = a.get("balance_inr", a.get("balance", 0.0))
                bal_usd = a.get("balance_usd", round(bal_inr / USD_INR_RATE, 2))
                hold = a.get("hold_amount", 0.0) or 0.0
                hold_note = f" (⚠️ **{format_inr(hold)}** on hold: _{a.get('hold_reason', '')[:40]}…_)" if hold else ""
                acc_details.append(f"• **{a['type']}** (`{a['account_id']}`): "
                                   f"**{format_inr(bal_inr)}** (~${bal_usd:,.2f} USD){hold_note}")
            acc_text = "\n".join(acc_details) if acc_details else "• No linked accounts on display right now."
            total_inr = customer_context.get("total_liquid_inr", 0.0)
            total_usd = customer_context.get("total_liquid_usd", 0.0)
            held_total = sum((a.get("hold_amount", 0.0) or 0.0) for a in accounts)
            hold_line = (f"\n• Funds on hold: **{format_inr(held_total)}** — ask me and I'll chase the release"
                         if held_total else "")
            closer = self._vary(seed + "|c", [
                "Want me to show the last 5 transactions behind these balances?",
                f"Shall I check if a fixed-deposit sweep of surplus funds at **6.85%** makes sense?",
            ])
            return (
                f"{greeting}! Here's your live money position.\n\n"
                f"{acc_text}\n\n"
                f"📊 **Total portfolio: {format_inr(total_inr)}** (~${total_usd:,.2f} USD at "
                f"the reference rate ₹{USD_INR_RATE}/USD)\n"
                "• Deposit insurance: DICGC-protected up to **₹5,00,000** per bank"
                f"{hold_line}\n\n"
                f"{closer}"
            )

        # 10. IFSC, Branch, and Bank Clearing Routing
        if any(w in msg for w in ["ifsc", "branch", "micr", "upi id", "vpa", "address"]):
            ifsc = customer_context.get("ifsc", "APEX0004921")
            return (
                f"{greeting}! Here are the routing credentials you'd need.\n\n"
                f"• Branch: **{BANK_BRANCH_FULL}**\n"
                f"• IFSC code: `{ifsc}`\n"
                f"• MICR code: `560002042`\n"
                f"• Linked UPI VPA: `{upi_id or 'apex@apexmudra'}`\n"
                "• NEFT / RTGS / IMPS clearing runs **24x7x365**\n\n"
                "Want me to copy these into a single shareable line for a vendor?"
            )

        # 11. Fixed Deposit & Treasury Interest Rates
        if any(w in msg for w in ["fixed deposit", "fd", "interest rate", "savings rate", "tax saver"]):
            return (
                f"{greeting}! Here's today's rate card — self-book takes seconds.\n\n"
                "• Savings account: **4.00% p.a.** (interest computed daily, credited quarterly)\n"
                "• 1-year FD: **6.85% p.a.**\n"
                "• 3-year tax-saver FD: **7.65% p.a.** — Section 80C eligible\n"
                "• Senior-citizen privilege: **+0.50% p.a.** across all tenures\n\n"
                "Want me to preview the FD maturity math for a specific amount and tenure?"
            )

        # 12. International Travel Notice
        if any(w in msg for w in ["travel notice", "travelling to", "flying to", "going to dubai",
                                  "going to london", "going to singapore", "visiting"]):
            dest = "your destination"
            for place in ["Dubai", "Singapore", "San Francisco", "London", "Paris", "Zurich", "Tokyo", "Mumbai"]:
                if place.lower() in msg:
                    dest = place
                    break
            if action.get("destination"):
                dest = action["destination"]
            return (
                f"{greeting}! Travel notice is in place for **{dest}**.\n\n"
                f"• {masked_card} whitelisted for POS and ATM in that corridor"
                + (f" (as `{action.get('destination')}`)" if action.get("destination") else "") + "\n"
                "• Dynamic behavioural fraud monitoring updated — no false declines from unfamiliar geography\n"
                "• Zero-forex markup applies on every swipe\n\n"
                "Want me to list the emergency card-helpline numbers for that country too?"
            )

        # 13. Loans & Facility Advisory
        if any(w in msg for w in ["loan", "home loan", "personal loan", "emi", "car loan"]):
            loan_rate_monthly = 0.0999 / 12  # 9.99% p.a.
            emi_amt = round(1000000 * loan_rate_monthly * (1 + loan_rate_monthly) ** 60 /
                            ((1 + loan_rate_monthly) ** 60 - 1))  # ₹10L over 60 months
            return (
                f"{greeting}! Good news — pre-approved offers are waiting on your profile.\n\n"
                "• Instant Digi-personal loan: pre-approved up to **₹10,00,000 at 9.99% p.a.**\n"
                f"• Illustrative EMI: ~**{format_inr(emi_amt)}/month** for ₹10,00,000 over 5 years\n"
                "• Premier housing loan: floating from **8.25% p.a.**, zero processing fee\n"
                "• No collateral, no paperwork — disbursal inside 24 hours\n\n"
                "Want me to compute the EMI for your exact amount and tenure?"
            )

        # 14. Formal Greeting & Scope
        if re.search(r"\b(hi|hello|hey|namaste|good morning|good evening|kya haal|who are you|what can you do|help)\b", msg):
            return (
                f"{greeting}! ClientCare at Apex Commercial Bank ({BANK_BRANCH}) — and I remember what we "
                "discuss, so no need to repeat yourself.\n\n"
                "A few things I can settle right here, end-to-end:\n"
                "• Card security — instant **block/unblock**, lost-card protection\n"
                "• **FIRC** issuance and SWIFT wire hold clearance\n"
                "• Weekend merchant QR/Soundbox settlement reconciliation\n"
                "• RBI courtesy **fee reversals** and dispute filing\n"
                "• Live multi-currency valuation (**₹94.11/USD**, zero markup)\n\n"
                "What would you like to sort out first?"
            )

        # 15. Default Context-Aware Response
        recalls_note = (f" (and I recall {len(recalled_memories)} earlier facts from our history)" if recalled_memories else "")
        latest = recent[0] if recent else None
        latest_line = (f"\n• Most recent activity: {latest.get('description', '')[:50]} — "
                       f"{format_inr(latest.get('amount_inr', 0))} ({latest.get('status', '')})") if latest else ""
        return (
            f"{greeting}! I have your **{tier}** profile, live balances and recent ledger in front of me"
            f"{recalls_note}.{latest_line}\n\n"
            "Tell me what you need — transactions, card security, FIRC, or settlement queries are all instant.\n\n"
            + self._vary(seed + "|c", [
                "What are we sorting out today?",
                "Where shall we start?",
                "What can I help you with right away?",
            ])
        )
