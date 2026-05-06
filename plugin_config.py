import math
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL_CHOICE_PROMPT = (
    "你当前的人格面具是：{persona_name}\n"
    "人格设定如下：\n{persona_mask}\n\n"
    "你正在群聊中扮演助手。以下是最近 {stack_size} 条群聊消息：\n"
    "{messages}\n\n"
    "额外历史上下文（最近 {history_count} 条）：\n"
    "{history_context}\n\n"
    "请严格站在该人格的角度判断你是否应该主动回复。"
    "如果需要回复，只输出 REPLY；如果不需要回复，只输出 SKIP。"
)
DEFAULT_WEB_SEARCH_SYSTEM_PROMPT = (
    "You are a web research assistant. Use live web search/browsing when answering. "
    "Return ONLY a single JSON object with keys: "
    "content (string), sources (array of objects with url/title/snippet when possible). "
    "Keep content concise and evidence-backed. "
    "IMPORTANT: Do NOT use Markdown formatting in the content field - use plain text only."
)


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _to_pos_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_probability(value: Any, default: float) -> float:
    parsed = _to_float(value, default)
    if not math.isfinite(parsed):
        parsed = default
    return min(1.0, max(0.0, parsed))


def _parse_whitelist(value: Any) -> list[str]:
    if isinstance(value, str):
        return [token.strip() for token in value.split(",") if token.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(token).strip() for token in value if str(token).strip()]
    return []


@dataclass(frozen=True)
class GroupHistoryEnhancementConfig:
    enable: bool = False
    max_messages: int = 300
    include_sender_id: bool = True
    include_role_tag: bool = True
    image_caption: bool = False
    image_caption_provider_id: str = ""
    image_caption_prompt: str = "Please describe the image using Chinese."


@dataclass(frozen=True)
class ActiveReplyConfig:
    enable: bool = False
    mode: str = "probability"
    possibility: float = 0.1
    model_stack_size: int = 8
    model_history_messages: int = 0
    model_choice_provider_id: str = ""
    model_choice_prompt: str = DEFAULT_MODEL_CHOICE_PROMPT
    whitelist: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GroupFeatureEnhancementConfig:
    react_mode_enable: bool = False
    role_display: bool = True
    mention_parse: bool = True
    ban_control_enable: bool = True
    ban_max_duration_sec: int = 2592000
    ban_allow_admin: bool = False
    role_prompt_enable: bool = False
    role_admin_prompt: str = ""
    role_member_prompt: str = ""


@dataclass(frozen=True)
class GlobalLruConfig:
    max_origins: int = 500


@dataclass(frozen=True)
class GlobalTimeoutConfig:
    image_caption_sec: float = 45.0
    model_choice_sec: float = 45.0


@dataclass(frozen=True)
class GlobalSettingsConfig:
    lru_cache: GlobalLruConfig = field(default_factory=GlobalLruConfig)
    timeouts: GlobalTimeoutConfig = field(default_factory=GlobalTimeoutConfig)


@dataclass(frozen=True)
class WebSearchConfig:
    enable: bool = False
    provider_id: str = ""
    system_prompt: str = DEFAULT_WEB_SEARCH_SYSTEM_PROMPT
    timeout_sec: float = 60.0
    request_mode: str = "auto"
    base_url_override: str = ""
    show_sources: bool = False
    max_sources: int = 5


@dataclass(frozen=True)
class BrowserToolConfig:
    enable: bool = False
    command: str = "agent-browser"
    timeout_sec: float = 60.0
    headed: bool = False
    persist_session: bool = True
    content_boundaries: bool = True
    max_output_chars: int = 12000
    allowed_domains: list[str] = field(default_factory=list)
    idle_timeout_ms: int = 300000


@dataclass(frozen=True)
class MemoryRAGConfig:
    enable: bool = True
    embedding_provider_id: str = ""
    default_recall_k: int = 20
    max_return_results: int = 200


@dataclass(frozen=True)
class MemoryRAGWebUIConfig:
    enable: bool = False
    host: str = "127.0.0.1"
    port: int = 8899
    access_password: str = ""
    session_timeout: int = 3600


@dataclass(frozen=True)
class PluginConfig:
    group_history: GroupHistoryEnhancementConfig = field(
        default_factory=GroupHistoryEnhancementConfig
    )
    active_reply: ActiveReplyConfig = field(default_factory=ActiveReplyConfig)
    group_features: GroupFeatureEnhancementConfig = field(
        default_factory=GroupFeatureEnhancementConfig
    )
    global_settings: GlobalSettingsConfig = field(default_factory=GlobalSettingsConfig)
    web_search: WebSearchConfig = field(default_factory=WebSearchConfig)
    browser_tool: BrowserToolConfig = field(default_factory=BrowserToolConfig)
    memory_rag: MemoryRAGConfig = field(default_factory=MemoryRAGConfig)
    memory_rag_webui: MemoryRAGWebUIConfig = field(default_factory=MemoryRAGWebUIConfig)

    @property
    def group_history_enabled(self) -> bool:
        return self.group_features.react_mode_enable and self.group_history.enable

    @property
    def active_reply_enabled(self) -> bool:
        return self.group_features.react_mode_enable and self.active_reply.enable


def parse_plugin_config(raw: dict[str, Any] | None) -> PluginConfig:
    raw = raw or {}

    group_features_raw = raw.get("group_features", {})
    group_features = GroupFeatureEnhancementConfig(
        react_mode_enable=_to_bool(group_features_raw.get("react_mode_enable"), False),
        role_display=_to_bool(group_features_raw.get("role_display"), True),
        mention_parse=_to_bool(group_features_raw.get("mention_parse"), True),
        ban_control_enable=_to_bool(group_features_raw.get("ban_control_enable"), True),
        ban_max_duration_sec=max(
            1, _to_int(group_features_raw.get("ban_max_duration_sec"), 2592000)
        ),
        ban_allow_admin=_to_bool(group_features_raw.get("ban_allow_admin"), False),
        role_prompt_enable=_to_bool(
            group_features_raw.get("role_prompt_enable"), False
        ),
        role_admin_prompt=str(group_features_raw.get("role_admin_prompt") or ""),
        role_member_prompt=str(group_features_raw.get("role_member_prompt") or ""),
    )

    group_history_raw = raw.get("group_history_enhancement", {})
    group_history = GroupHistoryEnhancementConfig(
        enable=_to_bool(group_history_raw.get("enable"), False),
        max_messages=max(1, _to_int(group_history_raw.get("max_messages"), 300)),
        include_sender_id=_to_bool(group_history_raw.get("include_sender_id"), True),
        include_role_tag=_to_bool(group_history_raw.get("include_role_tag"), True),
        image_caption=_to_bool(group_history_raw.get("image_caption"), False),
        image_caption_provider_id=str(
            group_history_raw.get("image_caption_provider_id") or ""
        ),
        image_caption_prompt=str(
            group_history_raw.get("image_caption_prompt")
            or "Please describe the image using Chinese."
        ),
    )

    active_reply_raw = raw.get("active_reply", {})
    mode = str(active_reply_raw.get("mode", "probability")).strip().lower()
    if mode not in {"probability", "model_choice"}:
        mode = "probability"
    active_reply = ActiveReplyConfig(
        enable=_to_bool(active_reply_raw.get("enable"), False),
        mode=mode,
        possibility=_to_probability(active_reply_raw.get("possibility"), 0.1),
        model_stack_size=max(1, _to_int(active_reply_raw.get("model_stack_size"), 8)),
        model_history_messages=max(
            0, _to_int(active_reply_raw.get("model_history_messages"), 0)
        ),
        model_choice_provider_id=str(
            active_reply_raw.get("model_choice_provider_id") or ""
        ).strip(),
        model_choice_prompt=str(
            active_reply_raw.get("model_choice_prompt") or DEFAULT_MODEL_CHOICE_PROMPT
        ),
        whitelist=_parse_whitelist(active_reply_raw.get("whitelist", "")),
    )

    global_settings_raw = raw.get("global_settings", {})
    lru_raw = global_settings_raw.get("lru_cache", {})
    timeouts_raw = global_settings_raw.get("timeouts", {})
    global_settings = GlobalSettingsConfig(
        lru_cache=GlobalLruConfig(
            max_origins=max(1, _to_int(lru_raw.get("max_origins"), 500))
        ),
        timeouts=GlobalTimeoutConfig(
            image_caption_sec=_to_pos_float(
                timeouts_raw.get("image_caption_sec"), 45.0
            ),
            model_choice_sec=_to_pos_float(timeouts_raw.get("model_choice_sec"), 45.0),
        ),
    )

    web_search_raw = raw.get("web_search", {})
    configured_web_search_prompt = str(
        web_search_raw.get("system_prompt") or ""
    ).strip()
    request_mode = str(web_search_raw.get("request_mode") or "auto").strip().lower()
    if request_mode not in {"auto", "responses", "chat_completions"}:
        request_mode = "auto"
    web_search = WebSearchConfig(
        enable=_to_bool(web_search_raw.get("enable"), False),
        provider_id=str(web_search_raw.get("provider_id") or "").strip(),
        system_prompt=(
            configured_web_search_prompt
            if configured_web_search_prompt
            else DEFAULT_WEB_SEARCH_SYSTEM_PROMPT
        ),
        timeout_sec=_to_pos_float(web_search_raw.get("timeout_sec"), 60.0),
        request_mode=request_mode,
        base_url_override=str(web_search_raw.get("base_url_override") or "").strip(),
        show_sources=_to_bool(web_search_raw.get("show_sources"), False),
        max_sources=max(0, _to_int(web_search_raw.get("max_sources"), 5)),
    )

    browser_tool_raw = raw.get("browser_tool", {})
    browser_tool = BrowserToolConfig(
        enable=_to_bool(browser_tool_raw.get("enable"), False),
        command=str(browser_tool_raw.get("command") or "agent-browser").strip()
        or "agent-browser",
        timeout_sec=_to_pos_float(browser_tool_raw.get("timeout_sec"), 60.0),
        headed=_to_bool(browser_tool_raw.get("headed"), False),
        persist_session=_to_bool(browser_tool_raw.get("persist_session"), True),
        content_boundaries=_to_bool(browser_tool_raw.get("content_boundaries"), True),
        max_output_chars=max(
            0, _to_int(browser_tool_raw.get("max_output_chars"), 12000)
        ),
        allowed_domains=_parse_whitelist(browser_tool_raw.get("allowed_domains", "")),
        idle_timeout_ms=max(
            0, _to_int(browser_tool_raw.get("idle_timeout_ms"), 300000)
        ),
    )

    memory_rag_raw = raw.get("memory_rag", {})
    memory_rag = MemoryRAGConfig(
        enable=_to_bool(memory_rag_raw.get("enable"), True),
        embedding_provider_id=str(memory_rag_raw.get("embedding_provider_id") or ""),
        default_recall_k=max(1, _to_int(memory_rag_raw.get("default_recall_k"), 20)),
        max_return_results=max(
            1, _to_int(memory_rag_raw.get("max_return_results"), 200)
        ),
    )

    memory_rag_webui_raw = raw.get("memory_rag_webui", {})
    memory_rag_webui = MemoryRAGWebUIConfig(
        enable=_to_bool(memory_rag_webui_raw.get("enable"), False),
        host=str(memory_rag_webui_raw.get("host") or "127.0.0.1").strip()
        or "127.0.0.1",
        port=max(1, min(65535, _to_int(memory_rag_webui_raw.get("port"), 8899))),
        access_password=str(memory_rag_webui_raw.get("access_password") or ""),
        session_timeout=max(
            60, _to_int(memory_rag_webui_raw.get("session_timeout"), 3600)
        ),
    )

    return PluginConfig(
        group_history=group_history,
        active_reply=active_reply,
        group_features=group_features,
        global_settings=global_settings,
        web_search=web_search,
        browser_tool=browser_tool,
        memory_rag=memory_rag,
        memory_rag_webui=memory_rag_webui,
    )
