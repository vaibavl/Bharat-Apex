# 🏦 BharatApex AI — ClientCare Conversational Banking Agent

> A full-stack **FinTech customer-support AI agent** for an Indian commercial bank ("BharatApex"),
> with **persistent long-term client memory**, tool-calling over a synthetic core-banking layer,
> and a compliance guardrail engine (RBI / NPCI / FEMA style rules).
> Maintained for **Hack-with-Bengaluru selection** — shared so teammates (and any AI assistant)
> can understand and run it quickly.

**Status:** working & demo-ready · **Stack:** Python 3.10+ / FastAPI / SQLite / vanilla JS (no build step) · The chatbot uses a real LLM when keys are provided (Groq), and ships with a deterministic local fallback engine so the demo **never breaks without keys**.

---

## 1. What is this, in one paragraph?

BharatApex AI is a **customer-service agent for a bank**, exposed through a premium dark dashboard
at `http://127.0.0.1:8000`. A user logs in as one of three synthetic bank customers and chats with
"ClientCare" — an agent that checks balances, releases SWIFT/FIRC holds, blocks/unblocks cards,
files RBI disputes, waives fees (with policy limits), converts INR↔USD, and registers travel
notices — by **calling real backend tools**, not by hallucinating. Every action and insight is
remembered in a **long-term memory engine (Vectorize Hindsight)**, so the agent answers follow-ups
with context on later days ("that wire we talked about…"). Everything is sandboxed per customer:
one login can only ever see its own accounts, cards, memories and conversations.

## 2. The demo personas

| Username | Password | Customer | Persona |
|----------|----------|----------|---------|
| `aarav`  | `Apex@101` | CUST-IN-101 Aarav Singhania | Premier Private & NRI Wealth (USD + NRE, SWIFT/FIRC) |
| `rajesh` | `Apex@102` | CUST-IN-102 Rajesh Sharma | MSME Vyapar trader (UPI/Soundbox weekend batches) |
| `priya`  | `Apex@103` | CUST-IN-103 Priya Patel | Youth digital (disputes, card controls, fee waiver) |

Passwords are demo defaults, overridable via `DEMO_*_PASSWORD` in `.env`.

## 3. How to run it

```powershell
# 1. Install dependencies (Python 3.10+ required)
pip install -r requirements.txt

# 2. (Optional) create .env — copy from .env.example and add keys.
#    The app runs 100% fine with NO keys: it falls back to the local engine.

# 3. Start
python run.py
# → http://127.0.0.1:8000   (dashboard + login)
# → http://127.0.0.1:8000/docs  (interactive FastAPI/Swagger docs)
```

⚠️ **Windows console note:** `run.py` prints an emoji banner. If PowerShell throws
`UnicodeEncodeError`, run with `$env:PYTHONIOENCODING="utf-8"; python run.py`
(or add the variable in `.vscode/launch.json` — it is already configured there; **F5 works as-is**).

### Environment variables (see `.env.example`)

| Variable | Purpose | If empty |
|---|---|---|
| `LLM_PROVIDER` | `groq` (recommended) / `gemini` / `openai` | local fallback engine |
| `GROQ_API_KEY` / `GROQ_MODEL` | real conversational LLM with tool-calling | local fallback engine |
| `HINDSIGHT_API_KEY`, `HINDSIGHT_BASE_URL` | Vectorize Hindsight Cloud memory | built-in local memory engine (`data/hindsight_local_banks.json`) |
| `BAND_AGENT_ID`, `BAND_API_KEY` | Band AI Desktop agent integration (`https://www.band.ai`) | runs in standalone mode |
| `PORT` / `HOST` | server binding | `127.0.0.1:8000` |
| `SESSION_TTL_HOURS` | server-side session lifetime | `12` |

Stale-data note: the FX rate used everywhere is **1 USD = ₹94.11** (brought in from the live
rate on 2026-11-08), and the bank's branch identity is **MG Road Corporate Branch, Bengaluru**
(`RBI/2026/BLR-049`). Customer residences remain Hyderabad-based on purpose.

## 4. What the agent can actually do (capability + compliance map)

| Capability | Rule/logic behind it | Where it lives |
|---|---|---|
| Card block / unblock | instant, zero-friction; urgent on "lost/stolen" | `src/banking/core_banking_service.py` |
| Fee waivers | courtesy waiver once per 12 months (`POL-RBI-FEE-001`), else escalate to Regional Branch Manager | same |
| Disputes + provisional credit | RBI zero-liability / customer-protection style flow | same |
| SWIFT / FIRC hold release | purpose-code memory (`P0802`) expedites hold | same |
| **UPI transfer (executes money)** | instant NPCI P2P debit + settled ledger row + UTR; caps ₹1,00,000/txn, ₹2,00,000 & 20/day; any VPA the customer names is authorized (no "saved payee" step) | same |
| **Fixed deposit booking (executes money)** | books ₹1,000+ for 6–120 months at the `POL-RATES-FD-009` card (6.75% → 7.65%, +0.50% seniors); opens a live FD account + updates holdings and maturity math | same |
| **Stock / index-fund purchase (executes money)** | market orders at ₹0 brokerage from a synthetic board (HDFC Bank, Reliance, TCS, Infosys, ICICI, SBI, ITC, Tata Motors, Wipro, Nifty 50 index fund); whole-share equity, T+1 Demat credit | same |
| Currency conversion | USD↔INR at ₹94.11, zero portfolio markup, Indian digit grouping | same |
| Reminder of settlement times | weekend UPI/QR batches settle Monday 06:00 IST | same |
| Travel notices | card regions enabled on request | same |
| Live dashboard sync | every executed action debits the account, appends a ledger row and updates the Holdings panel in the same request | `src/banking/core_banking_service.py` + `src/static/app.js` |
| Compliance guardrails | never asks for PIN/CVV/OTP; escalates Ombudsman/legal threats to the Principal Nodal Officer; single-customer data isolation | `src/banking/compliance.py` |

Executed money moves are **deterministic**: the orchestrator runs the UPI/FD/stock keyword
engine *before* the LLM, so a demo action always lands even if the model hiccups — the LLM
(Groq) still generates all conversational replies and tool-calls for everything else. Every
executed UPI transfer / FD / stock order is confirmed with Indian-grouped amounts
(`₹24,09,850.00`).

## 5. Project structure (LLM-friendly map)

```
apex-fintech-agent/
├── run.py                       # entry point: uvicorn on 127.0.0.1:8000 (F5 profile = same)
├── requirements.txt             # FastAPI, uvicorn, python-dotenv, httpx, requests, jinja2
├── .env.example                 # key template — copy to .env (real keys never committed)
├── data/
│   ├── clientcare.db            # SQLite: sessions, conversations, messages (auto-created)
│   └── hindsight_local_banks.json  # local memory fallback store (auto-created/seeded)
├── scripts/
│   └── reset_memory.py          # wipes local memory banks, re-seeds the 3 demo customers
├── src/
│   ├── api/
│   │   └── app.py               # FastAPI app + all routes + static file serving
│   ├── auth.py                  # login, server-side sessions, HttpOnly `apex_session` cookie
│   ├── db.py                    # SQLite helpers (conversations/messages persist across refreshes)
│   ├── agent/                   # ★ the conversational brain
│   │   ├── orchestrator.py      # pipeline: compliance → memory recall → snapshot → LLM/tools → retain memory
│   │   ├── llm_router.py        # Groq/OpenAI/Gemini router + tool loop + local fallback engine
│   │   ├── prompts.py           # system prompt (tone/rules/facts) + classification prompt
│   │   ├── tools.py             # ToolExecutor: binds tool calls to the logged-in customer only
│   │   └── comparison.py        # stateless-bot benchmark copy for the Benchmark button
│   ├── banking/                 # ★ the synthetic bank
│   │   ├── core_banking_service.py  # accounts, cards, transactions, holds, waivers, conversions (USD_INR_RATE=94.11)
│   │   └── compliance.py        # input sanitization + escalation checks
│   ├── band/                    # ★ Band AI Desktop & multi-agent mesh integration
│   │   └── band_service.py      # REST client, identity, room polling & message dispatch
│   ├── hindsight/               # ★ memory layer (Vectorize Hindsight Cloud w/ local fallback)
│   │   ├── client_wrapper.py    # cloud client + local fallback wiring
│   │   ├── engine.py            # local memory engine (seeded facts, recall/reflect/retain)
│   │   └── types.py             # Recall/Reflect/Retain result types
│   ├── synthetic_data/          # demo world (loaded at startup)
│   │   ├── customers.json       # 3 personas, balances, memory facts
│   │   ├── transactions.json    # transaction ledger seed
│   │   ├── policies.json        # bank policies (FX, UPI caps, waiver rule, HQ, MICR)
│   │   └── data_loader.py       # loads/validates the JSON seeds
│   └── static/                  # the whole frontend (served at /)
│       ├── index.html           # layout: sidebar / top bar / stat cards / chat / wallet / history
│       ├── styles.css           # dark premium design system (orange #FF4D00 accent) + light theme
│       └── app.js               # all UI logic, fetch calls, chat rendering — no frameworks
└── tests/                       # automated suite (fully mocked, no network needed)
```

## 6. Known quirks / notes for contributors

- The `.env` holds real keys and is **gitignored by design** — never commit it.
- If Hindsight Cloud is unreachable, the app silently switches to local memory (log line:
  `Cloud recall error … Using local`); the demo stays fully functional.
- The chatbot's model on Groq: use `openai/gpt-oss-120b` (older `llama-3.3-70b-versatile` was decommissioned).
- Groq free tier rejects any request over **8,000 tokens** — the agent prompt, tool schemas,
  history (last 8 messages) and memories (4 facts) are all capped to stay well under it.
- End-to-end verification: `python scripts/simulate_customer_flows.py` with the server running
  checks all 100 logins/overviews/chats and (for a 10-customer sample, `SIM_ACTION_SAMPLE=10`)
  EXECUTES a UPI + FD + stock basket through the chat and verifies the ₹7,490 ledger debit.
- Emoji banner needs UTF-8 console (see §3).
- A small suite of automated tests lives in `tests/` (offline, mocked) — speak to the team
  before touching `src/agent/` or `src/banking/` and keep the suite green.
- The UI was redesigned (v3): orange-accent dark dashboard, sidebar nav, stat cards, wallet tiles,
  cash-flow chart, recent-activities table, and an INR/USD/Dual toggle bound to all money displays.

## 7. Repo / team workflow for Hack-with-BLR

- Upstream repo: `hack_with_blr_selection` (shared selection repo).
- Do not push `.env` or generated `data/` stores.
- Suggested branches: `main` = stable demo, `feat/<name>-<topic>` per teammate.
