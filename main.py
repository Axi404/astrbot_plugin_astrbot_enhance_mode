import asyncio
import base64
import datetime
import json
import mimetypes
import os
import random
import re
import shlex
import time
import traceback
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from mcp import types as mcp_types

from astrbot.api import llm_tool, logger, sp, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse, Provider, ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.provider.provider import EmbeddingProvider
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.io import download_image_by_url

from .ban_control import BanStore, parse_duration_seconds
from .memory_rag_store import MemoryRAGStore
from .plugin_config import PluginConfig, parse_plugin_config
from .runtime_state import RuntimeState
from .tag_utils import (
    bounded_chat_history_text,
    build_interaction_instructions,
    chain_has_refuse_tag,
    clean_response_text_for_history,
    has_refuse_tag,
    transform_result_chain,
)
from .webui import RAGWebUIServer

IMAGE_MARKER_PATTERN = re.compile(r"\[Image(?:: [^\]]*)?\]")
MSG_ID_PATTERN = re.compile(r"#msg([^:]+):")


class Main(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = config or {}
        self.runtime = RuntimeState()
        self._display_timezone = self._resolve_config_timezone()
        plugin_data_dir = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_astrbot_enhance_mode"
        )
        self.ban_store = BanStore(plugin_data_dir / "ban_list.db")
        self.memory_rag_store: MemoryRAGStore | None = None
        self.rag_webui_server: RAGWebUIServer | None = None
        self._browser_tool_locks: dict[str, asyncio.Lock] = {}
        try:
            self.memory_rag_store = MemoryRAGStore(
                plugin_data_dir / "memory_rag.db",
                display_timezone=self._display_timezone,
            )
        except Exception as e:
            logger.error(f"enhance-mode | 初始化记忆 RAG 存储失败: {e}", exc_info=True)
        logger.info(
            "enhance-mode | plugin initialized | data_dir=%s memory_rag_store_ready=%s timezone=%s",
            plugin_data_dir,
            self.memory_rag_store is not None,
            self._display_timezone,
        )

    def _cfg(self) -> PluginConfig:
        return parse_plugin_config(self.config)

    def _touch_origin(self, origin: str, cfg: PluginConfig) -> None:
        self.runtime.touch_origin(origin, cfg.global_settings.lru_cache.max_origins)

    def _resolve_config_timezone(self) -> str:
        base_cfg = self.context.get_config()
        if not isinstance(base_cfg, dict):
            return "Asia/Shanghai"
        timezone_name = str(base_cfg.get("timezone") or "").strip()
        return timezone_name or "Asia/Shanghai"

    def _resolve_tzinfo(self) -> datetime.tzinfo:
        timezone_name = self._resolve_config_timezone()
        try:
            return ZoneInfo(timezone_name)
        except Exception:
            try:
                return ZoneInfo("Asia/Shanghai")
            except Exception:
                return datetime.timezone.utc

    def _format_timestamp_iso(self, timestamp: float | int) -> str:
        if self.memory_rag_store is not None:
            return self.memory_rag_store.format_timestamp_iso(timestamp)
        return datetime.datetime.fromtimestamp(
            float(timestamp), tz=self._resolve_tzinfo()
        ).isoformat()

    @staticmethod
    def _provider_label(provider: object | None) -> str:
        if provider is None:
            return "none"
        provider_id = getattr(provider, "provider_id", None) or getattr(
            provider, "id", None
        )
        if provider_id:
            return str(provider_id)
        model = getattr(provider, "model", None)
        cls_name = type(provider).__name__
        return f"{cls_name}({model})" if model else cls_name

    @staticmethod
    def _normalize_message_id(raw: str | int | None) -> str:
        text = str(raw or "").strip()
        if text.startswith("#msg"):
            text = text[4:]
        if text.endswith(":"):
            text = text[:-1]
        return text.strip()

    @staticmethod
    def _extract_message_id_from_history_line(line: str) -> str:
        matched = MSG_ID_PATTERN.search(str(line or ""))
        if not matched:
            return ""
        return str(matched.group(1) or "").strip()

    @staticmethod
    def _replace_image_marker_at_index(
        line: str, image_index: int, caption: str
    ) -> tuple[str, bool]:
        if image_index < 0:
            return line, False
        matches = list(IMAGE_MARKER_PATTERN.finditer(line))
        if image_index >= len(matches):
            return line, False

        target = matches[image_index]
        safe_caption = str(caption or "").strip().replace("]", ")")
        replacement = f"[Image: {safe_caption}]"
        new_line = line[: target.start()] + replacement + line[target.end() :]
        return new_line, new_line != line

    def _apply_image_caption_to_history(
        self,
        origin: str,
        message_id: str,
        image_index: int,
        caption: str,
    ) -> bool:
        chats = self.runtime.session_chats.get(origin)
        if not chats:
            return False

        target_marker = f"#msg{message_id}:"
        for idx, line in enumerate(chats):
            if target_marker not in line:
                continue
            replaced_line, changed = self._replace_image_marker_at_index(
                line, image_index, caption
            )
            if not changed:
                return False
            chats[idx] = replaced_line
            return True
        return False

    async def _resolve_image_ref_to_local_path(self, image_ref: str) -> str:
        clean_ref = str(image_ref or "").strip()
        if not clean_ref:
            return ""

        if clean_ref.startswith("file://"):
            clean_ref = clean_ref[7:]

        candidate = Path(clean_ref)
        if candidate.exists() and candidate.is_file():
            return str(candidate)

        if clean_ref.startswith("http://") or clean_ref.startswith("https://"):
            downloaded = await download_image_by_url(clean_ref)
            return str(downloaded or "")

        return ""

    @staticmethod
    def _encode_image_file(image_path: str) -> tuple[str, str]:
        path = Path(image_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Image file not found: {image_path}")

        raw_bytes = path.read_bytes()
        if not raw_bytes:
            raise ValueError(f"Image file is empty: {image_path}")

        mime_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        if not mime_type.startswith("image/"):
            mime_type = "image/jpeg"
        encoded = base64.b64encode(raw_bytes).decode("ascii")
        return encoded, mime_type

    @staticmethod
    def _format_duration(seconds: int) -> str:
        seconds = max(0, int(seconds))
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, secs = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if secs or not parts:
            parts.append(f"{secs}s")
        return " ".join(parts)

    @staticmethod
    def _ban_scope_id(event: AstrMessageEvent) -> str:
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return ""
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return ""
        platform_id = (
            str(event.get_platform_id() or "").strip()
            or str(event.get_platform_name() or "").strip()
        )
        return f"{platform_id}:{group_id}" if platform_id else group_id

    def _get_admin_sid_set(self) -> set[str]:
        base_cfg = self.context.get_config()
        if not isinstance(base_cfg, dict):
            return set()
        admins = base_cfg.get("admins_id", [])
        if not isinstance(admins, list):
            return set()
        return {str(sid).strip() for sid in admins if str(sid).strip()}

    @staticmethod
    def _parse_role_ids(raw: str) -> list[str]:
        text = str(raw or "").strip()
        if not text:
            return []

        parsed_ids: list[str] = []
        if text.startswith("["):
            try:
                payload = json.loads(text)
                if isinstance(payload, list):
                    parsed_ids = [str(item).strip() for item in payload]
            except json.JSONDecodeError:
                parsed_ids = []

        if not parsed_ids:
            normalized = text.replace("\n", ",").replace(";", ",").replace(" ", ",")
            parsed_ids = [token.strip() for token in normalized.split(",")]

        deduped: list[str] = []
        seen: set[str] = set()
        for role_id in parsed_ids:
            if not role_id or role_id in seen:
                continue
            seen.add(role_id)
            deduped.append(role_id)
        return deduped

    def _parse_optional_timestamp(self, raw: str) -> float | None:
        text = str(raw or "").strip()
        if not text:
            return None

        try:
            numeric = float(text)
            # Heuristic: treat millisecond timestamps as ms.
            if numeric > 1e12:
                numeric /= 1000.0
            return numeric
        except (TypeError, ValueError):
            pass

        tzinfo = self._resolve_tzinfo()
        normalized = text.replace("Z", "+00:00")
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.datetime.strptime(normalized, fmt).replace(tzinfo=tzinfo)
                return dt.timestamp()
            except ValueError:
                continue

        try:
            dt = datetime.datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tzinfo)
            return dt.timestamp()
        except ValueError:
            return None

    @staticmethod
    def _parse_extra_metadata(raw: str) -> dict:
        text = str(raw or "").strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return payload
            return {"raw_metadata": payload}
        except json.JSONDecodeError:
            return {"raw_metadata": text}

    @staticmethod
    def _normalize_sort_order(raw: str) -> str:
        return "asc" if str(raw or "").strip().lower() == "asc" else "desc"

    @staticmethod
    def _normalize_sort_by(raw: str) -> str:
        return "time" if str(raw or "").strip().lower() == "time" else "relevance"

    def _resolve_memory_scope(
        self,
        event: AstrMessageEvent,
        group_scope: str,
        group_id: str,
        platform_id: str,
    ) -> tuple[str, str, str]:
        explicit_scope = str(group_scope or "").strip()
        explicit_group_id = str(group_id or "").strip()
        explicit_platform_id = str(platform_id or "").strip()

        event_group_id = (
            str(event.get_group_id() or "").strip()
            if event.get_message_type() == MessageType.GROUP_MESSAGE
            else ""
        )
        event_platform_id = str(
            event.get_platform_id() or event.get_platform_name() or ""
        ).strip()

        final_group_id = explicit_group_id or event_group_id
        final_platform_id = explicit_platform_id or event_platform_id

        if explicit_scope:
            final_scope = explicit_scope
        elif final_group_id:
            final_scope = (
                f"{final_platform_id}:{final_group_id}"
                if final_platform_id
                else final_group_id
            )
        else:
            final_scope = ""

        return final_scope, final_group_id, final_platform_id

    def _resolve_embedding_provider(
        self, cfg: PluginConfig
    ) -> EmbeddingProvider | None:
        provider_id = str(cfg.memory_rag.embedding_provider_id or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider and isinstance(provider, EmbeddingProvider):
                logger.debug(
                    "enhance-mode | using configured embedding provider | provider=%s",
                    self._provider_label(provider),
                )
                return provider
            logger.warning(
                f"enhance-mode | 配置的 embedding_provider_id 无效或类型不匹配: {provider_id}"
            )

        all_embedding_providers = self.context.get_all_embedding_providers()
        if all_embedding_providers:
            logger.debug(
                "enhance-mode | using fallback embedding provider | provider=%s",
                self._provider_label(all_embedding_providers[0]),
            )
            return all_embedding_providers[0]
        return None

    def _check_memory_rag_ready(self) -> tuple[bool, str]:
        cfg = self._cfg()
        if not cfg.memory_rag.enable:
            return False, "Memory RAG is disabled in enhance mode config."
        if not self.memory_rag_store:
            return False, "Memory RAG store is not initialized."
        return True, ""

    def _memory_rag_webui_url(self, cfg: PluginConfig) -> str:
        host = str(cfg.memory_rag_webui.host or "127.0.0.1").strip() or "127.0.0.1"
        port = int(cfg.memory_rag_webui.port)
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    async def _start_memory_rag_webui(self) -> None:
        cfg = self._cfg()
        webui_cfg = cfg.memory_rag_webui
        if not webui_cfg.enable:
            return
        if not self.memory_rag_store:
            logger.warning(
                "enhance-mode | memory_rag_webui enabled but memory_rag_store is unavailable"
            )
            return
        if self.rag_webui_server is not None:
            return

        try:
            self.rag_webui_server = RAGWebUIServer(
                store=self.memory_rag_store,
                config={
                    "host": webui_cfg.host,
                    "port": webui_cfg.port,
                    "access_password": webui_cfg.access_password,
                    "session_timeout": webui_cfg.session_timeout,
                },
                plugin_version="0.2.4",
            )
            await self.rag_webui_server.start()
            logger.info(
                "enhance-mode | Memory RAG WebUI started at %s",
                self._memory_rag_webui_url(cfg),
            )
            if self.rag_webui_server.password_generated:
                logger.info(
                    "enhance-mode | Memory RAG WebUI generated password: %s",
                    self.rag_webui_server.access_password,
                )
        except Exception as e:
            logger.error(
                f"enhance-mode | 启动 Memory RAG WebUI 失败: {e}", exc_info=True
            )
            self.rag_webui_server = None

    async def _stop_memory_rag_webui(self) -> None:
        if self.rag_webui_server is None:
            return
        try:
            await self.rag_webui_server.stop()
        except Exception as e:
            logger.warning(
                f"enhance-mode | 停止 Memory RAG WebUI 失败: {e}", exc_info=True
            )
        finally:
            self.rag_webui_server = None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        cfg = self._cfg()
        self._display_timezone = self._resolve_config_timezone()
        if self.memory_rag_store is not None:
            self.memory_rag_store.set_display_timezone(self._display_timezone)
        logger.info(
            "enhance-mode | loaded | react_mode=%s group_history=%s active_reply=%s web_search=%s memory_rag=%s webui=%s lru_max_origins=%s timezone=%s",
            cfg.group_features.react_mode_enable,
            cfg.group_history_enabled,
            cfg.active_reply_enabled,
            cfg.web_search.enable,
            cfg.memory_rag.enable,
            cfg.memory_rag_webui.enable,
            cfg.global_settings.lru_cache.max_origins,
            self._display_timezone,
        )
        await self._start_memory_rag_webui()

    def _allow_active_reply(self, event: AstrMessageEvent, cfg: PluginConfig) -> bool:
        ar = cfg.active_reply
        if not cfg.active_reply_enabled:
            return False
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        if event.is_at_or_wake_command:
            return False
        if ar.whitelist and (
            event.unified_msg_origin not in ar.whitelist
            and (event.get_group_id() and event.get_group_id() not in ar.whitelist)
        ):
            return False
        return True

    async def _resolve_persona_mask(self, event: AstrMessageEvent) -> tuple[str, str]:
        persona_id = ""
        try:
            session_service_config = await sp.get_async(
                scope="umo",
                scope_id=event.unified_msg_origin,
                key="session_service_config",
                default={},
            )
            if isinstance(session_service_config, dict):
                persona_id = str(session_service_config.get("persona_id") or "").strip()
        except Exception as e:
            logger.debug(f"enhance-mode | 获取 session persona 失败: {e}")

        if not persona_id:
            try:
                curr_cid = (
                    await self.context.conversation_manager.get_curr_conversation_id(
                        event.unified_msg_origin,
                    )
                )
                if curr_cid:
                    conv = await self.context.conversation_manager.get_conversation(
                        event.unified_msg_origin,
                        curr_cid,
                    )
                    if conv and conv.persona_id:
                        persona_id = str(conv.persona_id).strip()
            except Exception as e:
                logger.debug(f"enhance-mode | 获取 conversation persona 失败: {e}")

        if not persona_id:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
            persona_id = str(
                cfg.get("provider_settings", {}).get("default_personality") or ""
            ).strip()

        if persona_id == "[%None]":
            return "none", "No persona mask."

        persona = None
        if persona_id:
            try:
                persona = next(
                    (
                        p
                        for p in self.context.persona_manager.personas_v3
                        if p.get("name") == persona_id
                    ),
                    None,
                )
            except Exception:
                persona = None

        if not persona:
            try:
                persona = await self.context.persona_manager.get_default_persona_v3(
                    event.unified_msg_origin
                )
            except Exception:
                persona = {"name": "default", "prompt": ""}

        persona_name = str(persona.get("name") or "default")
        persona_prompt = str(persona.get("prompt") or "").strip()
        if not persona_prompt:
            persona_prompt = "You are a helpful and friendly assistant."
        return persona_name, persona_prompt

    def _resolve_model_choice_provider(
        self, event: AstrMessageEvent, cfg: PluginConfig
    ) -> Provider | None:
        provider_id = str(cfg.active_reply.model_choice_provider_id or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider and isinstance(provider, Provider):
                logger.debug(
                    "enhance-mode | model_choice provider from config | provider=%s",
                    self._provider_label(provider),
                )
                return provider
            logger.warning(
                "enhance-mode | 配置的 model_choice_provider_id 无效或类型不匹配: %s",
                provider_id,
            )

        provider = self.context.get_using_provider(event.unified_msg_origin)
        if provider and isinstance(provider, Provider):
            logger.debug(
                "enhance-mode | model_choice provider fallback to current | provider=%s",
                self._provider_label(provider),
            )
            return provider
        return None

    @staticmethod
    def _provider_chat_id(provider: Provider) -> str:
        try:
            meta = provider.meta()
            meta_id = getattr(meta, "id", None)
            if meta_id:
                return str(meta_id)
        except Exception:
            pass
        provider_id = getattr(provider, "provider_id", None) or getattr(
            provider, "id", None
        )
        return str(provider_id or "").strip()

    def _resolve_web_search_provider(self, cfg: PluginConfig) -> Provider | None:
        provider_id = str(cfg.web_search.provider_id or "").strip()
        if not provider_id:
            return None
        provider = self.context.get_provider_by_id(provider_id)
        if provider and isinstance(provider, Provider):
            return provider
        logger.warning(
            "enhance-mode | 配置的 web_search.provider_id 无效或类型不匹配: %s",
            provider_id,
        )
        return None

    @staticmethod
    def _normalize_api_base_url(raw_base_url: str) -> str:
        base_url = str(raw_base_url or "").strip().rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[: -len("/v1")]
        return base_url

    @staticmethod
    def _extract_provider_api_key(provider: Provider) -> str:
        get_current_key = getattr(provider, "get_current_key", None)
        if callable(get_current_key):
            try:
                key = str(get_current_key() or "").strip()
                if key:
                    return key
            except Exception:
                pass

        keys: list[Any] = []
        get_keys = getattr(provider, "get_keys", None)
        if callable(get_keys):
            try:
                fetched = get_keys()
                if isinstance(fetched, list):
                    keys = fetched
                elif isinstance(fetched, str):
                    keys = [fetched]
            except Exception:
                keys = []

        if not keys:
            raw_keys = provider.provider_config.get("key", [])
            if isinstance(raw_keys, list):
                keys = raw_keys
            elif isinstance(raw_keys, str):
                keys = [raw_keys]

        for item in keys:
            key = str(item or "").strip()
            if key:
                return key
        return ""

    @staticmethod
    def _parse_sse_chat_completion(raw_text: str) -> dict[str, Any] | None:
        chunks: list[dict[str, Any]] = []
        for line in str(raw_text or "").splitlines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(chunk, dict):
                chunks.append(chunk)

        if not chunks:
            return None

        merged_content = ""
        model_name = ""
        usage_info: dict[str, Any] = {}
        for chunk in chunks:
            if not model_name:
                model_name = str(chunk.get("model") or "")
            chunk_usage = chunk.get("usage")
            if isinstance(chunk_usage, dict):
                usage_info = chunk_usage
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice0 = choices[0]
            if not isinstance(choice0, dict):
                continue
            delta = choice0.get("delta")
            if isinstance(delta, dict):
                delta_content = delta.get("content")
                if isinstance(delta_content, str):
                    merged_content += delta_content

        return {
            "choices": [{"message": {"content": merged_content}}],
            "model": model_name,
            "usage": usage_info,
        }

    @staticmethod
    def _extract_chat_completion_text(data: dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        choice0 = choices[0]
        if not isinstance(choice0, dict):
            return ""
        message = choice0.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""

        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)

    @staticmethod
    def _extract_usage_tokens(data: dict[str, Any]) -> dict[str, int]:
        usage_raw = data.get("usage")
        if not isinstance(usage_raw, dict):
            return {}
        prompt_tokens = int(
            usage_raw.get("prompt_tokens")
            or usage_raw.get("input_tokens")
            or usage_raw.get("input")
            or 0
        )
        completion_tokens = int(
            usage_raw.get("completion_tokens")
            or usage_raw.get("output_tokens")
            or usage_raw.get("output")
            or 0
        )
        total_tokens = int(
            usage_raw.get("total_tokens")
            or usage_raw.get("total")
            or (prompt_tokens + completion_tokens)
        )
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    @staticmethod
    def _join_base_with_path(base_url: str, path: str) -> str:
        cleaned_path = str(path or "").strip()
        if cleaned_path.startswith("http://") or cleaned_path.startswith("https://"):
            return cleaned_path
        if not cleaned_path.startswith("/"):
            cleaned_path = f"/{cleaned_path}"
        return f"{base_url.rstrip('/')}{cleaned_path}"

    @staticmethod
    def _extract_responses_text_and_sources(
        data: dict[str, Any],
    ) -> tuple[str, list[dict[str, str]]]:
        text_parts: list[str] = []
        source_map: dict[str, dict[str, str]] = {}

        def push_source(url: str, title: str = "", snippet: str = "") -> None:
            clean_url = str(url or "").strip()
            if not clean_url:
                return
            if clean_url not in source_map:
                source_map[clean_url] = {
                    "url": clean_url,
                    "title": str(title or "").strip(),
                    "snippet": str(snippet or "").strip(),
                }
                return
            if not source_map[clean_url]["title"] and title:
                source_map[clean_url]["title"] = str(title).strip()
            if not source_map[clean_url]["snippet"] and snippet:
                source_map[clean_url]["snippet"] = str(snippet).strip()

        output = data.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                if item_type == "message":
                    content = item.get("content")
                    if not isinstance(content, list):
                        continue
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        part_type = str(part.get("type") or "")
                        if part_type not in {"output_text", "text"}:
                            continue
                        part_text = part.get("text") or part.get("content")
                        if isinstance(part_text, str) and part_text.strip():
                            text_parts.append(part_text)
                        annotations = part.get("annotations")
                        if not isinstance(annotations, list):
                            continue
                        for annotation in annotations:
                            if not isinstance(annotation, dict):
                                continue
                            if str(annotation.get("type") or "") not in {
                                "url_citation",
                                "citation",
                            }:
                                continue
                            push_source(
                                url=str(
                                    annotation.get("url")
                                    or annotation.get("source_url")
                                    or ""
                                ),
                                title=str(annotation.get("title") or ""),
                                snippet=str(annotation.get("snippet") or ""),
                            )
                    continue
                if item_type == "web_search_call":
                    action = item.get("action")
                    if not isinstance(action, dict):
                        continue
                    action_sources = action.get("sources")
                    normalized = Main._normalize_web_search_sources(action_sources)
                    for source in normalized:
                        push_source(
                            url=source.get("url", ""),
                            title=source.get("title", ""),
                            snippet=source.get("snippet", ""),
                        )

        merged_text = "\n".join(part for part in text_parts if part.strip()).strip()
        if not merged_text:
            merged_text = str(data.get("output_text") or "").strip()
        return merged_text, list(source_map.values())

    def _build_web_search_http_requests(
        self, provider: Provider, query: str, cfg: PluginConfig
    ) -> tuple[list[dict[str, Any]], str]:
        provider_label = self._provider_chat_id(provider) or self._provider_label(
            provider
        )
        provider_cfg = (
            provider.provider_config
            if isinstance(provider.provider_config, dict)
            else {}
        )

        api_base_cfg = str(cfg.web_search.base_url_override or "").strip()
        api_base = self._normalize_api_base_url(
            api_base_cfg or str(provider_cfg.get("api_base") or "")
        )
        if not api_base:
            raise ValueError(
                f"Provider `{provider_label}` missing `api_base`, cannot run web search."
            )

        api_key = self._extract_provider_api_key(provider)
        if not api_key:
            raise ValueError(
                f"Provider `{provider_label}` missing API key, cannot run web search."
            )

        model = str(provider.get_model() or provider_cfg.get("model") or "").strip()
        custom_extra_body = provider_cfg.get("custom_extra_body", {})

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        custom_headers = provider_cfg.get("custom_headers", {})
        if isinstance(custom_headers, dict):
            protected_headers = {"authorization", "content-type"}
            for key, value in custom_headers.items():
                if str(key).lower() in protected_headers:
                    continue
                headers[str(key)] = str(value)

        request_mode = str(cfg.web_search.request_mode or "auto").strip().lower()
        modes: list[str]
        if request_mode in {"responses", "chat_completions"}:
            modes = [request_mode]
        else:
            modes = ["responses", "chat_completions"]

        requests: list[dict[str, Any]] = []
        for mode in modes:
            if mode == "responses":
                body = {
                    "input": query,
                    "instructions": cfg.web_search.system_prompt,
                    "temperature": 0.2,
                    "tools": [{"type": "web_search"}],
                    "tool_choice": "auto",
                }
                if model:
                    body["model"] = model
                if isinstance(custom_extra_body, dict):
                    protected_keys = {"model", "input", "instructions"}
                    for key, value in custom_extra_body.items():
                        if str(key) in protected_keys:
                            continue
                        body[str(key)] = value
                requests.append(
                    {
                        "mode": "responses",
                        "url": self._join_base_with_path(api_base, "/v1/responses"),
                        "headers": headers,
                        "body": body,
                    }
                )
                continue

            body = {
                "messages": [
                    {"role": "system", "content": cfg.web_search.system_prompt},
                    {"role": "user", "content": query},
                ],
                "temperature": 0.2,
                "stream": False,
            }
            if model:
                body["model"] = model
            if isinstance(custom_extra_body, dict):
                protected_keys = {"model", "messages", "stream"}
                for key, value in custom_extra_body.items():
                    if str(key) in protected_keys:
                        continue
                    body[str(key)] = value
            requests.append(
                {
                    "mode": "chat_completions",
                    "url": self._join_base_with_path(api_base, "/v1/chat/completions"),
                    "headers": headers,
                    "body": body,
                }
            )

        return requests, provider_label

    @staticmethod
    def _try_parse_web_search_json(text: str) -> dict | None:
        clean_text = str(text or "").strip()
        if not clean_text:
            return None

        if clean_text.startswith("{") and clean_text.endswith("}"):
            try:
                parsed = json.loads(clean_text)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        code_block_pattern = r"```(?:json)?\s*\n?([\s\S]*?)\n?```"
        for matched in re.findall(code_block_pattern, clean_text):
            try:
                parsed = json.loads(matched.strip())
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue

        decoder = json.JSONDecoder()
        start_idx = 0
        remaining_attempts = 10
        while start_idx < len(clean_text) and remaining_attempts > 0:
            brace_pos = clean_text.find("{", start_idx)
            if brace_pos == -1:
                break
            try:
                parsed, end_idx = decoder.raw_decode(clean_text, idx=brace_pos)
                if isinstance(parsed, dict) and (
                    "content" in parsed or "sources" in parsed
                ):
                    return parsed
                start_idx = end_idx
            except json.JSONDecodeError:
                start_idx = brace_pos + 1
            remaining_attempts -= 1
        return None

    @staticmethod
    def _normalize_web_search_sources(raw_sources: object) -> list[dict[str, str]]:
        from urllib.parse import urlparse

        normalized: list[dict[str, str]] = []
        if not isinstance(raw_sources, list):
            return normalized
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            try:
                parsed_url = urlparse(url)
                if parsed_url.scheme not in {"http", "https"}:
                    continue
                if len(url) > 2048 or any(ord(ch) < 32 for ch in url):
                    continue
            except Exception:
                continue
            normalized.append(
                {
                    "url": url,
                    "title": str(item.get("title") or "").strip(),
                    "snippet": str(item.get("snippet") or "").strip(),
                }
            )
        return normalized

    @staticmethod
    def _extract_web_search_sources_from_text(text: str) -> list[dict[str, str]]:
        from urllib.parse import urlparse

        url_pattern = r"https://[^\s)\]}>\"']+|http://[^\s)\]}>\"']+"
        seen: set[str] = set()
        out: list[dict[str, str]] = []
        for match in re.finditer(url_pattern, str(text or "")):
            url = match.group().rstrip(".,;:!?\"'")
            if not url or url in seen:
                continue
            try:
                parsed_url = urlparse(url)
                if parsed_url.scheme not in {"http", "https"}:
                    continue
                if len(url) > 2048 or any(ord(ch) < 32 for ch in url):
                    continue
            except Exception:
                continue
            seen.add(url)
            out.append({"url": url, "title": "", "snippet": ""})
        return out

    async def _run_web_search(
        self,
        event: AstrMessageEvent,
        query: str,
        cfg: PluginConfig,
    ) -> dict[str, object]:
        provider = self._resolve_web_search_provider(cfg)
        if not provider:
            return {
                "ok": False,
                "error": (
                    "Web search provider is not configured or invalid. "
                    "Please set `web_search.provider_id`."
                ),
            }
        try:
            request_specs, provider_label = self._build_web_search_http_requests(
                provider, query, cfg
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not request_specs:
            return {"ok": False, "error": "No web search request spec could be built."}

        start_ts = time.perf_counter()
        logger.info(
            "enhance-mode | web_search start(direct) | origin=%s provider=%s query_len=%s",
            event.unified_msg_origin,
            provider_label,
            len(query),
        )

        parsed_data: dict[str, Any] | None = None
        text = ""
        usage: dict[str, int] = {}
        sources_from_endpoint: list[dict[str, str]] = []
        last_error: dict[str, object] = {"ok": False, "error": "Web search failed."}

        try:
            timeout = aiohttp.ClientTimeout(total=cfg.web_search.timeout_sec)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for request_spec in request_specs:
                    mode = str(request_spec.get("mode") or "chat_completions")
                    request_url = str(request_spec.get("url") or "")
                    headers = request_spec.get("headers")
                    body = request_spec.get("body")
                    if not request_url or not isinstance(headers, dict):
                        continue

                    logger.info(
                        "enhance-mode | web_search request | origin=%s provider=%s mode=%s url=%s",
                        event.unified_msg_origin,
                        provider_label,
                        mode,
                        request_url,
                    )

                    async with session.post(
                        request_url,
                        json=body,
                        headers=headers,
                    ) as resp:
                        raw_text = await resp.text()
                        if resp.status != 200:
                            logger.warning(
                                "enhance-mode | web_search http failed | origin=%s provider=%s mode=%s status=%s body=%s",
                                event.unified_msg_origin,
                                provider_label,
                                mode,
                                resp.status,
                                raw_text[:800],
                            )
                            last_error = {
                                "ok": False,
                                "error": f"Web search HTTP {resp.status} ({mode})",
                                "raw": raw_text[:2000] if raw_text else "",
                            }
                            continue

                        parsed_data = None
                        content_type = resp.headers.get("Content-Type", "")
                        if mode == "chat_completions" and (
                            "text/event-stream" in content_type
                            or raw_text.strip().startswith("data:")
                        ):
                            parsed_data = self._parse_sse_chat_completion(raw_text)
                        else:
                            try:
                                decoded = json.loads(raw_text)
                                if isinstance(decoded, dict):
                                    parsed_data = decoded
                            except json.JSONDecodeError:
                                parsed_data = None

                        if parsed_data is None:
                            last_error = {
                                "ok": False,
                                "error": (
                                    f"Web search response parsing failed for mode={mode}."
                                ),
                                "raw": raw_text[:2000] if raw_text else "",
                            }
                            continue

                        usage = self._extract_usage_tokens(parsed_data)
                        if mode == "responses":
                            text, sources_from_endpoint = (
                                self._extract_responses_text_and_sources(parsed_data)
                            )
                        else:
                            text = self._extract_chat_completion_text(
                                parsed_data
                            ).strip()
                            sources_from_endpoint = []

                        if not text:
                            last_error = {
                                "ok": False,
                                "error": (
                                    "Provider returned empty response for web search."
                                ),
                                "raw": json.dumps(parsed_data, ensure_ascii=False)[
                                    :2000
                                ],
                            }
                            continue
                        break
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": (
                    "Web search timeout. "
                    f"Current timeout={cfg.web_search.timeout_sec:.1f}s."
                ),
            }
        except Exception as e:
            logger.exception(
                "enhance-mode | web_search direct call failed | origin=%s provider=%s error=%s",
                event.unified_msg_origin,
                provider_label,
                e,
            )
            return {"ok": False, "error": f"Web search provider call failed: {e}"}

        if not text:
            last_error["elapsed_ms"] = (time.perf_counter() - start_ts) * 1000
            last_error["usage"] = usage
            return last_error

        elapsed_ms = (time.perf_counter() - start_ts) * 1000
        merged_sources: list[dict[str, str]] = []
        seen_urls: set[str] = set()

        def push_sources(src_list: list[dict[str, str]]) -> None:
            for source in src_list:
                if not isinstance(source, dict):
                    continue
                url = str(source.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                merged_sources.append(
                    {
                        "url": url,
                        "title": str(source.get("title") or "").strip(),
                        "snippet": str(source.get("snippet") or "").strip(),
                    }
                )

        parsed = self._try_parse_web_search_json(text)
        if parsed is not None:
            content = str(parsed.get("content") or "").strip()
            if not content:
                content = text
            push_sources(self._normalize_web_search_sources(parsed.get("sources")))
            push_sources(sources_from_endpoint)
            if not merged_sources:
                push_sources(self._extract_web_search_sources_from_text(content))
            logger.info(
                "enhance-mode | web_search done(direct) | origin=%s provider=%s elapsed_ms=%.1f sources=%s",
                event.unified_msg_origin,
                provider_label,
                elapsed_ms,
                len(merged_sources),
            )
            return {
                "ok": True,
                "content": content,
                "sources": merged_sources,
                "elapsed_ms": elapsed_ms,
                "usage": usage,
            }

        push_sources(sources_from_endpoint)
        if not merged_sources:
            push_sources(self._extract_web_search_sources_from_text(text))
        logger.info(
            "enhance-mode | web_search done(non_json direct) | origin=%s provider=%s elapsed_ms=%.1f sources=%s",
            event.unified_msg_origin,
            provider_label,
            elapsed_ms,
            len(merged_sources),
        )
        return {
            "ok": True,
            "content": text,
            "sources": merged_sources,
            "elapsed_ms": elapsed_ms,
            "usage": usage,
            "raw": text,
        }

    def _format_web_search_tool_result(
        self, result: dict[str, object], cfg: PluginConfig
    ) -> str:
        if not bool(result.get("ok")):
            error = str(result.get("error") or "Unknown error")
            raw = str(result.get("raw") or "").strip()
            if raw:
                return f"Web search failed: {error}\n{raw}"
            return f"Web search failed: {error}"

        content = str(result.get("content") or "").strip()
        sources_raw = result.get("sources")
        sources = (
            sources_raw
            if isinstance(sources_raw, list)
            else self._extract_web_search_sources_from_text(content)
        )

        lines = [f"搜索结果:\n{content}"]
        if cfg.web_search.show_sources and sources:
            max_sources = cfg.web_search.max_sources
            if max_sources > 0:
                sources = sources[:max_sources]
            lines.append("\n参考来源:")
            for idx, source in enumerate(sources, start=1):
                if not isinstance(source, dict):
                    continue
                url = str(source.get("url") or "").strip()
                title = str(source.get("title") or "").strip()
                snippet = str(source.get("snippet") or "").strip()
                if title:
                    lines.append(f"  {idx}. {title}")
                    lines.append(f"     {url}")
                else:
                    lines.append(f"  {idx}. {url}")
                if snippet:
                    lines.append(f"     {snippet}")

        lines.append("\n[提示: 请基于以上搜索结果直接回答用户，不要输出 Markdown。]")
        return "\n".join(lines)

    @staticmethod
    def _browser_session_key(origin: str) -> str:
        raw_origin = str(origin or "").strip() or "default"
        readable = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw_origin).strip("-").lower()
        if len(readable) > 40:
            readable = readable[:40].rstrip("-")
        if not readable:
            readable = "session"
        suffix = uuid.uuid5(uuid.NAMESPACE_DNS, raw_origin).hex[:12]
        return f"astrbot-{readable}-{suffix}"

    def _browser_tool_lock(self, session_key: str) -> asyncio.Lock:
        locks = getattr(self, "_browser_tool_locks", None)
        if locks is None:
            locks = {}
            self._browser_tool_locks = locks
        lock = locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            locks[session_key] = lock
        return lock

    def _build_browser_tool_base_args(
        self, session_key: str, cfg: PluginConfig
    ) -> list[str]:
        browser_cfg = cfg.browser_tool
        command_parts = shlex.split(browser_cfg.command)
        if not command_parts:
            command_parts = ["agent-browser"]

        args = command_parts + ["--session", session_key]
        if browser_cfg.persist_session:
            args.extend(["--session-name", session_key])
        if browser_cfg.headed:
            args.append("--headed")
        if browser_cfg.content_boundaries:
            args.append("--content-boundaries")
        if browser_cfg.max_output_chars > 0:
            args.extend(["--max-output", str(browser_cfg.max_output_chars)])
        if browser_cfg.allowed_domains:
            args.extend(
                ["--allowed-domains", ",".join(cfg.browser_tool.allowed_domains)]
            )
        return args

    async def _run_browser_tool_command(
        self,
        event: AstrMessageEvent,
        command_args: list[str],
        cfg: PluginConfig,
    ) -> dict[str, object]:
        browser_cfg = cfg.browser_tool
        session_key = self._browser_session_key(event.unified_msg_origin)
        command_name = command_args[0] if command_args else "unknown"
        base_args = self._build_browser_tool_base_args(session_key, cfg)
        env = os.environ.copy()
        if browser_cfg.idle_timeout_ms > 0:
            env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] = str(browser_cfg.idle_timeout_ms)
        env["AGENT_BROWSER_DEFAULT_TIMEOUT"] = str(
            max(1000, min(int(browser_cfg.timeout_sec * 1000), 30000))
        )

        start = time.perf_counter()
        process: asyncio.subprocess.Process | None = None
        try:
            async with self._browser_tool_lock(session_key):
                process = await asyncio.create_subprocess_exec(
                    *(base_args + ["--json", *command_args]),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=max(browser_cfg.timeout_sec + 5.0, 10.0),
                )
        except FileNotFoundError:
            return {
                "ok": False,
                "action": command_name,
                "session": session_key,
                "error": (
                    "agent-browser command not found. "
                    "Install it with `npm install -g agent-browser` and then run "
                    "`agent-browser install`."
                ),
            }
        except asyncio.TimeoutError:
            if process is not None:
                process.kill()
                await process.communicate()
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.warning(
                "enhance-mode | browser_tool timeout | origin=%s session=%s action=%s elapsed_ms=%.1f",
                event.unified_msg_origin,
                session_key,
                command_name,
                elapsed_ms,
            )
            return {
                "ok": False,
                "action": command_name,
                "session": session_key,
                "elapsed_ms": round(elapsed_ms, 1),
                "error": (
                    "Browser command timed out. "
                    f"Current timeout={browser_cfg.timeout_sec:.1f}s."
                ),
            }

        elapsed_ms = (time.perf_counter() - start) * 1000
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        parsed_payload: object = stdout
        cli_success = process.returncode == 0
        cli_error = ""
        if stdout:
            try:
                parsed_json = json.loads(stdout)
            except json.JSONDecodeError:
                parsed_json = None
            if isinstance(parsed_json, dict):
                cli_success = bool(parsed_json.get("success", cli_success))
                cli_error = str(parsed_json.get("error") or "").strip()
                parsed_payload = parsed_json.get("data", parsed_json)

        result: dict[str, object] = {
            "ok": cli_success and process.returncode == 0,
            "action": command_name,
            "session": session_key,
            "elapsed_ms": round(elapsed_ms, 1),
            "data": parsed_payload,
        }
        if stderr:
            result["stderr"] = stderr
        if not bool(result["ok"]):
            error = stderr or cli_error or stdout or f"Exit code {process.returncode}"
            result["error"] = error
            result["exit_code"] = process.returncode
            logger.warning(
                "enhance-mode | browser_tool failed | origin=%s session=%s action=%s elapsed_ms=%.1f error=%s",
                event.unified_msg_origin,
                session_key,
                command_name,
                elapsed_ms,
                error[:400],
            )
        else:
            logger.info(
                "enhance-mode | browser_tool done | origin=%s session=%s action=%s elapsed_ms=%.1f",
                event.unified_msg_origin,
                session_key,
                command_name,
                elapsed_ms,
            )
        return result

    @staticmethod
    def _format_browser_tool_result(result: dict[str, object]) -> str:
        return json.dumps(result, ensure_ascii=False)

    async def _judge_model_choice(
        self,
        event: AstrMessageEvent,
        cfg: PluginConfig,
        origin: str,
        messages: list[str],
        trigger_reason: str,
    ) -> bool:
        ar = cfg.active_reply
        history = self.runtime.model_choice_histories[origin]
        history_context_lines = []
        if ar.model_history_messages > 0:
            history_context_lines = history[-ar.model_history_messages :]
        history_context = (
            "\n".join(history_context_lines)
            if history_context_lines
            else "(disabled or no additional history)"
        )

        logger.info(
            "enhance-mode | model_choice | 开始判定 | "
            f"origin={origin} trigger={trigger_reason} stack_size={len(messages)} "
            f"history={len(history_context_lines)}"
        )

        provider = self._resolve_model_choice_provider(event, cfg)
        if not provider:
            logger.error("enhance-mode | 未找到可用提供商，无法执行模型选择触发")
            return False

        persona_name, persona_mask = await self._resolve_persona_mask(event)
        prompt_tmpl = ar.model_choice_prompt
        try:
            judge_prompt = prompt_tmpl.format(
                stack_size=len(messages),
                messages="\n".join(messages),
                history_count=len(history_context_lines),
                history_context=history_context,
                persona_name=persona_name,
                persona_mask=persona_mask,
            )
        except Exception:
            judge_prompt = (
                f"{prompt_tmpl}\n\n"
                f"人格面具({persona_name}):\n{persona_mask}\n\n"
                f"最近消息:\n{chr(10).join(messages)}\n\n"
                f"额外历史上下文({len(history_context_lines)}):\n{history_context}\n\n"
                "请仅输出 REPLY 或 SKIP。"
            )

        try:
            judge_resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=judge_prompt,
                    session_id=uuid.uuid4().hex,
                    persist=False,
                ),
                timeout=cfg.global_settings.timeouts.model_choice_sec,
            )
        except asyncio.TimeoutError:
            logger.error("enhance-mode | 模型选择触发判定超时")
            return False
        except Exception as e:
            logger.error(f"enhance-mode | 模型选择触发判定失败: {e}")
            return False

        decision_raw = (judge_resp.completion_text or "").strip().upper()
        decision = decision_raw.split()[0] if decision_raw else ""
        if decision.startswith("REPLY"):
            logger.info(
                "enhance-mode | model_choice | 判定通过(REPLY) | "
                f"origin={origin} trigger={trigger_reason} persona={persona_name}"
            )
            return True
        if decision and not decision.startswith("SKIP"):
            logger.info(
                "enhance-mode | model_choice | 判定拒绝(非标准输出按 SKIP) | "
                f"origin={origin} trigger={trigger_reason} output={decision_raw}"
            )
            return False
        logger.info(
            "enhance-mode | model_choice | 判定拒绝(SKIP) | "
            f"origin={origin} trigger={trigger_reason} persona={persona_name}"
        )
        return False

    async def _need_active_reply_model_choice(
        self, event: AstrMessageEvent, cfg: PluginConfig
    ) -> bool:
        origin = event.unified_msg_origin
        self._touch_origin(origin, cfg)

        ar = cfg.active_reply
        text = (event.message_str or "").strip() or "[Empty]"
        nickname = event.message_obj.sender.nickname
        sender_id = event.get_sender_id()
        stack = self.runtime.active_reply_stacks[origin]
        history = self.runtime.model_choice_histories[origin]

        stack.append(f"[{nickname}/{sender_id}]: {text}")
        history_line = (
            f"[{nickname}/{sender_id}/"
            f"{datetime.datetime.now().strftime('%H:%M:%S')}]: {text}"
        )
        history.append(history_line)

        history_limit = max(
            60,
            ar.model_stack_size * 6,
            ar.model_history_messages * 6,
        )
        if len(history) > history_limit:
            del history[:-history_limit]

        logger.info(
            "enhance-mode | model_choice | 栈填充 | "
            f"origin={origin} progress={len(stack)}/{ar.model_stack_size} "
            f"sender={sender_id}"
        )

        if len(stack) < ar.model_stack_size:
            return False

        messages = stack[-ar.model_stack_size :]
        stack.clear()
        return await self._judge_model_choice(
            event,
            cfg,
            origin,
            messages,
            trigger_reason="stack_full",
        )

    async def _get_image_caption(
        self,
        image_url: str,
        provider_id: str,
        prompt: str,
        timeout_sec: float,
    ) -> str:
        if not provider_id:
            provider = self.context.get_using_provider()
        else:
            provider = self.context.get_provider_by_id(provider_id)
            if not provider:
                raise Exception(f"没有找到 ID 为 {provider_id} 的提供商")

        if not isinstance(provider, Provider):
            raise Exception(f"提供商类型错误({type(provider)})，无法获取图片描述")

        start_ts = time.perf_counter()
        logger.debug(
            "enhance-mode | image_caption start | provider=%s timeout=%.1fs",
            self._provider_label(provider),
            timeout_sec,
        )
        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=prompt,
                session_id=uuid.uuid4().hex,
                image_urls=[image_url],
                persist=False,
            ),
            timeout=timeout_sec,
        )
        elapsed_ms = (time.perf_counter() - start_ts) * 1000
        logger.debug(
            "enhance-mode | image_caption done | provider=%s elapsed_ms=%.1f caption_len=%s",
            self._provider_label(provider),
            elapsed_ms,
            len(response.completion_text or ""),
        )
        return response.completion_text

    async def _need_active_reply(
        self, event: AstrMessageEvent, cfg: PluginConfig
    ) -> bool:
        if not self._allow_active_reply(event, cfg):
            return False

        ar = cfg.active_reply
        if ar.mode == "model_choice":
            return await self._need_active_reply_model_choice(event, cfg)
        sample = random.random()
        decision = sample < ar.possibility
        logger.debug(
            "enhance-mode | active_reply probability | origin=%s sample=%.4f threshold=%.4f decision=%s",
            event.unified_msg_origin,
            sample,
            ar.possibility,
            decision,
        )
        return decision

    @filter.on_llm_request()
    async def inject_role(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        cfg = self._cfg()
        gf = cfg.group_features
        if not gf.role_display and not gf.role_prompt_enable:
            return

        base_cfg = self.context.get_config(umo=event.unified_msg_origin)
        if not base_cfg.get("identifier"):
            return

        is_admin = event.is_admin()
        role = "admin" if is_admin else "member"

        if gf.role_prompt_enable:
            extra_prompt = gf.role_admin_prompt if is_admin else gf.role_member_prompt
            extra_prompt = extra_prompt.strip()
            if extra_prompt:
                if req.system_prompt and not req.system_prompt.endswith("\n"):
                    req.system_prompt += "\n"
                req.system_prompt += extra_prompt

        if not gf.role_display:
            return

        role_line = f", Role: {role}"

        for part in req.extra_user_content_parts:
            if isinstance(part, TextPart) and "<system_reminder>" in part.text:
                if "Nickname: " in part.text and role_line not in part.text:
                    nickname_idx = part.text.index("Nickname: ")
                    rest = part.text[nickname_idx:]
                    newline_idx = rest.find("\n")
                    if newline_idx != -1:
                        insert_pos = nickname_idx + newline_idx
                    else:
                        close_idx = part.text.find("</system_reminder>")
                        insert_pos = close_idx if close_idx != -1 else len(part.text)
                    part.text = (
                        part.text[:insert_pos] + role_line + part.text[insert_pos:]
                    )
                return

        reminder = f"<system_reminder>Role: {role}</system_reminder>"
        req.extra_user_content_parts.append(TextPart(text=reminder))

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=9999)
    async def guard_banned_user(self, event: AstrMessageEvent) -> None:
        cfg = self._cfg()
        if not cfg.group_features.ban_control_enable:
            return
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        scope_id = self._ban_scope_id(event)
        if not scope_id:
            return

        released_count = self.ban_store.cleanup_expired(scope_id=scope_id)
        if released_count > 0:
            logger.info(
                "enhance-mode | 自动解封过期用户数量 | "
                f"count={released_count} scope={scope_id}"
            )

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            return
        if (
            not cfg.group_features.ban_allow_admin
            and sender_id in self._get_admin_sid_set()
        ):
            active_ban = self.ban_store.get_active_ban(
                scope_id=scope_id, user_id=sender_id
            )
            if active_ban:
                logger.info(
                    "enhance-mode | 管理员保护生效，命中封禁但按 bypass 放行(不修改数据库) | "
                    f"user_id={sender_id} remaining={self._format_duration(active_ban.remaining_seconds)} "
                    f"scope={scope_id} origin={event.unified_msg_origin}"
                )
            return

        active_ban = self.ban_store.get_active_ban(scope_id=scope_id, user_id=sender_id)
        if not active_ban:
            return

        logger.info(
            "enhance-mode | 命中封禁名单，已拦截消息 | "
            f"user_id={sender_id} remaining={self._format_duration(active_ban.remaining_seconds)} "
            f"scope={scope_id} origin={event.unified_msg_origin}"
        )
        event.stop_event()

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_group_message(self, event: AstrMessageEvent):
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        cfg = self._cfg()
        if not cfg.group_history_enabled and not cfg.active_reply_enabled:
            return

        has_content = any(
            isinstance(comp, (Plain, Image, Reply))
            for comp in event.message_obj.message
        )
        if not has_content:
            return

        need_active = await self._need_active_reply(event, cfg)

        if cfg.group_history_enabled:
            try:
                await self._record_message(event, cfg)
            except Exception as e:
                logger.error(f"enhance-mode | record message error: {e}")

        if need_active:
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if not provider:
                logger.error("enhance-mode | 未找到任何 LLM 提供商，无法主动回复")
                return
            try:
                logger.info(
                    "enhance-mode | active_reply triggered | origin=%s mode=%s provider=%s",
                    event.unified_msg_origin,
                    cfg.active_reply.mode,
                    self._provider_label(provider),
                )
                if hasattr(event, "set_extra"):
                    event.set_extra("_enhance_active_reply_triggered", True)
                    event.set_extra("_enhance_active_reply_mode", cfg.active_reply.mode)

                session_curr_cid = (
                    await self.context.conversation_manager.get_curr_conversation_id(
                        event.unified_msg_origin,
                    )
                )
                if not session_curr_cid:
                    logger.error(
                        "enhance-mode | 当前未处于对话状态，无法主动回复，"
                        "请使用 /switch 或 /new 创建一个会话。"
                    )
                    return

                conv = await self.context.conversation_manager.get_conversation(
                    event.unified_msg_origin,
                    session_curr_cid,
                )
                if not conv:
                    logger.error("enhance-mode | 未找到对话，无法主动回复")
                    return

                yield event.request_llm(
                    prompt=event.message_str,
                    session_id=event.session_id,
                    conversation=conv,
                )
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"enhance-mode | 主动回复失败: {e}")

    async def _record_message(self, event: AstrMessageEvent, cfg: PluginConfig) -> None:
        history_cfg = cfg.group_history

        datetime_str = datetime.datetime.now().strftime("%H:%M:%S")
        nickname = event.message_obj.sender.nickname
        msg_id = event.message_obj.message_id
        normalized_msg_id = self._normalize_message_id(msg_id)
        image_urls: list[str] = []

        if history_cfg.include_sender_id and history_cfg.include_role_tag:
            sender_id = event.get_sender_id()
            role_tag = "(admin)" if event.is_admin() else "(member)"
            header = f"[{nickname}/{sender_id}/{datetime_str}]{role_tag} #msg{msg_id}:"
        elif history_cfg.include_sender_id:
            sender_id = event.get_sender_id()
            header = f"[{nickname}/{sender_id}/{datetime_str}] #msg{msg_id}:"
        elif history_cfg.include_role_tag:
            role_tag = "(admin)" if event.is_admin() else "(member)"
            header = f"[{nickname}/{datetime_str}]{role_tag} #msg{msg_id}:"
        else:
            header = f"[{nickname}/{datetime_str}] #msg{msg_id}:"

        parts = [header]
        for comp in event.get_messages():
            if isinstance(comp, Reply):
                quote_nick = comp.sender_nickname or "Unknown"
                quote_text = (comp.message_str or "").strip() or "..."
                quote_id = self._normalize_message_id(getattr(comp, "id", ""))
                if quote_id:
                    parts.append(f" [Quote #msg{quote_id} {quote_nick}: {quote_text}]")
                else:
                    parts.append(f" [Quote {quote_nick}: {quote_text}]")
            elif isinstance(comp, Plain):
                parts.append(f" {comp.text}")
            elif isinstance(comp, Image):
                image_url = str(comp.url or comp.file or "").strip()
                image_urls.append(image_url)
                parts.append(" [Image]")
            elif isinstance(comp, At):
                parts.append(f" [At: {comp.name}]")

        final_message = "".join(parts)
        logger.debug(f"enhance-mode | {event.unified_msg_origin} | {final_message}")

        self._touch_origin(event.unified_msg_origin, cfg)
        chats = self.runtime.session_chats[event.unified_msg_origin]
        chats.append(final_message)
        if len(chats) > history_cfg.max_messages:
            removed_line = chats.pop(0)
            removed_msg_id = self._extract_message_id_from_history_line(removed_line)
            if removed_msg_id:
                self.runtime.image_message_registry[event.unified_msg_origin].pop(
                    removed_msg_id, None
                )
        if normalized_msg_id and image_urls:
            self.runtime.image_message_registry[event.unified_msg_origin][
                normalized_msg_id
            ] = {"urls": image_urls, "captions": {}}
            logger.debug(
                "enhance-mode | image message registered | origin=%s msg_id=%s image_count=%s deferred_caption=%s",
                event.unified_msg_origin,
                normalized_msg_id,
                len(image_urls),
                history_cfg.image_caption,
            )
        logger.debug(
            "enhance-mode | group history updated | origin=%s size=%s",
            event.unified_msg_origin,
            len(chats),
        )

    @filter.on_llm_request()
    async def inject_group_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        cfg = self._cfg()
        if not cfg.group_history_enabled:
            return
        if event.unified_msg_origin not in self.runtime.session_chats:
            return

        self._touch_origin(event.unified_msg_origin, cfg)
        bounded_chats = bounded_chat_history_text(
            self.runtime.session_chats[event.unified_msg_origin]
        )
        logger.debug(
            "enhance-mode | injecting group context | origin=%s history_size=%s",
            event.unified_msg_origin,
            len(self.runtime.session_chats[event.unified_msg_origin]),
        )
        interaction_instructions = build_interaction_instructions(
            cfg.group_features.mention_parse,
            cfg.group_history.include_sender_id,
        )
        interaction_instructions += (
            "\nIf a history message contains `[Image]` and visual details are necessary, "
            "you may call `enhance_use_image(message_id, image_index, attach_to_model, write_to_history, prompt)`. "
            "By default it does both: attach image to this run context and write description back into chat history. "
            "Set `attach_to_model=false` for history-only. "
            "Set `write_to_history=false` for attach-only."
        )
        if cfg.web_search.enable:
            interaction_instructions += (
                "\nWhen real-time facts or uncertain external information are needed, "
                "you may call `grok_web_search(query)`."
            )
        if cfg.browser_tool.enable:
            interaction_instructions += (
                "\nWhen you need to interact with a real webpage, "
                "first call `enhance_browser_open(url)`, then `enhance_browser_snapshot()` "
                "to inspect refs like `@e2`. Use `enhance_browser_action(action, target, value)` "
                "for actions such as click/fill/type/press/get_text/get_html/get_value/get_title/get_url/scroll, "
                "`enhance_browser_wait(mode, target, state)` when the page needs time to load, "
                "and `enhance_browser_close()` when done. "
                "Prefer the snapshot-ref workflow over guessing CSS selectors."
            )

        if (
            cfg.group_features.react_mode_enable
            and event.get_message_type() == MessageType.GROUP_MESSAGE
        ):
            is_active_triggered = event.get_extra(
                "_enhance_active_reply_triggered", False
            )
            active_mode = event.get_extra("_enhance_active_reply_mode", "")
            if is_active_triggered and active_mode == "model_choice":
                req.prompt = (
                    f"You are now in a chatroom. The chat history is as follows:\n{bounded_chats}\n\n"
                    "You decided to actively join this conversation because some recent messages are worth replying to.\n"
                    "Choose the message(s) you want to respond to from the chat history above, "
                    "and compose a natural reply. Quote the message you choose in most cases.\n"
                    "Only output your response and do not output any other information. "
                    "You MUST use the SAME language as the chatroom is using."
                    f"{interaction_instructions}"
                )
            else:
                prompt = req.prompt
                req.prompt = (
                    f"You are now in a chatroom. The chat history is as follows:\n{bounded_chats}\n\n"
                    f"Now, a new message is coming: `{prompt}`. "
                    "Please react to it. Your entire output is your reply to this message. "
                    "Quote the message which is coming in most cases. "
                    "Only output your response and do not output any other information. "
                    "You MUST use the SAME language as the chatroom is using."
                    f"{interaction_instructions}"
                )
            req.contexts = []
        else:
            req.system_prompt += (
                "You are now in a chatroom. The chat history is as follows: \n"
            )
            req.system_prompt += bounded_chats
            req.system_prompt += interaction_instructions

    @filter.on_decorating_result()
    async def parse_tags(self, event: AstrMessageEvent) -> None:
        result = event.get_result()
        if not result or not result.chain:
            return

        # 全局拦截：模型输出 <refuse/> 时直接清空结果链，阻止后续发送到平台。
        if chain_has_refuse_tag(result.chain):
            logger.info(
                "enhance-mode | 检测到 <refuse/>，已取消发送 | "
                f"origin={event.unified_msg_origin}"
            )
            if hasattr(event, "set_extra"):
                event.set_extra("_enhance_refused_reply", True)
            result.chain = []
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        transformed = transform_result_chain(
            result.chain,
            parse_mention=self._cfg().group_features.mention_parse,
        )
        if transformed is None:
            return

        result.chain = transformed

    @filter.on_llm_response()
    async def record_bot_response(
        self, event: AstrMessageEvent, resp: LLMResponse
    ) -> None:
        cfg = self._cfg()
        if not cfg.group_history_enabled:
            return
        if event.unified_msg_origin not in self.runtime.session_chats:
            return
        if not resp.completion_text:
            return

        if has_refuse_tag(resp.completion_text):
            logger.info(
                "enhance-mode | 检测到 <refuse/>，跳过机器人回复历史记录 | "
                f"origin={event.unified_msg_origin}"
            )
            return

        datetime_str = datetime.datetime.now().strftime("%H:%M:%S")
        text = clean_response_text_for_history(resp.completion_text)
        if not text:
            return
        final_message = f"[You/{datetime_str}]: {text}"

        logger.debug(
            f"enhance-mode | recorded AI response: "
            f"{event.unified_msg_origin} | {final_message}"
        )

        self._touch_origin(event.unified_msg_origin, cfg)
        chats = self.runtime.session_chats[event.unified_msg_origin]
        chats.append(final_message)
        if len(chats) > cfg.group_history.max_messages:
            removed_line = chats.pop(0)
            removed_msg_id = self._extract_message_id_from_history_line(removed_line)
            if removed_msg_id:
                self.runtime.image_message_registry[event.unified_msg_origin].pop(
                    removed_msg_id, None
                )
        logger.debug(
            "enhance-mode | bot response recorded | origin=%s size=%s",
            event.unified_msg_origin,
            len(chats),
        )

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        clean_session = event.get_extra("_clean_ltm_session", False)
        if not clean_session:
            return
        self.runtime.cleanup_origin(event.unified_msg_origin)
        logger.info(
            "enhance-mode | runtime session cache cleaned | origin=%s",
            event.unified_msg_origin,
        )

    @llm_tool(name="grok_web_search")
    async def grok_web_search(self, event: AstrMessageEvent, query: str) -> str:
        """Search live web information with configured provider and return plain text.

        Args:
            query(string): Required. Search query text.
        """
        cfg = self._cfg()
        if not cfg.web_search.enable:
            return "Web search tool is disabled in enhance mode config."

        clean_query = str(query or "").strip()
        if not clean_query:
            return "Invalid `query`: empty."

        result = await self._run_web_search(event, clean_query, cfg)
        return self._format_web_search_tool_result(result, cfg)

    @llm_tool(name="enhance_browser_open")
    async def enhance_browser_open(self, event: AstrMessageEvent, url: str) -> str:
        """Open a webpage with agent-browser in the current chat session.

        Args:
            url(string): Required. Absolute URL, for example `https://example.com`.
        """
        cfg = self._cfg()
        if not cfg.browser_tool.enable:
            return "Browser tool is disabled in enhance mode config."

        clean_url = str(url or "").strip()
        if not clean_url:
            return "Invalid `url`: empty."

        result = await self._run_browser_tool_command(event, ["open", clean_url], cfg)
        return self._format_browser_tool_result(result)

    @llm_tool(name="enhance_browser_snapshot")
    async def enhance_browser_snapshot(
        self, event: AstrMessageEvent, interactive_refs: bool = True
    ) -> str:
        """Get the current page snapshot from agent-browser.

        Args:
            interactive_refs(boolean): Optional. True means include refs like `@e2` for later actions.
        """
        cfg = self._cfg()
        if not cfg.browser_tool.enable:
            return "Browser tool is disabled in enhance mode config."

        command_args = ["snapshot"]
        if interactive_refs:
            command_args.append("-i")
        result = await self._run_browser_tool_command(event, command_args, cfg)
        return self._format_browser_tool_result(result)

    @llm_tool(name="enhance_browser_action")
    async def enhance_browser_action(
        self,
        event: AstrMessageEvent,
        action: str,
        target: str = "",
        value: str = "",
    ) -> str:
        """Run one browser action on the current page.

        Args:
            action(string): Required. One of `click`, `fill`, `type`, `press`, `get_text`, `get_html`, `get_value`, `get_title`, `get_url`, `scroll`.
            target(string): Optional. Selector or ref like `@e2`. For `press`, pass the key name. For `scroll`, pass direction: `up/down/left/right`.
            value(string): Optional. Text for `fill/type`, or pixel count for `scroll`.
        """
        cfg = self._cfg()
        if not cfg.browser_tool.enable:
            return "Browser tool is disabled in enhance mode config."

        normalized_action = str(action or "").strip().lower()
        clean_target = str(target or "").strip()
        clean_value = str(value or "")

        if normalized_action == "click":
            if not clean_target:
                return "Invalid `target`: empty for `click`."
            command_args = ["click", clean_target]
        elif normalized_action == "fill":
            if not clean_target:
                return "Invalid `target`: empty for `fill`."
            if not clean_value:
                return "Invalid `value`: empty for `fill`."
            command_args = ["fill", clean_target, clean_value]
        elif normalized_action == "type":
            if not clean_target:
                return "Invalid `target`: empty for `type`."
            if not clean_value:
                return "Invalid `value`: empty for `type`."
            command_args = ["type", clean_target, clean_value]
        elif normalized_action == "press":
            if not clean_target:
                return "Invalid `target`: empty for `press`."
            command_args = ["press", clean_target]
        elif normalized_action == "get_text":
            command_args = ["get", "text", clean_target or "body"]
        elif normalized_action == "get_html":
            command_args = ["get", "html", clean_target or "html"]
        elif normalized_action == "get_value":
            if not clean_target:
                return "Invalid `target`: empty for `get_value`."
            command_args = ["get", "value", clean_target]
        elif normalized_action == "get_title":
            command_args = ["get", "title"]
        elif normalized_action == "get_url":
            command_args = ["get", "url"]
        elif normalized_action == "scroll":
            direction = clean_target.lower()
            if direction not in {"up", "down", "left", "right"}:
                return "Invalid `target` for `scroll`: use `up`, `down`, `left`, or `right`."
            command_args = ["scroll", direction]
            if clean_value.strip():
                command_args.append(clean_value.strip())
        else:
            return (
                "Invalid `action`. Supported values: "
                "`click`, `fill`, `type`, `press`, `get_text`, `get_html`, "
                "`get_value`, `get_title`, `get_url`, `scroll`."
            )

        result = await self._run_browser_tool_command(event, command_args, cfg)
        return self._format_browser_tool_result(result)

    @llm_tool(name="enhance_browser_wait")
    async def enhance_browser_wait(
        self,
        event: AstrMessageEvent,
        mode: str = "load",
        target: str = "networkidle",
        state: str = "visible",
    ) -> str:
        """Wait for page state changes with agent-browser.

        Args:
            mode(string): Optional. One of `load`, `selector`, `text`, `url`, `time`.
            target(string): Optional. Load state, selector/ref, text, URL pattern, or milliseconds depending on mode.
            state(string): Optional. Only for `selector` mode. One of `visible`, `hidden`, `attached`, `detached`.
        """
        cfg = self._cfg()
        if not cfg.browser_tool.enable:
            return "Browser tool is disabled in enhance mode config."

        normalized_mode = str(mode or "load").strip().lower()
        clean_target = str(target or "").strip()
        normalized_state = str(state or "visible").strip().lower() or "visible"

        if normalized_mode == "load":
            load_state = clean_target or "networkidle"
            if load_state not in {"load", "domcontentloaded", "networkidle"}:
                return (
                    "Invalid `target` for `load` mode. "
                    "Use `load`, `domcontentloaded`, or `networkidle`."
                )
            command_args = ["wait", "--load", load_state]
        elif normalized_mode == "selector":
            if not clean_target:
                return "Invalid `target`: empty for `selector` mode."
            command_args = ["wait", clean_target]
            if normalized_state != "visible":
                if normalized_state not in {"hidden", "attached", "detached"}:
                    return (
                        "Invalid `state` for `selector` mode. "
                        "Use `visible`, `hidden`, `attached`, or `detached`."
                    )
                command_args.extend(["--state", normalized_state])
        elif normalized_mode == "text":
            if not clean_target:
                return "Invalid `target`: empty for `text` mode."
            command_args = ["wait", "--text", clean_target]
        elif normalized_mode == "url":
            if not clean_target:
                return "Invalid `target`: empty for `url` mode."
            command_args = ["wait", "--url", clean_target]
        elif normalized_mode == "time":
            if not clean_target:
                return "Invalid `target`: empty for `time` mode."
            try:
                wait_ms = int(clean_target)
            except ValueError:
                return "Invalid `target` for `time` mode: must be an integer millisecond value."
            if wait_ms < 0:
                return "Invalid `target` for `time` mode: must be >= 0."
            command_args = ["wait", str(wait_ms)]
        else:
            return "Invalid `mode`. Supported values: `load`, `selector`, `text`, `url`, `time`."

        result = await self._run_browser_tool_command(event, command_args, cfg)
        return self._format_browser_tool_result(result)

    @llm_tool(name="enhance_browser_close")
    async def enhance_browser_close(self, event: AstrMessageEvent) -> str:
        """Close the current chat session browser managed by agent-browser."""
        cfg = self._cfg()
        if not cfg.browser_tool.enable:
            return "Browser tool is disabled in enhance mode config."

        result = await self._run_browser_tool_command(event, ["close"], cfg)
        return self._format_browser_tool_result(result)

    @llm_tool(name="enhance_get_ban_list_status")
    async def get_ban_list_status(
        self,
        event: AstrMessageEvent,
        user_id: str = "",
        max_results: int = 20,
    ) -> str:
        """Get ban-list status maintained by enhance mode.

        Args:
            user_id(string): Optional. Target user ID to query. Empty means list active bans.
            max_results(number): Optional. Maximum number of active bans to return when user_id is empty.
        """
        cfg = self._cfg()
        if not cfg.group_features.ban_control_enable:
            return "Ban control is disabled in enhance mode config."

        scope_id = self._ban_scope_id(event)
        if not scope_id:
            return (
                "Ban list is group-scoped. "
                "Please call this tool in current group chat context."
            )

        released_count = self.ban_store.cleanup_expired(scope_id=scope_id)
        target_user_id = str(user_id or "").strip().lstrip("@")
        if (
            target_user_id
            and not cfg.group_features.ban_allow_admin
            and target_user_id in self._get_admin_sid_set()
        ):
            return (
                f"User `{target_user_id}` is AstrBot admin and protected from ban "
                f"(scope={scope_id}, auto released this round: {released_count})."
            )

        if target_user_id:
            active_ban = self.ban_store.get_active_ban(
                scope_id=scope_id, user_id=target_user_id
            )
            if not active_ban:
                return (
                    f"User `{target_user_id}` is not banned now. "
                    f"(scope={scope_id}, auto released this round: {released_count})"
                )

            expires_at_text = datetime.datetime.fromtimestamp(
                active_ban.expires_at
            ).strftime("%Y-%m-%d %H:%M:%S")
            return (
                f"User `{target_user_id}` is currently banned.\n"
                f"- Remaining: {self._format_duration(active_ban.remaining_seconds)}\n"
                f"- Expires at: {expires_at_text}\n"
                f"- Scope: {scope_id}\n"
                f"- Auto released this round: {released_count}"
            )

        try:
            parsed_max_results = int(max_results)
        except (TypeError, ValueError):
            parsed_max_results = 20
        limit = max(1, min(parsed_max_results, 200))
        records = self.ban_store.list_active_bans(scope_id=scope_id, limit=limit)
        if not records:
            return (
                f"No active bans in scope `{scope_id}`. "
                f"(auto released this round: {released_count})"
            )

        lines = [
            f"Active bans in scope `{scope_id}` ({len(records)} shown, limit={limit}). "
            f"Auto released this round: {released_count}",
        ]
        for idx, record in enumerate(records, start=1):
            expire_text = datetime.datetime.fromtimestamp(record.expires_at).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            lines.append(
                f"{idx}. user_id={record.user_id}, remaining={self._format_duration(record.remaining_seconds)}, "
                f"expires_at={expire_text}"
            )
        return "\n".join(lines)

    @llm_tool(name="enhance_ban_user")
    async def ban_user(
        self,
        event: AstrMessageEvent,
        user_id: str,
        duration: str = "10m",
    ) -> str:
        """Temporarily ban a user in current group scope.

        Args:
            user_id(string): Required. Target user ID to ban.
            duration(string): Optional. Ban duration like 60s, 10m, 2h, 1d. Default is 10m.
        """
        cfg = self._cfg()
        if not cfg.group_features.ban_control_enable:
            return "Ban control is disabled in enhance mode config."

        scope_id = self._ban_scope_id(event)
        if not scope_id:
            return (
                "Ban action is group-scoped. "
                "Please call this tool in current group chat context."
            )

        target_user_id = str(user_id or "").strip().lstrip("@")
        if not target_user_id:
            return "Invalid `user_id`: empty."

        duration_seconds = parse_duration_seconds(duration)
        if duration_seconds is None:
            return "Invalid `duration`. Use formats like `60s`, `10m`, `2h`, `1d`."

        if (
            not cfg.group_features.ban_allow_admin
            and target_user_id in self._get_admin_sid_set()
        ):
            return (
                f"User `{target_user_id}` is AstrBot admin and protected from ban "
                f"(scope={scope_id})."
            )

        final_duration = max(
            1,
            min(duration_seconds, cfg.group_features.ban_max_duration_sec),
        )
        expires_at = self.ban_store.ban_user(
            scope_id=scope_id,
            user_id=target_user_id,
            duration_seconds=final_duration,
            source_origin=event.unified_msg_origin,
        )
        expire_time = datetime.datetime.fromtimestamp(expires_at).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        logger.info(
            "enhance-mode | ban applied | scope=%s user_id=%s duration=%s origin=%s",
            scope_id,
            target_user_id,
            self._format_duration(final_duration),
            event.unified_msg_origin,
        )
        return (
            f"Banned user `{target_user_id}` in scope `{scope_id}`.\n"
            f"- Duration: {self._format_duration(final_duration)}\n"
            f"- Expires at: {expire_time}"
        )

    @llm_tool(name="enhance_unban_user")
    async def unban_user(self, event: AstrMessageEvent, user_id: str) -> str:
        """Unban a user in current group scope.

        Args:
            user_id(string): Required. Target user ID to unban.
        """
        cfg = self._cfg()
        if not cfg.group_features.ban_control_enable:
            return "Ban control is disabled in enhance mode config."

        scope_id = self._ban_scope_id(event)
        if not scope_id:
            return (
                "Unban action is group-scoped. "
                "Please call this tool in current group chat context."
            )

        target_user_id = str(user_id or "").strip().lstrip("@")
        if not target_user_id:
            return "Invalid `user_id`: empty."

        removed = self.ban_store.unban_user(scope_id=scope_id, user_id=target_user_id)
        if not removed:
            return f"User `{target_user_id}` is not banned in scope `{scope_id}`."
        logger.info(
            "enhance-mode | unban applied | scope=%s user_id=%s origin=%s",
            scope_id,
            target_user_id,
            event.unified_msg_origin,
        )
        return f"Unbanned user `{target_user_id}` in scope `{scope_id}`."

    @staticmethod
    def _make_text_tool_result(text: str) -> mcp_types.CallToolResult:
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=str(text or ""))]
        )

    @llm_tool(name="enhance_use_image")
    async def use_image(
        self,
        event: AstrMessageEvent,
        message_id: str,
        image_index: int = 1,
        attach_to_model: bool = True,
        write_to_history: bool = True,
        prompt: str = "",
    ) -> AsyncGenerator[mcp_types.CallToolResult, None]:
        """Use one image from runtime history for multimodal input and/or history backfill.

        Args:
            message_id(string): Required. Message ID in history header, for example `123456`.
            image_index(number): Optional. One-based index of image in that message. Default is 1.
            attach_to_model(bool): Optional. Attach image bytes to this run context. Default is true.
            write_to_history(bool): Optional. Replace `[Image]` with description in history. Default is true.
            prompt(string): Optional. Override image caption prompt for this call.
        """
        cfg = self._cfg()
        if not cfg.group_history_enabled:
            yield self._make_text_tool_result(
                "Group history enhancement is disabled in enhance mode config."
            )
            return

        normalized_message_id = self._normalize_message_id(message_id)
        if not normalized_message_id:
            yield self._make_text_tool_result("Invalid `message_id`: empty.")
            return

        try:
            index_number = int(image_index)
        except (TypeError, ValueError):
            yield self._make_text_tool_result(
                "Invalid `image_index`: must be a positive integer."
            )
            return
        if index_number <= 0:
            yield self._make_text_tool_result("Invalid `image_index`: must be >= 1.")
            return
        image_idx = index_number - 1

        attach_requested = bool(attach_to_model)
        history_requested = bool(write_to_history)
        if not attach_requested and not history_requested:
            yield self._make_text_tool_result(
                "Invalid mode: `attach_to_model` and `write_to_history` cannot both be false."
            )
            return

        origin = event.unified_msg_origin
        message_registry = self.runtime.image_message_registry.get(origin, {})
        message_entry = message_registry.get(normalized_message_id)
        if not isinstance(message_entry, dict):
            yield self._make_text_tool_result(
                f"Image message `{normalized_message_id}` not found in current runtime history. "
                "Try a newer message ID from current chat context."
            )
            return

        urls_raw = message_entry.get("urls")
        if not isinstance(urls_raw, list) or not urls_raw:
            yield self._make_text_tool_result(
                f"No image records found for message `{normalized_message_id}`."
            )
            return
        if image_idx >= len(urls_raw):
            yield self._make_text_tool_result(
                f"`image_index` out of range. message `{normalized_message_id}` has "
                f"{len(urls_raw)} image(s)."
            )
            return

        image_url = str(urls_raw[image_idx] or "").strip()
        if not image_url:
            yield self._make_text_tool_result(
                f"Image URL is unavailable for message `{normalized_message_id}` "
                f"at index {index_number}."
            )
            return

        captions_raw = message_entry.get("captions")
        if isinstance(captions_raw, dict):
            captions_map = captions_raw
        else:
            captions_map = {}
            message_entry["captions"] = captions_map

        caption = ""
        caption_cached = False
        cached_caption = captions_map.get(image_idx)
        if isinstance(cached_caption, str) and cached_caption.strip():
            caption = cached_caption.strip()
            caption_cached = True
        elif history_requested:
            try:
                if not cfg.group_history.image_caption:
                    yield self._make_text_tool_result(
                        "Image caption is disabled in enhance mode config."
                    )
                    return
                final_prompt = (
                    str(prompt or "").strip() or cfg.group_history.image_caption_prompt
                )
                caption = await self._get_image_caption(
                    image_url=image_url,
                    provider_id=cfg.group_history.image_caption_provider_id,
                    prompt=final_prompt,
                    timeout_sec=cfg.global_settings.timeouts.image_caption_sec,
                )
                caption = str(caption or "").strip()
                if caption:
                    captions_map[image_idx] = caption
            except Exception as e:
                logger.exception(
                    "enhance-mode | use_image caption failed | origin=%s msg_id=%s image_index=%s error=%s",
                    origin,
                    normalized_message_id,
                    index_number,
                    e,
                )
                yield self._make_text_tool_result(
                    f"Failed to get image description: {e}"
                )
                return

        attach_success = False
        attach_error = ""
        if attach_requested:
            resolved_paths_raw = message_entry.get("resolved_paths")
            if not isinstance(resolved_paths_raw, list) or len(
                resolved_paths_raw
            ) != len(urls_raw):
                resolved_paths_raw = [""] * len(urls_raw)
                message_entry["resolved_paths"] = resolved_paths_raw

            selected_ref = str(
                resolved_paths_raw[image_idx] or urls_raw[image_idx] or ""
            ).strip()
            if not selected_ref:
                attach_error = (
                    f"Image reference is unavailable for message `{normalized_message_id}` "
                    f"at index {index_number}."
                )
            else:
                try:
                    local_path = await self._resolve_image_ref_to_local_path(
                        selected_ref
                    )
                    if not local_path:
                        attach_error = (
                            f"Failed to resolve image path for message `{normalized_message_id}` "
                            f"index {index_number}."
                        )
                    else:
                        resolved_paths_raw[image_idx] = local_path
                        image_b64, mime_type = self._encode_image_file(local_path)
                        yield mcp_types.CallToolResult(
                            content=[
                                mcp_types.ImageContent(
                                    type="image",
                                    data=image_b64,
                                    mimeType=mime_type,
                                )
                            ]
                        )
                        attach_success = True
                        logger.info(
                            "enhance-mode | use_image attach success | origin=%s msg_id=%s image_index=%s mime=%s",
                            origin,
                            normalized_message_id,
                            index_number,
                            mime_type,
                        )
                except Exception as e:
                    attach_error = str(e)
                    logger.exception(
                        "enhance-mode | use_image attach failed | origin=%s msg_id=%s image_index=%s error=%s",
                        origin,
                        normalized_message_id,
                        index_number,
                        e,
                    )

        history_success = False
        history_error = ""
        if history_requested:
            if not caption:
                history_error = "Image description is empty."
            else:
                history_success = self._apply_image_caption_to_history(
                    origin=origin,
                    message_id=normalized_message_id,
                    image_index=image_idx,
                    caption=caption,
                )
                if history_success:
                    logger.info(
                        "enhance-mode | use_image history write success | origin=%s msg_id=%s image_index=%s",
                        origin,
                        normalized_message_id,
                        index_number,
                    )
                else:
                    history_error = (
                        "Failed to apply image description back to runtime history."
                    )

        if attach_requested and history_requested:
            success = attach_success and history_success
        elif attach_requested:
            success = attach_success
        else:
            success = history_success

        payload: dict[str, object] = {
            "status": "ok" if success else "failed",
            "success": success,
            "origin": origin,
            "message_id": normalized_message_id,
            "image_index": index_number,
            "attach_requested": attach_requested,
            "write_to_history_requested": history_requested,
            "attach_success": attach_success if attach_requested else None,
            "write_to_history_success": history_success if history_requested else None,
            "description_cached": caption_cached,
        }
        if attach_requested and not history_requested:
            payload["description"] = caption
        if attach_error:
            payload["attach_error"] = attach_error
        if history_error:
            payload["write_to_history_error"] = history_error

        logger.info(
            "enhance-mode | use_image done | origin=%s msg_id=%s image_index=%s attach=%s/%s write=%s/%s success=%s",
            origin,
            normalized_message_id,
            index_number,
            attach_success,
            attach_requested,
            history_success,
            history_requested,
            success,
        )
        yield self._make_text_tool_result(json.dumps(payload, ensure_ascii=False))

    @llm_tool(name="enhance_memory_rag_write")
    async def memory_rag_write(
        self,
        event: AstrMessageEvent,
        content: str,
        related_role_ids: str,
        memory_time: str = "",
        group_scope: str = "",
        group_id: str = "",
        platform_id: str = "",
        extra_metadata_json: str = "{}",
    ) -> str:
        """Write a memory record into enhance-mode memory RAG store.

        Args:
            content(string): Required. Memory content text.
            related_role_ids(string): Required. Role IDs in JSON array or comma-separated string.
            memory_time(string): Optional. Memory time as unix timestamp or ISO datetime.
            group_scope(string): Optional. Full group scope, for example `qq:123456`.
            group_id(string): Optional. Group ID, combined with platform_id when group_scope is empty.
            platform_id(string): Optional. Platform ID used with group_id.
            extra_metadata_json(string): Optional. JSON object string for extra metadata.
        """
        ready, reason = self._check_memory_rag_ready()
        if not ready:
            return reason
        if self.memory_rag_store is None:
            return "Memory RAG store is not initialized."

        clean_content = str(content or "").strip()
        if not clean_content:
            return "Invalid `content`: empty."

        role_ids = self._parse_role_ids(related_role_ids)
        if not role_ids:
            return "Invalid `related_role_ids`: at least one role ID is required."

        ts = self._parse_optional_timestamp(memory_time)
        if memory_time and ts is None:
            return (
                "Invalid `memory_time`. Use unix timestamp or ISO datetime "
                "(for example `1735689600` or `2026-01-01 12:00:00`)."
            )
        memory_ts = ts if ts is not None else time.time()

        final_scope, final_group_id, final_platform_id = self._resolve_memory_scope(
            event, group_scope, group_id, platform_id
        )

        cfg = self._cfg()
        embedding_provider = self._resolve_embedding_provider(cfg)
        if not embedding_provider:
            return (
                "No embedding provider is available. "
                "Please configure one in AstrBot provider settings."
            )

        start_ts = time.perf_counter()
        logger.info(
            "enhance-mode | memory_rag_write start | origin=%s roles=%s content_len=%s scope=%s",
            event.unified_msg_origin,
            len(role_ids),
            len(clean_content),
            final_scope,
        )
        try:
            embedding = await embedding_provider.get_embedding(clean_content)
        except Exception as e:
            logger.exception("enhance-mode | memory_rag_write embedding failed: %s", e)
            return f"Failed to generate embedding: {e}"

        metadata = self._parse_extra_metadata(extra_metadata_json)
        try:
            memory_id = self.memory_rag_store.add_memory(
                content=clean_content,
                embedding=embedding,
                role_ids=role_ids,
                memory_time=memory_ts,
                group_scope=final_scope,
                group_id=final_group_id,
                platform_id=final_platform_id,
                extra_metadata=metadata,
            )
        except Exception as e:
            logger.exception("enhance-mode | memory_rag_write store failed: %s", e)
            return f"Failed to write memory: {e}"

        elapsed_ms = (time.perf_counter() - start_ts) * 1000
        logger.info(
            "enhance-mode | memory_rag_write done | memory_id=%s provider=%s embedding_dim=%s elapsed_ms=%.1f",
            memory_id,
            self._provider_label(embedding_provider),
            len(embedding),
            elapsed_ms,
        )

        return json.dumps(
            {
                "status": "ok",
                "memory_id": memory_id,
                "memory_time": memory_ts,
                "memory_time_iso": self._format_timestamp_iso(memory_ts),
                "group_scope": final_scope,
                "group_id": final_group_id,
                "platform_id": final_platform_id,
                "related_role_ids": role_ids,
            },
            ensure_ascii=False,
        )

    @llm_tool(name="enhance_memory_rag_read")
    async def memory_rag_read(
        self,
        event: AstrMessageEvent,
        query: str = "",
        related_role_ids: str = "",
        role_match_mode: str = "any",
        start_time: str = "",
        end_time: str = "",
        group_scope: str = "",
        group_id: str = "",
        platform_id: str = "",
        sort_by: str = "relevance",
        sort_order: str = "desc",
        max_results: int = 10,
        embedding_recall_k: int = 0,
        ignore_group_id: bool = False,
    ) -> str:
        """Read memory records from enhance-mode memory RAG store.

        Args:
            query(string): Optional. Query text for embedding retrieval.
            related_role_ids(string): Optional. Role IDs in JSON array or comma-separated string.
            role_match_mode(string): Optional. `any` or `all`.
            start_time(string): Optional. Start time as unix timestamp or ISO datetime.
            end_time(string): Optional. End time as unix timestamp or ISO datetime.
            group_scope(string): Optional. Full group scope, for example `qq:123456`.
            group_id(string): Optional. Group ID, combined with platform_id when group_scope is empty.
            platform_id(string): Optional. Platform ID used with group_id.
            sort_by(string): Optional. `relevance` or `time`.
            sort_order(string): Optional. `desc` or `asc`.
            max_results(number): Optional. Number of results to return.
            embedding_recall_k(number): Optional. Top-K cutoff for embedding recall before final sorting.
            ignore_group_id(boolean): Optional. When true, do not auto-apply current group filter, allowing cross-group read.
        """
        ready, reason = self._check_memory_rag_ready()
        if not ready:
            return reason
        if self.memory_rag_store is None:
            return "Memory RAG store is not initialized."

        start_time_ts = self._parse_optional_timestamp(start_time)
        end_time_ts = self._parse_optional_timestamp(end_time)
        if start_time and start_time_ts is None:
            return (
                "Invalid `start_time`. Use unix timestamp or ISO datetime "
                "(for example `1735689600` or `2026-01-01 12:00:00`)."
            )
        if end_time and end_time_ts is None:
            return (
                "Invalid `end_time`. Use unix timestamp or ISO datetime "
                "(for example `1735689600` or `2026-01-01 12:00:00`)."
            )
        if (
            start_time_ts is not None
            and end_time_ts is not None
            and start_time_ts > end_time_ts
        ):
            return "Invalid time range: `start_time` cannot be greater than `end_time`."

        normalized_ignore_group = (
            ignore_group_id
            if isinstance(ignore_group_id, bool)
            else str(ignore_group_id or "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        if normalized_ignore_group:
            final_scope = str(group_scope or "").strip()
            final_group_id = str(group_id or "").strip()
            final_platform_id = str(platform_id or "").strip()
            if not final_scope and final_group_id:
                final_scope = (
                    f"{final_platform_id}:{final_group_id}"
                    if final_platform_id
                    else final_group_id
                )
        else:
            final_scope, final_group_id, final_platform_id = self._resolve_memory_scope(
                event, group_scope, group_id, platform_id
            )
        normalized_roles = self._parse_role_ids(related_role_ids)
        normalized_mode = (
            "all" if str(role_match_mode or "").strip().lower() == "all" else "any"
        )
        normalized_sort_by = self._normalize_sort_by(sort_by)
        normalized_sort_order = self._normalize_sort_order(sort_order)

        cfg = self._cfg()
        max_allowed = max(1, cfg.memory_rag.max_return_results)
        try:
            requested_max = int(max_results)
        except (TypeError, ValueError):
            requested_max = 10
        if requested_max <= 0:
            final_max_results = max_allowed
        else:
            final_max_results = min(requested_max, max_allowed)

        try:
            parsed_recall_k = int(embedding_recall_k)
        except (TypeError, ValueError):
            parsed_recall_k = 0
        effective_recall_k = (
            parsed_recall_k
            if parsed_recall_k > 0
            else int(cfg.memory_rag.default_recall_k)
        )

        query_embedding: list[float] | None = None
        clean_query = str(query or "").strip()
        read_start_ts = time.perf_counter()
        logger.info(
            "enhance-mode | memory_rag_read start | origin=%s query_len=%s role_count=%s scope=%s max_results=%s sort=%s/%s",
            event.unified_msg_origin,
            len(clean_query),
            len(normalized_roles),
            final_scope,
            final_max_results,
            normalized_sort_by,
            normalized_sort_order,
        )
        if clean_query:
            embedding_provider = self._resolve_embedding_provider(cfg)
            if not embedding_provider:
                return (
                    "No embedding provider is available. "
                    "Please configure one in AstrBot provider settings."
                )
            try:
                query_embedding = await embedding_provider.get_embedding(clean_query)
            except Exception as e:
                logger.exception(
                    "enhance-mode | memory_rag_read embedding failed: %s", e
                )
                return f"Failed to generate query embedding: {e}"

        try:
            records = self.memory_rag_store.search_memories(
                query_embedding=query_embedding,
                embedding_recall_k=effective_recall_k,
                role_ids=normalized_roles,
                role_match_mode=normalized_mode,
                group_scope=final_scope,
                group_id=final_group_id,
                platform_id=final_platform_id,
                start_time=start_time_ts,
                end_time=end_time_ts,
                sort_by=normalized_sort_by,
                sort_order=normalized_sort_order,
                max_results=final_max_results,
            )
        except Exception as e:
            logger.exception("enhance-mode | memory_rag_read search failed: %s", e)
            return f"Failed to read memories: {e}"

        elapsed_ms = (time.perf_counter() - read_start_ts) * 1000
        logger.info(
            "enhance-mode | memory_rag_read done | origin=%s result_count=%s elapsed_ms=%.1f",
            event.unified_msg_origin,
            len(records),
            elapsed_ms,
        )

        return json.dumps(
            {
                "status": "ok",
                "count": len(records),
                "query": clean_query,
                "filters": {
                    "related_role_ids": normalized_roles,
                    "role_match_mode": normalized_mode,
                    "start_time": start_time_ts,
                    "end_time": end_time_ts,
                    "ignore_group_id": normalized_ignore_group,
                    "group_scope": final_scope,
                    "group_id": final_group_id,
                    "platform_id": final_platform_id,
                },
                "sort": {"by": normalized_sort_by, "order": normalized_sort_order},
                "max_results": final_max_results,
                "embedding_recall_k": effective_recall_k if clean_query else None,
                "records": records,
            },
            ensure_ascii=False,
        )

    @filter.command_group("enhance")
    def enhance(self) -> None:
        pass

    @permission_type(PermissionType.ADMIN)
    @enhance.command("rag-webui")
    async def rag_webui(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        cfg = self._cfg()
        webui_cfg = cfg.memory_rag_webui
        logger.info(
            "enhance-mode | rag-webui command received | origin=%s webui_enable=%s",
            event.unified_msg_origin,
            webui_cfg.enable,
        )
        if not webui_cfg.enable:
            yield event.plain_result(
                "Memory RAG WebUI is disabled.\n"
                "Please enable `memory_rag_webui.enable` in enhance plugin config."
            )
            return

        if self.rag_webui_server is None:
            await self._start_memory_rag_webui()

        if self.rag_webui_server is None:
            yield event.plain_result(
                "Memory RAG WebUI failed to start.\n"
                "Please check AstrBot logs for error details."
            )
            return

        message = (
            "Enhance Memory RAG WebUI\n\n"
            f"URL: {self._memory_rag_webui_url(cfg)}\n"
            "Functions: login, filters, pagination, detail view, delete memory.\n"
        )
        if self.rag_webui_server.password_generated:
            message += (
                "\nPassword is auto generated for this run. "
                "Please check AstrBot logs for the generated password."
            )
        yield event.plain_result(message)

    async def terminate(self) -> None:
        logger.info("enhance-mode | plugin terminating")
        await self._stop_memory_rag_webui()
        logger.info("enhance-mode | plugin terminated")
