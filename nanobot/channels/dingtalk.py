"""DingTalk/DingDing channel implementation using Stream Mode."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger
import httpx

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import DingTalkConfig

try:
    from dingtalk_stream import (
        DingTalkStreamClient,
        Credential,
        CallbackHandler,
        CallbackMessage,
        AckMessage,
    )
    from dingtalk_stream.chatbot import ChatbotMessage

    DINGTALK_AVAILABLE = True
except ImportError:
    DINGTALK_AVAILABLE = False
    # Fallback so class definitions don't crash at module level
    CallbackHandler = object  # type: ignore[assignment,misc]
    CallbackMessage = None  # type: ignore[assignment,misc]
    AckMessage = None  # type: ignore[assignment,misc]
    ChatbotMessage = None  # type: ignore[assignment,misc]


class NanobotDingTalkHandler(CallbackHandler):
    """
    Standard DingTalk Stream SDK Callback Handler.
    Parses incoming messages and forwards them to the Nanobot channel.
    """

    def __init__(self, channel: "DingTalkChannel"):
        super().__init__()
        self.channel = channel

    async def process(self, message: CallbackMessage):
        """Process incoming stream message."""
        try:
            # Parse using SDK's ChatbotMessage for robust handling
            chatbot_msg = ChatbotMessage.from_dict(message.data)
            content, media_paths, msg_type = await self.channel._extract_inbound_message(chatbot_msg, message.data)
            sender_id = str(chatbot_msg.sender_staff_id or chatbot_msg.sender_id or "unknown")
            sender_name = str(chatbot_msg.sender_nick or "Unknown")

            if not content and not media_paths:
                logger.warning("Received empty or unsupported DingTalk message type: {}", msg_type)
                return AckMessage.STATUS_OK, "OK"

            logger.info(
                "Received DingTalk {} from {} ({}), media={}",
                msg_type,
                sender_name,
                sender_id,
                len(media_paths),
            )

            # Forward to Nanobot via _on_message (non-blocking).
            # Store reference to prevent GC before task completes.
            task = asyncio.create_task(
                self.channel._on_message(
                    content,
                    sender_id,
                    sender_name,
                    media=media_paths,
                    metadata={"msg_type": msg_type},
                )
            )
            self.channel._background_tasks.add(task)
            task.add_done_callback(self.channel._background_tasks.discard)

            return AckMessage.STATUS_OK, "OK"

        except Exception as e:
            logger.error("Error processing DingTalk message: {}", e)
            # Return OK to avoid retry loop from DingTalk server
            return AckMessage.STATUS_OK, "Error"


class DingTalkChannel(BaseChannel):
    """
    DingTalk channel using Stream Mode.

    Uses WebSocket to receive events via `dingtalk-stream` SDK.
    Uses direct HTTP API to send messages (SDK is mainly for receiving).

    Note: Currently only supports private (1:1) chat. Group messages are
    received but replies are sent back as private messages to the sender.
    """

    name = "dingtalk"

    def __init__(self, config: DingTalkConfig, bus: MessageBus, groq_api_key: str = ""):
        super().__init__(config, bus)
        self.config: DingTalkConfig = config
        self._client: Any = None
        self._http: httpx.AsyncClient | None = None
        self.groq_api_key: str = groq_api_key

        # Access Token management for sending messages
        self._access_token: str | None = None
        self._token_expiry: float = 0

        # Hold references to background tasks to prevent GC
        self._background_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Start the DingTalk bot with Stream Mode."""
        try:
            if not DINGTALK_AVAILABLE:
                logger.error(
                    "DingTalk Stream SDK not installed. Run: pip install dingtalk-stream"
                )
                return

            if not self.config.client_id or not self.config.client_secret:
                logger.error("DingTalk client_id and client_secret not configured")
                return

            self._running = True
            self._http = httpx.AsyncClient()

            logger.info(
                "Initializing DingTalk Stream Client with Client ID: {}...",
                self.config.client_id,
            )
            credential = Credential(self.config.client_id, self.config.client_secret)
            self._client = DingTalkStreamClient(credential)

            # Register standard handler
            handler = NanobotDingTalkHandler(self)
            self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)

            logger.info("DingTalk bot started with Stream Mode")

            # Reconnect loop: restart stream if SDK exits or crashes
            while self._running:
                try:
                    await self._client.start()
                except Exception as e:
                    logger.warning("DingTalk stream error: {}", e)
                if self._running:
                    logger.info("Reconnecting DingTalk stream in 5 seconds...")
                    await asyncio.sleep(5)

        except Exception as e:
            logger.exception("Failed to start DingTalk channel: {}", e)

    async def stop(self) -> None:
        """Stop the DingTalk bot."""
        self._running = False
        # Close the shared HTTP client
        if self._http:
            await self._http.aclose()
            self._http = None
        # Cancel outstanding background tasks
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()

    @staticmethod
    def _media_workspace_dir() -> Path:
        media_dir = Path.home() / ".nanobot" / "workspace" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        return media_dir

    @staticmethod
    def _safe_text(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _extract_message_type(chatbot_msg: Any, raw_data: dict[str, Any]) -> str:
        msg_type = (
            getattr(chatbot_msg, "message_type", None)
            or raw_data.get("messageType")
            or raw_data.get("msgtype")
            or raw_data.get("msg_type")
            or "unknown"
        )
        return str(msg_type).strip().lower()

    @classmethod
    def _extract_text_content(cls, chatbot_msg: Any, raw_data: dict[str, Any]) -> str:
        text_obj = getattr(chatbot_msg, "text", None)
        if text_obj and getattr(text_obj, "content", None):
            return cls._safe_text(text_obj.content)
        return cls._safe_text(raw_data.get("text", {}).get("content", ""))

    @classmethod
    def _find_first_url(cls, obj: Any) -> str:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_l = str(key).lower()
                if isinstance(value, str):
                    v = value.strip()
                    if v.startswith(("http://", "https://")) and (
                        "url" in key_l or "download" in key_l or "media" in key_l
                    ):
                        return v
                found = cls._find_first_url(value)
                if found:
                    return found
            return ""
        if isinstance(obj, list):
            for item in obj:
                found = cls._find_first_url(item)
                if found:
                    return found
        return ""

    @classmethod
    def _find_first_transcript(cls, obj: Any) -> str:
        keys = {"recognition", "speechtext", "transcript", "asrtext", "asr_text"}
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() in keys and isinstance(value, str) and value.strip():
                    return value.strip()
                found = cls._find_first_transcript(value)
                if found:
                    return found
            return ""
        if isinstance(obj, list):
            for item in obj:
                found = cls._find_first_transcript(item)
                if found:
                    return found
        return ""

    async def _download_voice_media(self, url: str) -> str | None:
        if not url:
            return None

        parsed = urlparse(url)
        ext = Path(parsed.path).suffix.lower() or ".amr"
        filename = f"dingtalk_voice_{int(time.time() * 1000)}{ext}"
        file_path = self._media_workspace_dir() / filename

        created_client = False
        client = self._http
        if client is None:
            client = httpx.AsyncClient()
            created_client = True

        try:
            head = await client.head(url, timeout=15.0)
            if head.status_code < 400:
                content_length = int(head.headers.get("content-length", "0") or "0")
                if content_length and content_length > self.config.max_download_bytes:
                    logger.warning(
                        "DingTalk voice download skipped: {} > {} bytes",
                        content_length,
                        self.config.max_download_bytes,
                    )
                    return None

            resp = await client.get(url, timeout=30.0)
            if resp.status_code >= 400:
                logger.warning("DingTalk voice download failed: status={} url={}", resp.status_code, url)
                return None
            if len(resp.content) > self.config.max_download_bytes:
                logger.warning(
                    "DingTalk voice download skipped after fetch: {} > {} bytes",
                    len(resp.content),
                    self.config.max_download_bytes,
                )
                return None
            file_path.write_bytes(resp.content)
            return str(file_path)
        except Exception as e:
            logger.warning("DingTalk voice download error: {}", e)
            return None
        finally:
            if created_client:
                await client.aclose()

    async def _transcribe_voice(self, media_path: str) -> str:
        if not self.config.enable_voice_transcription:
            return ""
        try:
            from nanobot.providers.transcription import GroqTranscriptionProvider

            provider = GroqTranscriptionProvider(api_key=self.groq_api_key)
            return await provider.transcribe(media_path)
        except Exception as e:
            logger.warning("DingTalk voice transcription failed: {}", e)
            return ""

    async def _extract_inbound_message(self, chatbot_msg: Any, raw_data: dict[str, Any]) -> tuple[str, list[str], str]:
        """Extract text/media from inbound DingTalk event with fail-open behavior."""
        msg_type = self._extract_message_type(chatbot_msg, raw_data)
        content_parts: list[str] = []
        media_paths: list[str] = []

        text_content = self._extract_text_content(chatbot_msg, raw_data)
        if text_content:
            content_parts.append(text_content)

        if not self.config.enable_media_receive:
            content = "\n".join([p for p in content_parts if p]).strip()
            return content, media_paths, msg_type

        voice_types = {"audio", "voice", "audio_message"}
        if msg_type in voice_types:
            voice_url = self._find_first_url(raw_data)
            transcript = self._find_first_transcript(raw_data)

            if voice_url:
                media_path = await self._download_voice_media(voice_url)
                if media_path:
                    media_paths.append(media_path)
                    if transcript:
                        content_parts.append(f"[transcription: {transcript}]")
                    else:
                        auto_transcript = await self._transcribe_voice(media_path)
                        if auto_transcript:
                            content_parts.append(f"[transcription: {auto_transcript}]")
                        else:
                            content_parts.append(f"[voice: {media_path}]")
                else:
                    content_parts.append("[voice message received: download failed]")
            else:
                if transcript:
                    content_parts.append(f"[transcription: {transcript}]")
                else:
                    content_parts.append("[voice message received]")

        content = "\n".join([p for p in content_parts if p]).strip()
        return content, media_paths, msg_type

    async def _get_access_token(self) -> str | None:
        """Get or refresh Access Token."""
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        data = {
            "appKey": self.config.client_id,
            "appSecret": self.config.client_secret,
        }

        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot refresh token")
            return None

        try:
            resp = await self._http.post(url, json=data)
            resp.raise_for_status()
            res_data = resp.json()
            self._access_token = res_data.get("accessToken")
            # Expire 60s early to be safe
            self._token_expiry = time.time() + int(res_data.get("expireIn", 7200)) - 60
            return self._access_token
        except Exception as e:
            logger.error("Failed to get DingTalk access token: {}", e)
            return None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through DingTalk."""
        token = await self._get_access_token()
        if not token:
            return

        # oToMessages/batchSend: sends to individual users (private chat)
        # https://open.dingtalk.com/document/orgapp/robot-batch-send-messages
        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"

        headers = {"x-acs-dingtalk-access-token": token}

        data = {
            "robotCode": self.config.client_id,
            "userIds": [msg.chat_id],  # chat_id is the user's staffId
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({
                "text": msg.content,
                "title": "Nanobot Reply",
            }, ensure_ascii=False),
        }

        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot send")
            return

        try:
            resp = await self._http.post(url, json=data, headers=headers)
            if resp.status_code != 200:
                logger.error("DingTalk send failed: {}", resp.text)
            else:
                logger.debug("DingTalk message sent to {}", msg.chat_id)
        except Exception as e:
            logger.error("Error sending DingTalk message: {}", e)

    async def _on_message(
        self,
        content: str,
        sender_id: str,
        sender_name: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Handle incoming message (called by NanobotDingTalkHandler).

        Delegates to BaseChannel._handle_message() which enforces allow_from
        permission checks before publishing to the bus.
        """
        try:
            logger.info("DingTalk inbound: {} from {}", content, sender_name)
            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender_id,  # For private chat, chat_id == sender_id
                content=str(content),
                media=media or [],
                metadata={
                    "sender_name": sender_name,
                    "platform": "dingtalk",
                    **(metadata or {}),
                },
            )
        except Exception as e:
            logger.error("Error publishing DingTalk message: {}", e)
