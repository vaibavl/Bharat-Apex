"""
Hindsight Manager — Unified wrapper for Vectorize Hindsight Cloud
and the local offline engine.

If HINDSIGHT_API_KEY / HINDSIGHT_BASE_URL are set, it connects to the
Vectorize Hindsight Cloud service.  Otherwise it runs the built-in
local engine with zero external dependencies.
"""

import os
import asyncio
import threading
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from .types import RecallResult, RecallResponse, ReflectResponse
from .engine import LocalHindsightEngine
from ..synthetic_data.data_loader import get_all_customers


class HindsightManager:
    """Unified Hindsight Cloud + Local Engine manager."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.base_url = base_url or os.getenv("HINDSIGHT_BASE_URL")
        self.api_key = api_key or os.getenv("HINDSIGHT_API_KEY")
        self.local_engine = LocalHindsightEngine()
        self.client = None
        self.mode = "local"
        self._loop = None
        self._loop_thread = None

        # Try connecting to Hindsight Cloud if credentials are provided
        if self.base_url or self.api_key:
            try:
                from hindsight_client import Hindsight  # type: ignore

                url = self.base_url or "https://api.hindsight.vectorize.io"
                self.client = Hindsight(base_url=url, api_key=self.api_key)
                self.mode = "cloud"
                self._start_cloud_loop()
                print(f"[HindsightManager] Connected to remote server: {url}")
            except Exception as e:
                print(
                    f"[HindsightManager] Remote connection failed: {e}. "
                    "Falling back to Local Engine."
                )
                self.client = None
                self.mode = "local"

        # Auto-seed memory banks with synthetic customer history
        self._bootstrap_synthetic_memories()

    # ----------------------------------------------------- cloud loop
    def _start_cloud_loop(self) -> None:
        """Pin all SDK calls to ONE dedicated event-loop thread.

        The hindsight SDK builds an internal httpx AsyncClient that gets bound
        to whichever event loop first touches it. Uvicorn rotates FastAPI worker
        threads (each with its own fresh loop), so SDK sync wrappers called
        there fail intermittently with 'Timeout context manager should be used
        inside a task'. Run every cloud coroutine on this private loop instead.
        """
        self._loop = asyncio.new_event_loop()

        def _spin(loop: asyncio.AbstractEventLoop) -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_spin, args=(self._loop,), daemon=True, name="hindsight-cloud-loop")
        self._loop_thread.start()

    def _cloud(self, coro, timeout: float = 90.0):
        """await a Hindsight coroutine on the dedicated cloud loop (blocking call)."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    # ----------------------------------------------------- Bootstrap
    def _bootstrap_synthetic_memories(self):
        """Pre-populate memory banks with historical customer interactions."""
        for cust in get_all_customers():
            bank_id = f"bank_{cust['id']}"
            if self.local_engine.get_all_memories(bank_id):
                continue  # already seeded
            for mem in cust.get("historical_memories", []):
                self.local_engine.retain(
                    bank_id=bank_id,
                    content=mem["fact"],
                    timestamp=mem.get("timestamp"),
                    tags=mem.get("tags", []),
                    memory_type=mem.get("type", "experience"),
                )

    # --------------------------------------------------------- RETAIN
    def retain(
        self,
        bank_id: str,
        content: str,
        context: Optional[str] = None,
        tags: Optional[List[str]] = None,
        timestamp: Optional[str] = None,
        memory_type: str = "experience",
    ) -> Dict[str, Any]:
        """Store a memory fact into the customer's bank."""
        if self.mode == "cloud" and self.client:
            try:
                resp = self._cloud(self.client.aretain(
                    bank_id=bank_id,
                    content=content,
                    context=context,
                    tags=tags,
                    timestamp=timestamp,
                ))
                # Mirror locally for instant inspection
                self.local_engine.retain(
                    bank_id, content, timestamp, context, tags,
                    memory_type=memory_type,
                )
                return {
                    "status": "success",
                    "mode": "cloud",
                    "response": str(resp),
                }
            except Exception as e:
                print(f"[HindsightManager] Cloud retain error: {e}. Using local.")

        return self.local_engine.retain(
            bank_id=bank_id,
            content=content,
            timestamp=timestamp,
            context=context,
            tags=tags,
            memory_type=memory_type,
        )

    # --------------------------------------------------------- RECALL
    def recall(
        self,
        bank_id: str,
        query: str,
        max_results: int = 5,
        types: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
    ) -> RecallResponse:
        """Recall relevant memories for the query."""
        if self.mode == "cloud" and self.client:
            try:
                resp = self._cloud(self.client.arecall(bank_id=bank_id, query=query))
                results = [
                    RecallResult(
                        fact=getattr(item, "fact", str(item)),
                        score=getattr(item, "score", 0.9),
                        type=getattr(item, "type", "experience"),
                        timestamp=getattr(
                            item, "timestamp",
                            datetime.now(timezone.utc).isoformat(),
                        ),
                        tags=getattr(item, "tags", []),
                    )
                    for item in getattr(resp, "results", [])
                ]
                return RecallResponse(
                    results=results,
                    query=query,
                    bank_id=bank_id,
                    total_memories_searched=len(results),
                )
            except Exception as e:
                print(f"[HindsightManager] Cloud recall error: {e}. Using local.")

        return self.local_engine.recall(
            bank_id=bank_id,
            query=query,
            max_results=max_results,
            types=types,
            tags=tags,
        )

    # -------------------------------------------------------- REFLECT
    def reflect(
        self,
        bank_id: str,
        query: str,
        context: Optional[str] = None,
    ) -> ReflectResponse:
        """Synthesize customer disposition and behavioural directives."""
        if self.mode == "cloud" and self.client:
            try:
                resp = self._cloud(self.client.areflect(
                    bank_id=bank_id, query=query, context=context,
                ))
                based_on = getattr(resp, "based_on", [])
                answer = getattr(resp, "answer", str(resp))
                return ReflectResponse(
                    answer=answer,
                    based_on=based_on,
                    disposition={"status": "cloud_reflection"},
                    directives=[answer],
                )
            except Exception as e:
                print(f"[HindsightManager] Cloud reflect error: {e}. Using local.")

        return self.local_engine.reflect(
            bank_id=bank_id, query=query, context=context,
        )

    # ------------------------------------------------------- HELPERS
    def get_all_memories(self, bank_id: str) -> List[Dict[str, Any]]:
        return self.local_engine.get_all_memories(bank_id)

    def reset_bank(self, bank_id: str):
        self.local_engine.reset_bank(bank_id)
        self._bootstrap_synthetic_memories()


# ---- Global singleton (initialised once at import time) ----
hindsight_service = HindsightManager()
