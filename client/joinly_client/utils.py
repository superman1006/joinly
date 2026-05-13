import asyncio
import hashlib
import json
import logging
import os
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Never

import httpx
from mcp import ClientSession
from pydantic_ai.mcp import MCPServer
from pydantic_ai.models import Model, infer_model
from pydantic_ai.models.openai import OpenAIModel, OpenAIResponsesModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from joinly_client.prompts import (
    DEFAULT_PROMPT_TEMPLATE,
    DYADIC_INSTRUCTIONS,
    MPC_INSTRUCTIONS,
)
from joinly_client.types import McpClientConfig, ToolExecutor, Transcript

logger = logging.getLogger(__name__)


class _ThinkingPassthroughTransport(httpx.AsyncBaseTransport):
    """透传 reasoning_content 的 HTTP 传输层。

    部分兼容 OpenAI 格式的推理模型（如 mimo、Qwen 思维链版）在响应中返回
    reasoning_content 字段，并要求后续请求将其原样回传；标准 OpenAI 客户端
    不处理该字段，导致 HTTP 400 错误。本 transport 拦截请求/响应，自动完成
    reasoning_content 的缓存与注入。
    """

    def __init__(self) -> None:
        """初始化传输层。"""
        self._inner = httpx.AsyncHTTPTransport()
        # MD5(content) -> reasoning_content
        self._cache: dict[str, str] = {}

    @staticmethod
    def _key(content: object) -> str:
        return hashlib.md5(  # noqa: S324
            json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """拦截请求，注入 reasoning_content；拦截响应，缓存 reasoning_content。"""
        request = self._inject(request)
        response = await self._inner.handle_async_request(request)
        self._capture(response)
        return response

    def _inject(self, request: httpx.Request) -> httpx.Request:
        if not self._cache:
            return request
        try:
            body = json.loads(request.content)
            modified = False
            for msg in body.get("messages", []):
                if msg.get("role") == "assistant":
                    key = self._key(msg.get("content", ""))
                    if key in self._cache:
                        msg["reasoning_content"] = self._cache[key]
                        modified = True
            if not modified:
                return request
            new_body = json.dumps(body, ensure_ascii=False).encode()
            # 不拷贝 content-length，让 httpx 重新计算
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() != "content-length"
            }
            return httpx.Request(
                method=request.method,
                url=request.url,
                headers=headers,
                content=new_body,
            )
        except Exception:  # noqa: BLE001
            return request

    def _capture(self, response: httpx.Response) -> None:
        try:
            body = json.loads(response.content)
            for choice in body.get("choices", []):
                msg = choice.get("message", {})
                reasoning = msg.get("reasoning_content")
                content = msg.get("content", "")
                if reasoning and content is not None:
                    self._cache[self._key(content)] = reasoning
        except Exception:  # noqa: BLE001, S110
            pass

    async def aclose(self) -> None:
        """关闭内部传输层。"""
        await self._inner.aclose()


def get_llm(llm_provider: str, model_name: str) -> Model:
    """根据提供方与模型名获取大语言模型实例。

    参数:
        llm_provider (str): 大语言模型提供方（例如 'openai'、'anthropic'、
            'openai_compatible'）。
        model_name (str): 要使用的模型名称。

    返回:
        Model: 大语言模型实例。
    """
    if llm_provider == "ollama":
        ollama_url = os.getenv("OLLAMA_URL")
        if not ollama_url:
            ollama_url = (
                f"http://{os.getenv('OLLAMA_HOST', 'localhost')}:"
                f"{os.getenv('OLLAMA_PORT', '11434')}/v1"
            )
        return OpenAIModel(
            model_name,
            provider=OpenAIProvider(
                base_url=ollama_url,
            ),
        )

    # 兼容任意 OpenAI 格式的第三方 API（通过 OPENAI_BASE_URL 指定接入地址）
    if llm_provider in ("openai_compatible", "custom") or (
        llm_provider == "openai" and os.getenv("OPENAI_BASE_URL")
    ):
        base_url = os.getenv("OPENAI_BASE_URL")
        api_key = os.getenv("OPENAI_API_KEY", "sk-placeholder")
        if not base_url:
            msg = (
                "使用 openai_compatible 提供方时，"
                "必须在环境变量中设置 OPENAI_BASE_URL。"
            )
            raise ValueError(msg)
        http_client = httpx.AsyncClient(transport=_ThinkingPassthroughTransport())
        return OpenAIModel(
            model_name,
            provider=OpenAIProvider(
                base_url=base_url,
                api_key=api_key,
                http_client=http_client,
            ),
        )

    if llm_provider == "azure_openai":
        llm_provider = "azure"

    if llm_provider == "google":
        llm_provider = "google-gla"

    # 使用 provider="azure" 时似乎会失败
    if llm_provider == "openai" and model_name.startswith("gpt-5"):
        model = OpenAIResponsesModel(
            model_name,
            provider=llm_provider,  # type: ignore[arg-type]
            settings=ModelSettings(
                extra_body={
                    "reasoning": {
                        "effort": "minimal",
                    },
                    "text": {
                        "verbosity": "low",
                    },
                }
            ),
        )
    else:
        model = infer_model(f"{llm_provider}:{model_name}")

    if model_name.startswith("gpt-5"):
        model.profile = model.profile.update(
            OpenAIModelProfile(openai_supports_sampling_settings=False)
        )

    return model


def get_prompt(
    template: str | None = None,
    instructions: str | None = None,
    prompt_style: str | None = None,
    name: str = "joinly",
) -> str:
    """获取智能体使用的提示模板。

    参数:
        template (str): 要使用的提示模板；默认为 DEFAULT_PROMPT_TEMPLATE。
        instructions (str): Instructions for the agent.
        If None, uses instructions based on prompt_style.
        prompt_style (str): 默认指令类型；默认为 "mpc"。
        name (str): 智能体名称；默认为 'joinly'。

    返回:
        str: 格式化后的提示模板。
    """
    template = template if template is not None else DEFAULT_PROMPT_TEMPLATE
    if instructions is None:
        instructions = (
            DYADIC_INSTRUCTIONS if prompt_style == "dyadic" else MPC_INSTRUCTIONS
        )
    today = datetime.now(tz=UTC).date().isoformat()
    return template.format(date=today, name=name, instructions=instructions)


class _Mapper(MCPServer):
    def __init__(self, client: ClientSession) -> None:
        self._client = client

    async def client_streams(self) -> Never:  # type: ignore[override]
        raise RuntimeError


def sanitize_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
    """清理工具 JSON schema。

    This function removes unsupported JSON schema features and ensures the schema
    is compatible with OpenAI's requirements.

    参数:
        schema (dict[str, Any]): 原始 JSON schema。

    返回:
        dict[str, Any]: 清理后的 JSON schema。
    """
    unsupported = {
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "$schema",
        "$id",
        "$ref",
        "definitions",
        "$defs",
        "patternProperties",
    }

    def default_object() -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": True}

    def choose_type(t: Any) -> str:  # noqa: ANN401
        if isinstance(t, list):
            return t[0] if t and isinstance(t[0], str) else "object"
        return t if isinstance(t, str) else "object"

    def walk(node: Any) -> dict[str, Any]:  # noqa: ANN401
        if not isinstance(node, dict):
            return default_object()
        out = {k: v for k, v in node.items() if k not in unsupported}
        t = choose_type(out.get("type", "object"))

        if t == "object":
            props = out.get("properties")
            props = props if isinstance(props, dict) else {}
            out["properties"] = {k: walk(v) for k, v in props.items()}
            ap = out.get("additionalProperties", True)
            out["additionalProperties"] = ap if isinstance(ap, bool) else True
            req = out.get("required")
            if isinstance(req, list):
                req = [k for k in req if isinstance(k, str) and k in out["properties"]]
                if req:
                    out["required"] = req
                else:
                    out.pop("required", None)
            out["type"] = "object"
            return out

        if t == "array":
            items = out.get("items")
            if isinstance(items, list):
                out["items"] = walk(items[0]) if items else default_object()
            elif isinstance(items, dict):
                out["items"] = walk(items)
            else:
                out["items"] = default_object()
            out["type"] = "array"
            return out

        out["type"] = t
        return out

    return walk(schema)


async def load_tools(  # noqa: C901
    clients: McpClientConfig | dict[str, McpClientConfig],
    sanitize_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = (
        sanitize_tool_schema
    ),
) -> tuple[list[ToolDefinition], ToolExecutor]:
    """从客户端加载工具定义。

    参数:
        clients: A dictionary of client configurations, where the key is the client name
            and the value is the client configuration.
        sanitize_fn: 用于清理工具 schema 的函数；默认为
            sanitize_tool_schema. No sanitization if None.

    返回:
        tuple[list[ToolDefinition], ToolExecutor]: 工具定义列表以及
            corresponding tool executor.
    """
    tools = []
    client_items = clients.items() if isinstance(clients, dict) else [(None, clients)]
    for prefix, config in client_items:
        for tool in await config.client.list_tools():
            if tool.name in config.exclude:
                continue
            if config.include and tool.name not in config.include:
                continue

            if sanitize_fn is None:
                schema = tool.inputSchema
            else:
                try:
                    schema = sanitize_fn(tool.inputSchema)
                except Exception:
                    logger.exception(
                        "Error sanitizing schema for tool %s of MCP %s, skipping",
                        tool.name,
                        prefix,
                    )
                    continue

            tools.append(
                ToolDefinition(
                    name=f"{prefix}_{tool.name}" if prefix is not None else tool.name,
                    description=tool.description,
                    parameters_json_schema=schema,
                )
            )

    async def _tool_executor(tool_name: str, args: dict[str, Any]) -> Any:  # noqa: ANN401
        """按名称与参数执行工具。"""
        if isinstance(clients, McpClientConfig):
            client = clients.client
            post_callback = clients.post_callback
        else:
            prefix, tool_name = tool_name.split("_", 1)
            if prefix not in clients:
                msg = f"MCP '{prefix}' not found"
                raise ValueError(msg)
            client = clients[prefix].client
            post_callback = clients[prefix].post_callback

        result = await client.call_tool_mcp(tool_name, args)
        if post_callback:
            result = await post_callback(tool_name, args, result)

        mapper = _Mapper(client.session)
        mapped = [await mapper._map_tool_result_part(p) for p in result.content]  # noqa: SLF001

        if result.isError:
            return f"[error] {'\n'.join(str(part) for part in mapped)}"

        return mapped[0] if len(mapped) == 1 else mapped

    return tools, _tool_executor


def normalize(s: str) -> str:
    """规范化字符串。

    参数:
        s: 待规范化的字符串。

    返回:
        规范化后的字符串。
    """
    normalized = unicodedata.normalize("NFKD", s.casefold().strip())
    chars = (c for c in normalized if unicodedata.category(c) != "Mn")
    return re.sub(r"[^\w\s]", "", "".join(chars))


def name_in_transcript(transcript: Transcript, name: str) -> bool:
    """检查名称是否出现在转写中。

    参数:
        transcript: 待检查的转写。
        name: 要查找的名称。

    返回:
        True if the name is mentioned in the transcript, False otherwise.
    """
    pattern = rf"\b{re.escape(normalize(name))}\b"
    return bool(re.search(pattern, normalize(transcript.text)))


def is_async_context() -> bool:
    """判断当前上下文是否为异步。

    返回:
        bool: True if the current context is asynchronous, False otherwise.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    else:
        return True
