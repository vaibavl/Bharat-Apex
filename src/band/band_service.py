import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Try to import Band REST client safely
try:
    from band_rest import RestClient
    from band_rest.types import ChatMessageRequest, ChatMessageRequestMentionsItem
    BAND_REST_AVAILABLE = True
except ImportError:
    RestClient = None
    ChatMessageRequest = None
    ChatMessageRequestMentionsItem = None
    BAND_REST_AVAILABLE = False



class BandAIService:
    """Band AI (https://www.band.ai) integration service.

    Enables the BharatApex banking agent to interact within the Band AI
    Desktop & multi-agent collaboration network as required by the hackathon.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        agent_id: Optional[str] = None,
        auto_poll: bool = True,
    ):
        self.api_key = api_key or os.getenv("BAND_API_KEY", "").strip()
        self.agent_id = agent_id or os.getenv("BAND_AGENT_ID", "").strip()
        self.auto_poll = auto_poll and os.getenv("BAND_AUTO_POLL", "1") == "1"

        self.client: Optional[Any] = None
        self.connected = False
        self.agent_metadata: Dict[str, Any] = {}
        self.last_poll_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.processed_count = 0

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        self._init_client()

    def _init_client(self) -> None:
        """Initialize the Band REST client and fetch agent metadata."""
        if not self.api_key or not self.agent_id:
            logger.info("Band AI credentials not provided. Running in standalone mode.")
            return

        if not BAND_REST_AVAILABLE:
            self.last_error = "band_rest package is not available"
            logger.warning(self.last_error)
            return

        try:
            self.client = RestClient(api_key=self.api_key)
            me_resp = self.client.agent_api_identity.get_agent_me()
            data = getattr(me_resp, "data", None)
            if data:
                self.agent_metadata = {
                    "id": getattr(data, "id", self.agent_id),
                    "name": getattr(data, "name", "Apex Fintech Agent"),
                    "handle": getattr(data, "handle", "apex-fintech-agent"),
                    "description": getattr(data, "description", ""),
                    "avatar_url": getattr(data, "avatar_url", ""),
                    "owner_uuid": getattr(data, "owner_uuid", ""),
                    "tags": getattr(data, "tags", ["fintech"]),
                }
                self.connected = True
                self.last_error = None
                logger.info(
                    f"Connected to Band AI Agent: {self.agent_metadata.get('name')} "
                    f"({self.agent_metadata.get('handle')})"
                )
        except Exception as e:
            self.connected = False
            self.last_error = str(e)
            logger.warning(f"Failed to connect to Band AI: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Return full status payload for APIs and frontend displays."""
        return {
            "configured": bool(self.api_key and self.agent_id),
            "connected": self.connected,
            "agent_id": self.agent_metadata.get("id", self.agent_id or "Not Configured"),
            "agent_name": self.agent_metadata.get("name", "Apex Fintech Agent"),
            "handle": self.agent_metadata.get("handle", "darshannn2007/apex-fintech-agent"),
            "avatar_url": self.agent_metadata.get("avatar_url", ""),
            "description": self.agent_metadata.get("description", ""),
            "desktop_url": "https://www.band.ai",
            "last_poll_at": self.last_poll_at,
            "processed_count": self.processed_count,
            "last_error": self.last_error,
            "auto_poll_active": bool(self._worker_thread and self._worker_thread.is_alive()),
        }

    def list_chats(self) -> List[Dict[str, Any]]:
        """List active rooms / chats where this agent is a participant."""
        if not self.connected or not self.client:
            return []
        try:
            resp = self.client.agent_api_chats.list_agent_chats()
            chats = getattr(resp, "data", [])
            out = []
            for c in chats:
                out.append({
                    "id": getattr(c, "id", None),
                    "title": getattr(c, "title", "Untitled Chat"),
                    "status": getattr(c, "status", "active"),
                    "updated_at": str(getattr(c, "updated_at", "")),
                })
            return out
        except Exception as e:
            logger.error(f"Error listing Band AI chats: {e}")
            return []

    def send_message(self, chat_id: str, content: str, mention_target: Optional[Dict[str, str]] = None) -> bool:
        """Send a message to a Band AI chat room."""
        if not self.connected or not self.client or not ChatMessageRequest:
            return False
        try:
            mentions = []
            if mention_target and ChatMessageRequestMentionsItem:
                mentions.append(ChatMessageRequestMentionsItem(
                    id=mention_target.get("id", "9a657531-6fc1-4d14-96b7-00aece638d94"),
                    name=mention_target.get("name", "User"),
                    handle=mention_target.get("handle", "user"),
                    kind="mention",
                ))
            elif ChatMessageRequestMentionsItem:
                # Default to room owner/Darshan
                mentions.append(ChatMessageRequestMentionsItem(
                    id="9a657531-6fc1-4d14-96b7-00aece638d94",
                    name="Darshan",
                    handle="darshannn2007",
                    kind="mention",
                ))

            msg_req = ChatMessageRequest(content=content, mentions=mentions)
            self.client.agent_api_messages.create_agent_chat_message(
                chat_id=chat_id,
                message=msg_req,
            )
            return True
        except Exception as e:
            logger.error(f"Error sending Band AI message to {chat_id}: {e}")
            return False

    def poll_messages(
        self,
        handler: Optional[Callable[[str, str], str]] = None,
    ) -> List[Dict[str, Any]]:
        """Poll all active chat rooms for pending messages and respond.

        `handler` signature: handler(message_text, customer_id) -> reply_text.
        If handler is None, a default banking agent call is used.
        """
        if not self.connected or not self.client:
            return []

        self.last_poll_at = datetime.now(timezone.utc).isoformat()
        processed = []

        try:
            chats_resp = self.client.agent_api_chats.list_agent_chats()
            chats = getattr(chats_resp, "data", []) or []

            for chat in chats:
                chat_id = getattr(chat, "id", None)
                if not chat_id:
                    continue

                # Check if there is a next message for this agent
                try:
                    next_resp = self.client.agent_api_messages.get_agent_next_message(chat_id=chat_id)
                    msg = getattr(next_resp, "data", None)
                    if not msg:
                        continue

                    msg_id = getattr(msg, "id", None)
                    raw_content = getattr(msg, "content", "")
                    sender_id = getattr(msg, "sender_id", "")
                    sender_name = getattr(msg, "sender_name", "User") or "User"
                    sender_handle = getattr(msg, "sender_handle", "") or sender_name.lower().replace(" ", "")

                    # Avoid replying to self
                    if sender_id == self.agent_id or not raw_content:
                        continue

                    # Mark message as processing
                    try:
                        self.client.agent_api_messages.mark_agent_message_processing(
                            chat_id=chat_id, id=msg_id
                        )
                    except Exception:
                        pass

                    # Report activity
                    try:
                        self.client.agent_api_activity.report_agent_chat_activity(
                            chat_id=chat_id, working=True
                        )
                    except Exception:
                        pass

                    # Clean prompt text (strip @[[uuid]] or @agent mention syntax)
                    clean_prompt = re.sub(r"@\[\[.*?\]\]", "", raw_content)
                    clean_prompt = re.sub(r"@[\w\s-]+", "", clean_prompt).strip()
                    if not clean_prompt:
                        clean_prompt = "Hello"

                    # Process via handler (or support_agent)
                    reply_text = ""
                    if handler:
                        reply_text = handler(clean_prompt, "CUST-IN-101")
                    else:
                        from ..agent.orchestrator import support_agent
                        res = support_agent.process_message(
                            customer_id="CUST-IN-101",
                            message=clean_prompt,
                            use_memory=True,
                        )
                        reply_text = res.get("reply", "Understood. BharatApex AI is processing your request.")

                    # Format reply with mention to sender
                    mentions = []
                    if ChatMessageRequestMentionsItem and sender_id:
                        mentions.append(ChatMessageRequestMentionsItem(
                            id=sender_id,
                            name=sender_name,
                            handle=sender_handle,
                            kind="mention",
                        ))
                        # Prepend mention tag if not already there
                        mention_prefix = f"@[[{sender_id}]] "
                        if not reply_text.startswith(mention_prefix):
                            reply_text = f"{mention_prefix}{reply_text}"

                    # Send reply message to room
                    msg_req = ChatMessageRequest(content=reply_text, mentions=mentions)
                    self.client.agent_api_messages.create_agent_chat_message(
                        chat_id=chat_id,
                        message=msg_req,
                    )

                    # Mark message as processed
                    try:
                        self.client.agent_api_messages.mark_agent_message_processed(
                            chat_id=chat_id, id=msg_id
                        )
                    except Exception:
                        pass

                    # Clear working activity
                    try:
                        self.client.agent_api_activity.report_agent_chat_activity(
                            chat_id=chat_id, working=False
                        )
                    except Exception:
                        pass

                    self.processed_count += 1
                    processed.append({
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "prompt": clean_prompt,
                        "reply": reply_text,
                    })

                except Exception as ex:
                    logger.debug(f"Chat {chat_id} poll note: {ex}")
                    continue


        except Exception as e:
            err_msg = str(e)
            self.last_error = err_msg
            logger.debug(f"Band AI poll error: {e}")
            # Recreate client on connection drops/pool disconnects
            if "disconnect" in err_msg.lower() or "connection" in err_msg.lower():
                try:
                    self.client = RestClient(api_key=self.api_key)
                except Exception:
                    pass

        return processed

    def start_background_worker(self, interval_seconds: int = 12) -> None:
        """Start a lightweight daemon thread to poll Band AI rooms."""
        if not self.connected or (self._worker_thread and self._worker_thread.is_alive()):
            return

        self._stop_event.clear()

        def _worker_loop():
            logger.info("Band AI background polling worker started.")
            while not self._stop_event.is_set():
                try:
                    self.poll_messages()
                except Exception as e:
                    logger.debug(f"Background poll tick exception: {e}")
                self._stop_event.wait(interval_seconds)
            logger.info("Band AI background worker stopped.")

        self._worker_thread = threading.Thread(
            target=_worker_loop,
            daemon=True,
            name="BandAIPoller",
        )
        self._worker_thread.start()

    def stop_background_worker(self) -> None:
        """Stop background polling thread."""
        self._stop_event.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=3)
            self._worker_thread = None


# Global singleton instance
band_service = BandAIService()
