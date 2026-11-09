import json
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import auth, db
from ..agent.comparison import MemoryBenchmarkComparator
from ..agent.orchestrator import support_agent
from ..banking.core_banking_service import core_banking
from ..band.band_service import band_service
from ..hindsight.client_wrapper import hindsight_service

app = FastAPI(
    title="BharatApex AI - FinTech Support Agent API",
    description="Login-protected conversational banking agent with Hindsight memory",
    version="2.0.0",
)

db.init_db()


# ----------------------------------------------------------------- models
class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    message: str = Field(max_length=4000)


class BenchmarkRequest(BaseModel):
    message: str = Field(max_length=4000)


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class MemoryRecallRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class CardToggleRequest(BaseModel):
    card_id: str = Field(min_length=1, max_length=64)
    field: Optional[str] = "status"


class BandMessageRequest(BaseModel):
    chat_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)



Session = Depends(auth.current_session)


def _title_from(message: str) -> str:
    t = " ".join(message.split())
    return (t[:42] + "…") if len(t) > 43 else (t or "New chat")


def _format_message(m: dict) -> dict:
    meta = json.loads(m["meta"]) if m.get("meta") else {}
    return {"id": m["id"], "role": m["role"], "content": m["content"],
            "created_at": m["created_at"], **meta}


# ------------------------------------------------------------------- auth
@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response):
    customer_id = auth.authenticate(req.username, req.password)
    if not customer_id:
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    token = auth.create_session(customer_id, req.username)
    response.set_cookie(
        auth.COOKIE_NAME, token,
        max_age=auth.session_ttl_hours() * 3600,
        httponly=True, samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "0") == "1",
    )
    summary = core_banking.get_account_summary(customer_id)
    return {"customer_id": customer_id, "name": summary["name"], "tier": summary["tier"]}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    auth.end_session(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME)
    return {"status": "signed_out"}


@app.get("/api/auth/me")
def me(session: dict = Session):
    summary = core_banking.get_account_summary(session["customer_id"])
    return {"customer_id": session["customer_id"], "username": session["username"],
            "name": summary["name"], "tier": summary["tier"]}


# -------------------------------------------------------------- overview
@app.get("/api/overview")
def overview(session: dict = Session):
    cid = session["customer_id"]
    return {
        "profile": core_banking.get_account_summary(cid),
        "transactions": core_banking.get_transactions(cid, limit=10),
    }


# ---------------------------------------------------------- conversations
@app.get("/api/conversations")
def list_conversations(session: dict = Session):
    return db.list_conversations(session["customer_id"])


@app.post("/api/conversations")
def new_conversation(session: dict = Session):
    return db.create_conversation(session["customer_id"])


@app.patch("/api/conversations/{conv_id}")
def rename_conversation(conv_id: str, req: RenameRequest, session: dict = Session):
    if not db.rename_conversation(session["customer_id"], conv_id, req.title.strip()):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"status": "renamed"}


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str, session: dict = Session):
    if not db.delete_conversation(session["customer_id"], conv_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"status": "deleted"}


@app.get("/api/conversations/{conv_id}/messages")
def conversation_messages(conv_id: str, session: dict = Session):
    if not db.get_conversation(session["customer_id"], conv_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return [_format_message(m) for m in db.get_messages(conv_id)]


# ------------------------------------------------------------------- chat
@app.post("/api/chat")
def chat(req: ChatRequest, session: dict = Session):
    cid = session["customer_id"]
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    conv = db.get_conversation(cid, req.conversation_id) if req.conversation_id else None
    if req.conversation_id and not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not conv:
        conv = db.create_conversation(cid, _title_from(message))

    prior = db.get_messages(conv["id"], limit=20)
    history = [{"role": m["role"], "content": m["content"]} for m in prior]

    if not prior and conv["title"] == "New chat":
        db.rename_conversation(cid, conv["id"], _title_from(message))

    result = support_agent.process_message(
        customer_id=cid, message=message, use_memory=True, history=history)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    meta = {
        "actions": result.get("actions", []),
        "latency_ms": result.get("latency_ms"),
        "escalation": result.get("escalation"),
    }
    db.add_message(conv["id"], "user", message)
    db.add_message(conv["id"], "assistant", result["reply"], json.dumps(meta, default=str))

    return {
        "conversation": db.get_conversation(cid, conv["id"]),
        "reply": result["reply"],
        **meta,
    }


@app.post("/api/benchmark")
def benchmark(req: BenchmarkRequest, session: dict = Session):
    return MemoryBenchmarkComparator.compare(customer_id=session["customer_id"], message=req.message)


# --------------------------------------------------------- investments
@app.get("/api/investments/analysis")
def investment_analysis(session: dict = Session):
    return core_banking.get_investment_analysis(session["customer_id"])


# -------------------------------------------------------------- memory
@app.get("/api/memory")
def get_memory(session: dict = Session):
    cid = session["customer_id"]
    bank_id = f"bank_{cid}"
    raw_memories = hindsight_service.get_all_memories(bank_id)
    summary = core_banking.get_account_summary(cid)

    reflect_res = hindsight_service.local_engine.reflect(
        bank_id=bank_id, query="customer profile sentiment preferences"
    )

    return {
        "bank_id": bank_id,
        "customer_id": cid,
        "customer_name": summary.get("name"),
        "tier": summary.get("tier"),
        "mode": hindsight_service.mode,
        "total_memories": len(raw_memories),
        "memories": raw_memories,
        "disposition": reflect_res.disposition,
        "directives": reflect_res.directives,
        "reflection_summary": reflect_res.answer,
    }


@app.post("/api/memory/recall")
def test_recall(req: MemoryRecallRequest, session: dict = Session):
    cid = session["customer_id"]
    bank_id = f"bank_{cid}"
    recall_res = hindsight_service.recall(
        bank_id=bank_id, query=req.query, max_results=6
    )
    return {
        "bank_id": bank_id,
        "query": req.query,
        "mode": hindsight_service.mode,
        "results": [
            {
                "fact": r.fact,
                "score": r.score,
                "type": r.type,
                "timestamp": r.timestamp,
                "tags": r.tags,
            }
            for r in recall_res.results
        ],
    }


@app.post("/api/memory/reset")
def reset_memory(session: dict = Session):
    cid = session["customer_id"]
    bank_id = f"bank_{cid}"
    hindsight_service.reset_bank(bank_id)
    return {"status": "reset_completed", "bank_id": bank_id}


# --------------------------------------------------------------- cards
@app.post("/api/cards/toggle")
def toggle_card(req: CardToggleRequest, session: dict = Session):
    cid = session["customer_id"]
    if req.field == "international":
        return core_banking.toggle_card_international(cid, req.card_id)
    return core_banking.toggle_card_freeze(cid, req.card_id)


# ------------------------------------------------------- notifications
@app.get("/api/notifications")
def notifications(session: dict = Session):
    cid = session["customer_id"]
    items = []
    if cid == "CUST-IN-101":
        items = [
            {
                "id": "notif-1",
                "title": "Inward SWIFT Wire Under Compliance Review",
                "message": "$5,000.00 USD (TXN-IN-90811) from Silicon Valley Bank held per FEMA guidelines. Standing declaration with Purpose Code P0802 ready for instant STP release.",
                "type": "alert",
                "time": "12 mins ago",
                "action_prompt": "Why is my $5,000 SWIFT wire from Silicon Valley Bank on hold?",
            },
            {
                "id": "notif-2",
                "title": "Quarterly FD Interest Accrual",
                "message": "Interest of ₹28,687.50 accrued on High-Yield NRE Fixed Deposit (7.65% p.a.). Credited to Premier NRE Checking.",
                "type": "success",
                "time": "1 day ago",
                "action_prompt": "What are my latest FD interest rates and tax savings?",
            },
            {
                "id": "notif-3",
                "title": "RBI Reference Forex Fixation",
                "message": "Interbank reference rate fixed at ₹94.1100 / USD. Zero foreign currency conversion markup active on Premier accounts.",
                "type": "info",
                "time": "2 days ago",
                "action_prompt": "Convert $1,200 USD to INR at today's interbank rate",
            },
        ]
    elif cid == "CUST-IN-102":
        items = [
            {
                "id": "notif-1",
                "title": "Weekend Soundbox Batch Ready for Settlement",
                "message": "Begum Bazaar QR & Soundbox collections of ₹3,85,000 batched. Automatic Monday morning NEFT/RTGS settlement scheduled for 06:00 IST.",
                "type": "info",
                "time": "25 mins ago",
                "action_prompt": "Where is my weekend Soundbox & QR collection of ₹3,85,000? I need it for Monday dispatches.",
            },
            {
                "id": "notif-2",
                "title": "Courtesy Fee Waiver Credited",
                "message": "Courtesy reversal of ₹350 NACH return fee processed under RBI Master Direction on Customer Protection.",
                "type": "success",
                "time": "3 hours ago",
                "action_prompt": "Can you waive my ₹350 NACH mandate return fee as a courtesy?",
            },
            {
                "id": "notif-3",
                "title": "Commercial RuPay Card Active",
                "message": "RuPay Commercial Card ending 8912 active with Begum Bazaar POS terminal limits enabled.",
                "type": "info",
                "time": "1 day ago",
                "action_prompt": "What is the status of my RuPay business card?",
            },
        ]
    else:
        items = [
            {
                "id": "notif-1",
                "title": "Provisional Dispute Credit Posted",
                "message": "Provisional credit of ₹899.00 posted for disputed international cloud transaction under RBI Zero Liability framework.",
                "type": "success",
                "time": "18 mins ago",
                "action_prompt": "I did not authorize the ₹899 international cloud charge, please refund me.",
            },
            {
                "id": "notif-2",
                "title": "Mutual Fund NAV Gain Alert",
                "message": "Parag Parikh Flexi Cap Fund gained +1.8% this week. Current portfolio value: ₹1,45,000.",
                "type": "info",
                "time": "1 day ago",
                "action_prompt": "How are my mutual fund investments performing?",
            },
            {
                "id": "notif-3",
                "title": "Upcoming UPI AutoPay Mandate",
                "message": "Standing UPI mandate for Spotify Premium (₹119.00) scheduled for debit in 3 days.",
                "type": "info",
                "time": "2 days ago",
                "action_prompt": "What are my active UPI mandate limits?",
            },
        ]

    return {"notifications": items, "unread_count": len(items)}


# ----------------------------------------------------------- statement
@app.get("/api/statement/export")
def export_statement(session: dict = Session):
    cid = session["customer_id"]
    summary = core_banking.get_account_summary(cid)
    txns = core_banking.get_transactions(cid, limit=100)

    lines = [
        "BHARATAPEX COMMERCIAL BANK LTD. - CUSTOMER ACCOUNT STATEMENT",
        f"Customer Name: {summary.get('name')}",
        f"Customer ID: {cid}",
        f"Account Tier: {summary.get('tier')}",
        f"Statement Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "Branch: MG Road Corporate Branch Bengaluru (RBI/2026/BLR-049)",
        "IFSC: APEX0004921",
        "",
        "Date,Transaction ID,Description,Category,Payment Mode,UTR / Reference,Amount (INR),Amount (USD),Status",
    ]

    for t in txns:
        d = t.get("date", "")[:10]
        tid = t.get("transaction_id", "")
        desc = f'"{t.get("description", "")}"'
        cat = t.get("category", "")
        mode = t.get("payment_mode", "UPI")
        utr = t.get("utr_ref", t.get("status", ""))
        amt_inr = t.get("amount_inr", t.get("amount", 0.0))
        amt_usd = t.get("amount_usd", round(amt_inr / 94.11, 2))
        stat = t.get("status", "Settled")
        lines.append(f"{d},{tid},{desc},{cat},{mode},{utr},{amt_inr},{amt_usd},{stat}")

    csv_data = "\n".join(lines)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="BharatApex_Statement_{cid}.csv"'},
    )


# ------------------------------------------------------------- band.ai
@app.get("/api/band/status")
def get_band_status():
    """Return status and metadata for the Band AI agent integration."""
    return band_service.get_status()


@app.get("/api/band/chats")
def get_band_chats():
    """List active rooms / chats from the Band AI network."""
    return {"chats": band_service.list_chats()}


@app.post("/api/band/poll")
def poll_band():
    """Manually trigger a Band AI message poll cycle."""
    processed = band_service.poll_messages()
    return {"status": "polled", "processed": processed, "count": len(processed)}


@app.post("/api/band/message")
def send_band_message(req: BandMessageRequest):
    """Post an outbound message to a Band AI chat room."""
    ok = band_service.send_message(req.chat_id, req.message)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to deliver message to Band AI room")
    return {"status": "sent", "chat_id": req.chat_id}


@app.on_event("startup")
def on_startup():
    if band_service.connected and band_service.auto_poll:
        band_service.start_background_worker(interval_seconds=8)


# ----------------------------------------------------------------- public
@app.get("/api/health")
def health():
    return {
        "status": "online",
        "service": "BharatApex AI",
        "hindsight_mode": hindsight_service.mode,
        "llm": "groq" if support_agent.llm_router.agentic_available else "local",
        "band_ai": band_service.get_status(),
    }



STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def serve_frontend():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"),
                            headers={"Cache-Control": "no-store"})
