"""
HTTP Client - synchronous request/reply against an OpenAI-compatible agent API

For every inbound LoRa message the bridge POSTs to the agent's chat completions
endpoint with Bearer auth, reads `choices[0].message.content` back, and returns
that text as the reply to transmit over LoRa.

Replies must fit the LoRa byte budget, so the budget travels with the request (in
the system prompt and as a one-line reminder on the user message) and a reply that
still overshoots gets one rewrite attempt before it is truncated. The rewrite
costs an extra API round trip but no radio airtime, which is the trade this
bridge wants: airtime is a shared, congested resource, tokens are not.

Sessions: `X-Hermes-Session-Id` is set per mesh node, so the agent keeps one
conversation per node instead of starting from scratch on every message.

No HMAC, no callback endpoint: one request, one reply.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp

from .text import truncate_bytes

logger = logging.getLogger(__name__)

# Appended to the user message so the budget is stated next to the question, not
# only in the system prompt (which the model ignored in practice).
BUDGET_REMINDER = (
    "\n\n[reply budget: {max_bytes} bytes of plain text; "
    "if the full answer does not fit, give the short answer plus a pointer]"
)

# Asked when the first reply overshoots the budget
SHORTEN_INSTRUCTION = (
    "Rewrite your previous answer so the whole reply is under {max_bytes} bytes "
    "of plain text. If it cannot all fit, keep the short version and add a pointer "
    "to the rest. No preamble, no markdown."
)


def session_id_for(prefix: str, from_id: str) -> str:
    """Build a header-safe session id for a mesh node, e.g. meshtastic-68916e4c."""
    safe = "".join(c for c in (from_id or "") if c.isalnum() or c in "-_")
    if not safe:
        return ""
    return f"{prefix}-{safe}" if prefix else safe


class AgentClient:
    """OpenAI-compatible client used to answer inbound LoRa messages."""

    def __init__(
        self,
        url: str,
        api_key: str,
        model: str = "hermes-agent",
        timeout: int = 60,
        system_prompt: str = "",
        max_reply_bytes: int = 200,
        max_retries: int = 1,
        session_prefix: str = "meshtastic",
        shorten_retry: bool = True,
    ):
        self.url = url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.system_prompt = system_prompt
        self.max_reply_bytes = max_reply_bytes
        self.max_retries = max_retries
        self.session_prefix = session_prefix
        self.shorten_retry = shorten_retry

        self._session: Optional[aiohttp.ClientSession] = None
        self._retry_delay = 1.0
        self._max_retry_delay = 60.0
        self._base_url = self._derive_base_url(url)

    @staticmethod
    def _derive_base_url(url: str) -> str:
        """scheme://host:port of the agent API, used for the health probe."""
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
        return ""

    async def start(self) -> None:
        """Start HTTP session."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        )
        logger.info(f"Agent client started, endpoint: {self.url}")
        logger.info(
            f"Reply budget: {self.max_reply_bytes} bytes, "
            f"shorten retry {'on' if self.shorten_retry else 'off'}"
        )

    async def stop(self) -> None:
        """Stop HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("Agent client stopped")

    async def ask(self, text: str, from_id: str) -> Optional[str]:
        """
        Send one LoRa message to the agent and return its reply.

        Args:
            text: Inbound message text
            from_id: Source node ID (e.g. "!68916e4c")

        Returns:
            Reply text (already truncated to the LoRa byte budget), or None when
            the agent could not answer. None means "say nothing on the mesh".
        """
        if not self._session:
            logger.error("Agent client not started")
            return None

        reply = await self._request(self._build_messages(text, from_id), from_id)
        if reply is None:
            return None

        if len(reply.encode("utf-8")) <= self.max_reply_bytes:
            return reply

        if not self.shorten_retry:
            return self._truncate(reply, "over budget")

        # One rewrite attempt: costs tokens, saves 2 to 6 seconds of shared airtime
        shortened = await self._request(
            self._build_shorten_messages(text, reply, from_id), from_id
        )
        if shortened and len(shortened.encode("utf-8")) <= self.max_reply_bytes:
            logger.info(
                f"Agent shortened its reply from {len(reply)} to "
                f"{len(shortened)} chars to fit the LoRa budget"
            )
            return shortened

        return self._truncate(
            shortened or reply, "still over budget after a shorten attempt"
        )

    def _truncate(self, reply: str, reason: str) -> str:
        """Cut to the byte budget, marked, and say why in the log."""
        truncated = truncate_bytes(reply, self.max_reply_bytes)
        logger.warning(
            f"Reply truncated from {len(reply)} to {len(truncated)} chars "
            f"({reason}, limit {self.max_reply_bytes} bytes)"
        )
        return truncated

    async def _request(
        self, messages: List[Dict[str, str]], from_id: str
    ) -> Optional[str]:
        """
        One chat-completions exchange with the configured retry policy.

        Returns the agent's raw text, or None when it could not answer.
        """
        payload = {"model": self.model, "messages": messages}
        headers = self._build_headers(from_id)

        attempt = 0
        while True:
            reply, retryable = await self._post(payload, headers, from_id)

            if reply is not None:
                return reply
            if not retryable or attempt >= self.max_retries:
                return None

            attempt += 1
            logger.info(
                f"Retrying agent request in {self._retry_delay:.1f}s "
                f"(attempt {attempt}/{self.max_retries})"
            )
            await asyncio.sleep(self._retry_delay)
            self._retry_delay = min(self._retry_delay * 2, self._max_retry_delay)

    async def _post(
        self, payload: Dict[str, Any], headers: Dict[str, str], from_id: str
    ) -> Tuple[Optional[str], bool]:
        """
        One request attempt.

        Returns:
            (reply_text, retryable). reply_text None means failure; retryable says
            whether another attempt is worth making.
        """
        session = self._session
        if session is None:
            return None, False

        try:
            async with session.post(
                self.url, json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(
                        f"Agent returned {resp.status}: {body[:200]}"
                    )
                    # 5xx and 429 are transient; 4xx means we are asking wrong
                    return None, resp.status >= 500 or resp.status == 429

                data = await resp.json()
                reply = self._extract_reply(data)
                if reply is None:
                    return None, False

                self._retry_delay = 1.0
                logger.info(f"Agent replied to {from_id}: {reply[:60]}...")
                return reply, False

        except asyncio.TimeoutError:
            logger.error(f"Agent request timed out after {self.timeout}s")
            return None, True
        except aiohttp.ClientError as e:
            logger.error(f"Agent request failed: {e}")
            return None, True
        except Exception as e:
            logger.error(f"Unexpected agent request error: {e}", exc_info=True)
            return None, False

    def _render_prompt(self, template: str, from_id: str) -> str:
        """Fill a prompt template; leave it verbatim if it uses other placeholders."""
        try:
            return template.format(from_id=from_id, max_bytes=self.max_reply_bytes)
        except (KeyError, IndexError, ValueError):
            return template

    def _build_messages(self, text: str, from_id: str) -> List[Dict[str, str]]:
        """Build the messages for the first request, budget included."""
        messages: List[Dict[str, str]] = []
        if self.system_prompt:
            messages.append(
                {
                    "role": "system",
                    "content": self._render_prompt(self.system_prompt, from_id),
                }
            )
        messages.append(
            {
                "role": "user",
                "content": text
                + BUDGET_REMINDER.format(max_bytes=self.max_reply_bytes),
            }
        )
        return messages

    def _build_shorten_messages(
        self, text: str, first_reply: str, from_id: str
    ) -> List[Dict[str, str]]:
        """
        Build the rewrite request.

        The first exchange is replayed so this works even when the agent side does
        not thread sessions; the session header keeps the context on the Hermes side.
        """
        messages = self._build_messages(text, from_id)
        messages.append({"role": "assistant", "content": first_reply})
        messages.append(
            {
                "role": "user",
                "content": SHORTEN_INSTRUCTION.format(max_bytes=self.max_reply_bytes),
            }
        )
        return messages

    def _build_headers(self, from_id: str) -> Dict[str, str]:
        """Build request headers, including the per-node session id."""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        session_id = session_id_for(self.session_prefix, from_id)
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id
        return headers

    @staticmethod
    def _extract_reply(data: Dict[str, Any]) -> Optional[str]:
        """Pull the assistant text out of an OpenAI-compatible response."""
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            logger.error(f"Unexpected agent response shape: {str(data)[:200]}")
            return None

        if not isinstance(content, str) or not content.strip():
            logger.error("Agent returned an empty reply")
            return None

        return content.strip()

    async def check_health(self) -> bool:
        """Check that the agent API is reachable (GET {base}/health, no auth)."""
        if not self._session or not self._base_url:
            return False

        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with self._session.get(
                f"{self._base_url}/health", timeout=timeout
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
