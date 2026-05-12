import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Literal, Self

from pydantic_ai import BinaryContent
from pydantic_ai.direct import model_request
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings, merge_model_settings
from pydantic_ai.tools import ToolDefinition

from joinly_client.types import ToolExecutor, TranscriptSegment, Usage
from joinly_client.utils import get_prompt

logger = logging.getLogger(__name__)

AgentStatus = Literal["llm_call", "tool_call"]


class ConversationalToolAgent:
    """与 joinly 交互的对话式智能体实现。"""

    def __init__(  # noqa: PLR0913
        self,
        llm: Model,
        tools: list[ToolDefinition],
        tool_executor: ToolExecutor,
        *,
        prompt: str | None = None,
        max_messages: int = 50,
        max_tool_result_chars: int = 2048,
        max_ephemeral_tool_result_chars: int = 16384,
        max_agent_iter: int | None = 15,
        on_status: Callable[[AgentStatus | None], Awaitable[None]] | None = None,
    ) -> None:
        """使用给定模型初始化对话式智能体。

        参数:
            llm (Model): 智能体使用的大语言模型。
            tools (list[ToolDefinition] | None): 工具定义列表；默认为 None。
            tool_executor (ToolExecutor | None): 执行工具的函数；默认为 None。
            prompt (str | None): 初始化智能体时可选的系统提示词。
            max_messages (int): 保留在智能体历史中的最大消息条数；默认 50。
            max_tool_result_chars (int): 每轮结束后工具返回结果的最大字符数
                （超出则截断）；默认 2048。
            max_ephemeral_tool_result_chars (int): 单次工具调用后立即截断时的
                最大字符数；默认 16384。
            max_agent_iter (int | None): 智能体最大迭代轮数；默认 15。
            on_status: 可选的状态变更回调，在智能体状态变化时调用。
        """
        if not tools:
            msg = "At least one tool must be provided to the agent."
            raise ValueError(msg)

        self._llm = llm
        self._prompt = prompt or get_prompt()
        self._tools = tools
        self._tool_executor = tool_executor
        self._on_status = on_status
        self._messages: list[ModelMessage] = []
        self._max_messages = max_messages
        self._max_tool_result_chars = max_tool_result_chars
        self._max_ephemeral_tool_result_chars = max_ephemeral_tool_result_chars
        self._max_agent_iter = max_agent_iter
        self._usage = Usage()
        self._run_task: asyncio.Task | None = None

    @property
    def usage(self) -> Usage:
        """获取智能体的用量统计。"""
        return self._usage

    async def __aenter__(self) -> Self:
        """进入智能体异步上下文。"""
        self._messages = []
        self._usage = Usage()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """退出智能体异步上下文并清理资源。"""
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
        self._run_task = None

    async def on_utterance(self, segments: list[TranscriptSegment]) -> None:
        """处理一句完整话语（utterance）事件。

        参数:
            segments (list[TranscriptSegment]): 待处理的转写片段列表。
        """
        if self._run_task and not self._run_task.done():
            logger.debug("Cancelling current agent run task")
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
        self._run_task = asyncio.create_task(self._run_loop(segments))

    async def _set_status(self, status: AgentStatus | None) -> None:
        """若已注册回调，则通知状态变更。"""
        if self._on_status:
            await self._on_status(status)

    async def _run_loop(self, segments: list[TranscriptSegment]) -> None:
        """根据给定片段运行智能体主循环。

        参数:
            segments (list[TranscriptSegment]): 待处理的转写片段列表。
        """
        self._messages.append(
            ModelRequest(
                parts=[
                    UserPromptPart(
                        f"{segment.speaker or 'Participant'}: {segment.text}"
                    )
                    for segment in segments
                ]
            )
        )

        iteration: int = 0
        self._messages = self._truncate_tool_results(
            self._messages, max_chars=self._max_tool_result_chars
        )
        self._messages = self._omit_binary_tool_results(self._messages)
        try:
            while self._max_agent_iter is None or iteration < self._max_agent_iter:
                self._messages = self._limit_messages(
                    self._messages, max_messages=self._max_messages
                )
                self._messages = self._truncate_tool_results(
                    self._messages, max_chars=self._max_ephemeral_tool_result_chars
                )

                await self._set_status("llm_call")
                response = await self._call_llm(self._messages)
                await self._set_status(None)
                request = await self._call_tools(response)
                self._messages.append(response)
                if request:
                    self._messages.append(request)
                if self._check_end_turn(response, request):
                    break
                iteration += 1
        finally:
            await self._set_status(None)

    async def _call_llm(self, messages: list[ModelMessage]) -> ModelResponse:
        """使用当前消息列表调用大语言模型。

        参数:
            messages (list[ModelMessage]): 发送给大语言模型的消息列表。

        返回:
            ModelResponse: 大语言模型的响应。
        """
        logger.debug("Calling LLM with %d messages", len(messages))
        response = await model_request(
            self._llm,
            [ModelRequest(parts=[SystemPromptPart(self._prompt)]), *messages],
            model_settings=merge_model_settings(
                self._llm.settings,
                ModelSettings(
                    temperature=0.2,
                    parallel_tool_calls=True,
                ),
            ),
            model_request_parameters=ModelRequestParameters(
                function_tools=[
                    ToolDefinition(
                        name="end_turn",
                        description=(
                            "End the current response turn. "
                            "Use this directly if no or no further response is needed."
                        ),
                        parameters_json_schema={"properties": {}, "type": "object"},
                    ),
                    *self._tools,
                ],
                # gpt-5 不要将 tool calls 设为 required，否则易出问题
                allow_text_output=self._llm.model_name.startswith("gpt-5"),
            ),
        )
        logger.debug(
            "LLM response received with %d parts, %d input tokens and %d output tokens",
            len(response.parts),
            response.usage.request_tokens or 0,
            response.usage.response_tokens or 0,
        )
        self._usage.add(
            "llm",
            usage={
                "input_tokens": response.usage.request_tokens or 0,
                "output_tokens": response.usage.response_tokens or 0,
            },
            meta={"model": self._llm.model_name, "provider": self._llm.system},
        )
        return response

    async def _call_tools(self, response: ModelResponse) -> ModelRequest | None:
        """处理大语言模型响应并执行其中的工具调用。

        参数:
            response (ModelResponse): 包含工具调用的模型响应。

        返回:
            ModelRequest | None: 包含各工具调用结果（可附加二进制工件）的请求；
                若无工具调用则为 None。
        """
        tool_calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
        if not tool_calls:
            return None

        signal = any(t.tool_name != "end_turn" for t in tool_calls)
        if signal:
            await self._set_status("tool_call")
        try:
            results = await asyncio.gather(*[self._call_tool(t) for t in tool_calls])
        finally:
            if signal:
                await self._set_status(None)

        parts: list[ModelRequestPart] = [tool_return for tool_return, _ in results]
        parts.extend(user_part for _, user_part in results if user_part)

        return ModelRequest(parts=parts)

    async def _call_tool(
        self, tool_call: ToolCallPart
    ) -> tuple[ToolReturnPart, UserPromptPart | None]:
        """根据工具调用部件执行指定工具。

        参数:
            tool_call (ToolCallPart): 包含工具名与参数的工具调用部件。

        返回:
            tuple[ToolReturnPart, UserPromptPart | None]: 工具返回部件，以及用于承载
                二进制内容的可选用户提示部件。
        """
        if tool_call.tool_name == "end_turn":
            return (
                ToolReturnPart(
                    tool_name="end_turn",
                    content="",
                    tool_call_id=tool_call.tool_call_id,
                ),
                None,
            )

        logger.info(
            "%s: %s",
            tool_call.tool_name,
            ", ".join(
                f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}"
                for k, v in tool_call.args_as_dict().items()
            ),
        )

        try:
            content = await self._tool_executor(
                tool_call.tool_name, tool_call.args_as_dict()
            )
        except Exception:
            logger.exception("Error calling tool %s", tool_call.tool_name)
            content = f"Error calling tool {tool_call.tool_name}"

        logger.info(
            "%s: %s",
            tool_call.tool_name,
            content
            if not isinstance(content, BinaryContent)
            else (
                f"BinaryContent(media_type='{content.media_type}', "
                f"data_bytes={len(content.data)})"
            ),
        )

        artifacts = []

        def process_item(item: Any, idx: int | None = None) -> str | float:  # noqa: ANN401
            if isinstance(item, BinaryContent):
                suffix = "" if idx is None else f"_{idx}"
                identifier = f"artifact_{tool_call.tool_call_id}{suffix}"
                artifacts.extend([f"This is {identifier}:", item])
                return f"See {identifier}"
            return item

        if isinstance(content, list):
            tool_content = [process_item(item, i) for i, item in enumerate(content)]
        else:
            tool_content = process_item(content)

        user_part = (
            UserPromptPart(content=artifacts, part_kind="user-prompt")
            if artifacts
            else None
        )

        return (
            ToolReturnPart(
                tool_name=tool_call.tool_name,
                content=tool_content,
                tool_call_id=tool_call.tool_call_id,
            ),
            user_part,
        )

    def _check_end_turn(
        self, response: ModelResponse, request: ModelRequest | None
    ) -> bool:
        """判断本轮智能体是否已结束。

        若调用了 `end_turn` 工具、无任何工具调用，或工具返回表明发生语音打断，
        则视为本轮结束。

        参数:
            response (ModelResponse): 大语言模型的响应。
            request (ModelRequest): 发往大语言模型的请求（含工具返回等）。

        返回:
            bool: 本轮已结束为 True，否则为 False。
        """
        tool_calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
        tool_responses = (
            [p for p in request.parts if isinstance(p, ToolReturnPart)]
            if request
            else []
        )

        end_turn_tool_called = any(p.tool_name == "end_turn" for p in tool_calls)
        interrupted = any(
            "Interrupted by detected speech" in str(p.content)
            and p.tool_name.endswith("speak_text")
            for p in tool_responses
        )
        left_meeting = any(
            str(p.content) == "Left the meeting."
            and p.tool_name.endswith("leave_meeting")
            for p in tool_responses
        )

        finished = not tool_calls or end_turn_tool_called or interrupted or left_meeting
        if finished:
            logger.debug(
                "Agent turn ended: %s",
                "No tool calls"
                if not tool_calls
                else "End turn tool called"
                if end_turn_tool_called
                else "Interrupted by speech"
                if interrupted
                else "Left meeting"
                if left_meeting
                else "Unknown",
            )

        return finished

    def _truncate_tool_results(
        self, messages: list[ModelMessage], max_chars: int
    ) -> list[ModelMessage]:
        """截断消息中过大的工具返回文本。

        参数:
            messages (list[ModelMessage]): 待处理的消息列表。
            max_chars (int): 工具返回允许的最大字符数。

        返回:
            list[ModelMessage]: 截断工具结果后的消息列表。
        """

        def _truncate(obj: object) -> str | object:
            string = (
                obj
                if isinstance(obj, str)
                else (
                    json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                    if isinstance(obj, (dict, list, tuple))
                    else str(obj)
                )
            )
            if len(string) <= max_chars:
                return obj

            truncated = string[: max_chars - 26]
            logger.debug(
                "Truncated %d chars from tool result",
                len(string) - len(truncated),
            )
            return f"{truncated} [truncated {len(string) - len(truncated)} chars]"

        out: list[ModelMessage] = []
        for message in messages:
            if isinstance(message, ModelResponse):
                out.append(message)
                continue

            parts = []
            for p in message.parts:
                if isinstance(p, ToolReturnPart):
                    parts.append(replace(p, content=_truncate(p.content)))
                else:
                    parts.append(p)

            out.append(ModelRequest(parts=parts))

        return out

    def _omit_binary_tool_results(
        self, messages: list[ModelMessage]
    ) -> list[ModelMessage]:
        """从消息中省略二进制形式的工具结果。

        参数:
            messages (list[ModelMessage]): 待处理的消息列表。

        返回:
            list[ModelMessage]: 已替换二进制工具结果占位说明后的消息列表。
        """
        out: list[ModelMessage] = []
        for message in messages:
            if isinstance(message, ModelResponse):
                out.append(message)
                continue

            parts = []
            for p in message.parts:
                if isinstance(p, UserPromptPart) and isinstance(
                    p.content, (list, tuple)
                ):
                    parts.append(
                        replace(
                            p,
                            content=[
                                (
                                    f"[omitted {it.media_type}, {len(it.data)} bytes]"
                                    if isinstance(it, BinaryContent)
                                    else it
                                )
                                for it in p.content
                            ],
                        )
                    )
                else:
                    parts.append(p)

            out.append(ModelRequest(parts=parts))

        return out

    def _limit_messages(
        self, messages: list[ModelMessage], max_messages: int
    ) -> list[ModelMessage]:
        """限制智能体保留的消息条数。

        超过上限时删除最旧的消息；不会在单独的 ToolReturn 处截断，以保证工具调用
        与返回成对保留。

        参数:
            messages (list[ModelMessage]): 待限制的消息列表。
            max_messages (int): 历史中保留的最大消息条数。

        返回:
            list[ModelMessage]: 限制条数后的消息列表。
        """
        n = len(messages)
        if n > max_messages:
            start = n - max_messages
            while start > 0 and any(
                isinstance(p, ToolReturnPart) for p in messages[start].parts
            ):
                start -= 1
            if start > 0:
                logger.debug(
                    "Limited messages by removing %d",
                    start,
                )
                return messages[start:]
        return messages[:]
