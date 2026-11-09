"""
Local Hindsight Memory Engine.
High-fidelity offline implementation of Vectorize Hindsight memory system.

Supports:
- Multi-tenant memory banks (isolated per bank_id / customer)
- Retain: Entity extraction, category tagging, temporal stamping
- Recall: Multi-strategy scoring (semantic keywords, entity matching, temporal recency)
- Reflect: Disposition modeling and directive generation
- Persistence across sessions via JSON storage
"""

import os
import json
import uuid
import re
import math
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from .types import RecallResult, RecallResponse, ReflectResponse

# FIX: Store persistent data in a 'data/' directory at the project root,
# not inside the source tree (avoids git pollution and read-only deploy issues).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
os.makedirs(_DATA_DIR, exist_ok=True)
STORAGE_FILE = os.path.join(_DATA_DIR, "hindsight_local_banks.json")


class LocalHindsightEngine:
    """Offline-capable Hindsight memory engine with TEMPR-style recall."""

    def __init__(self, storage_path: str = STORAGE_FILE):
        self.storage_path = storage_path
        self.banks: Dict[str, List[Dict[str, Any]]] = {}
        self._load()

    # ------------------------------------------------------------------ I/O
    def _load(self):
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    self.banks = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.banks = {}
        else:
            self.banks = {}

    def _save(self):
        try:
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(self.banks, f, indent=2)
        except IOError as e:
            print(f"[HindsightEngine] Warning saving storage: {e}")

    # --------------------------------------------------------------- RETAIN
    def retain(
        self,
        bank_id: str,
        content: str,
        timestamp: Optional[str] = None,
        context: Optional[str] = None,
        tags: Optional[List[str]] = None,
        entities: Optional[List[Dict[str, str]]] = None,
        memory_type: str = "experience",
    ) -> Dict[str, Any]:
        """
        Store a structured fact in the specified memory bank.
        Auto-extracts monetary amounts and card references as entities.
        Auto-tags content with relevant FinTech categories.
        """
        if bank_id not in self.banks:
            self.banks[bank_id] = []

        now_str = timestamp or datetime.now(timezone.utc).isoformat()
        unit_id = f"mem_{uuid.uuid4().hex[:10]}"

        # --- Auto-extract FinTech entities ---
        detected_entities = list(entities or [])
        for amt in re.findall(r"\$\d+(?:,\d{3})*(?:\.\d{2})?", content):
            detected_entities.append({"text": amt, "type": "MONETARY_AMOUNT"})
        for card_last4 in re.findall(
            r"(?:card|ending in|ending with)\s*(?:\*+|#)?(\d{4})",
            content,
            re.IGNORECASE,
        ):
            detected_entities.append(
                {"text": f"Card ending in {card_last4}", "type": "PAYMENT_METHOD"}
            )

        # --- Auto-tag content ---
        auto_tags = list(tags or [])
        lower = content.lower()
        tag_rules = {
            "wire_transfer": ("wire", "swift"),
            "dispute": ("dispute", "chargeback", "fraud"),
            "fees": ("fee", "waiver", "overdraft"),
            "security_hold": ("hold", "pending"),
            "travel": ("travel", "foreign"),
        }
        for tag_name, keywords in tag_rules.items():
            if any(kw in lower for kw in keywords):
                auto_tags.append(tag_name)
        auto_tags = list(set(auto_tags))

        memory = {
            "id": unit_id,
            "fact": content.strip(),
            "timestamp": now_str,
            "type": memory_type,
            "tags": auto_tags,
            "entities": detected_entities,
            "context": context or "customer_support_turn",
        }

        # Prevent exact-duplicate facts in the same bank
        if not any(
            m["fact"].lower() == content.strip().lower() for m in self.banks[bank_id]
        ):
            self.banks[bank_id].append(memory)
            self._save()

        return {"status": "success", "id": unit_id, "bank_id": bank_id}

    # --------------------------------------------------------------- RECALL
    def recall(
        self,
        bank_id: str,
        query: str,
        max_results: int = 5,
        types: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
    ) -> RecallResponse:
        """
        Recall relevant memories using multi-strategy scoring:
        1. Token-overlap similarity (TF-IDF style)
        2. FinTech domain-keyword boosting
        3. Tag-match boosting
        4. Temporal recency boost
        """
        memories = self.banks.get(bank_id, [])
        if not memories:
            return RecallResponse(
                results=[], query=query, bank_id=bank_id, total_memories_searched=0
            )

        query_tokens = set(re.findall(r"\w+", query.lower()))
        scored: List[RecallResult] = []
        now = datetime.now(timezone.utc)

        fintech_keywords = {
            "wire", "zurich", "hold", "fee", "waiver", "card",
            "dispute", "batch", "settlement", "travel", "overdraft",
        }

        for mem in memories:
            if types and mem.get("type") not in types:
                continue
            if tags:
                mem_tags_set = set(mem.get("tags", []))
                if not mem_tags_set.intersection(set(tags)):
                    continue

            fact_text = mem.get("fact", "")
            fact_tokens = set(re.findall(r"\w+", fact_text.lower()))

            # 1. Token overlap
            overlap = query_tokens & fact_tokens
            denom = math.sqrt(len(query_tokens) + 1) * math.sqrt(len(fact_tokens) + 1)
            score = len(overlap) / denom if query_tokens else 0.0

            # 2. FinTech keyword boost
            for kw in fintech_keywords:
                if kw in query_tokens and kw in fact_tokens:
                    score += 0.25

            # 3. Tag match boost
            for t in mem.get("tags", []):
                if t.lower() in query.lower():
                    score += 0.2

            # 4. Temporal recency boost (memories < 90 days old get up to +0.15)
            try:
                ts = mem.get("timestamp", "").replace("Z", "+00:00")
                mem_date = datetime.fromisoformat(ts)
                days_ago = max(0, (now - mem_date).days)
                score += max(0.0, 0.15 * (1.0 - days_ago / 90.0))
            except (ValueError, TypeError):
                pass

            if score > 0.05 or len(memories) <= 3:
                scored.append(
                    RecallResult(
                        fact=fact_text,
                        score=round(score, 4),
                        type=mem.get("type", "experience"),
                        timestamp=mem.get("timestamp", ""),
                        tags=mem.get("tags", []),
                        entities=mem.get("entities", []),
                    )
                )

        scored.sort(key=lambda r: r.score, reverse=True)
        return RecallResponse(
            results=scored[:max_results],
            query=query,
            bank_id=bank_id,
            total_memories_searched=len(memories),
        )

    # -------------------------------------------------------------- REFLECT
    def reflect(
        self,
        bank_id: str,
        query: str,
        context: Optional[str] = None,
    ) -> ReflectResponse:
        """
        Synthesize agent disposition, emotional context, and behavioural
        directives from the full history in a customer's memory bank.
        """
        recall_resp = self.recall(bank_id, query, max_results=6)
        based_on = [r.fact for r in recall_resp.results]

        all_memories = self.banks.get(bank_id, [])
        all_text = " ".join(m.get("fact", "") for m in all_memories).lower()

        # --- Disposition analysis ---
        frustration_detected = any(
            kw in all_text for kw in ("frustrated", "anxious", "disputed")
        )
        communication_style = (
            "Concise & Executive-level (no fluff)"
            if any(kw in all_text for kw in ("concise", "executive"))
            else "Professional & Direct"
        )
        vip_status = any(
            kw in all_text for kw in ("vip", "premier", "high-yield")
        )

        # --- Directives ---
        directives: List[str] = []
        if frustration_detected:
            directives.append(
                "Prioritize immediate problem resolution without making "
                "customer repeat prior case facts."
            )
        if "wire" in all_text and ("zurich" in all_text or "hold" in all_text):
            directives.append(
                "Customer previously experienced a Zurich wire hold; "
                "proactively clarify verification status."
            )
        if "fee_waiver" in all_text or "courtesy_used" in all_text:
            directives.append(
                "Check past fee waiver history before committing to any "
                "courtesy credit."
            )
        if "batch_settlement" in all_text:
            directives.append(
                "Directly confirm exact clearing timestamps for weekend "
                "merchant batch settlements."
            )

        disposition = {
            "customer_loyalty": "High Tier" if vip_status else "Standard",
            "patience_level": (
                "Low (prior friction)" if frustration_detected else "Normal"
            ),
            "preferred_style": communication_style,
            "known_pain_points": [
                r.fact
                for r in recall_resp.results
                if any(
                    kw in r.fact.lower()
                    for kw in ("frustrated", "dispute", "hold")
                )
            ],
        }

        answer = (
            f"Synthesized context from {len(based_on)} past memories: "
            f"Customer appreciates {communication_style}."
        )
        if directives:
            answer += f" Key Directive: {directives[0]}"

        return ReflectResponse(
            answer=answer,
            based_on=based_on,
            disposition=disposition,
            directives=directives,
        )

    # ------------------------------------------------------------ UTILITIES
    def get_all_memories(self, bank_id: str) -> List[Dict[str, Any]]:
        """Return every memory stored in a bank."""
        return self.banks.get(bank_id, [])

    def reset_bank(self, bank_id: str):
        """Delete all memories in a bank and persist the change."""
        if bank_id in self.banks:
            del self.banks[bank_id]
            self._save()
