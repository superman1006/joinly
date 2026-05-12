from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    RootModel,
    computed_field,
    field_validator,
)


class SpeakerRole(str, Enum):
    """会议中说话人角色的枚举。

    属性:
        participant (str): 表示会议中的（普通）参与者。
        assistant (str): 表示会议中的本助手。
    """

    participant = "participant"
    assistant = "assistant"


class TranscriptSegment(BaseModel):
    """表示转写中的一个片段。

    属性:
        text (str): 片段文本。
        start (float): 片段开始时间（秒）。
        end (float): 片段结束时间（秒）。
        speaker (str | None): 说话人，若可得。
        role (SpeakerRole): 该片段中说话人的角色。
    """

    text: str
    start: float
    end: float
    speaker: str | None = None
    role: SpeakerRole = Field(default=SpeakerRole.participant)

    model_config = ConfigDict(frozen=True)

    @field_validator("start", "end", mode="after")
    @classmethod
    def _round(cls, v: float) -> float:
        """将开始与结束时间四舍五入到小数点后 3 位。"""
        return float(Decimal(str(v)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


class Transcript(BaseModel):
    """表示一份转写。"""

    _segments: set[TranscriptSegment] = PrivateAttr(default_factory=set)

    def add_segment(self, segment: TranscriptSegment) -> None:
        """向转写中添加一个片段。

        参数:
            segment (TranscriptSegment): 要添加的片段。
        """
        self._segments.add(segment)

    def __init__(
        self,
        *,
        segments: Iterable[TranscriptSegment | dict] | None = None,
        **data,  # noqa: ANN003
    ) -> None:
        """使用可选的片段列表初始化转写。

        参数:
            segments: 可迭代的 TranscriptSegment 或可转为 TranscriptSegment 的字典。
            **data: 传给父类的其他数据。
        """
        super().__init__(**data)
        if segments:
            for s in segments:
                segment = (
                    s
                    if isinstance(s, TranscriptSegment)
                    else TranscriptSegment.model_validate(s)
                )
                self._segments.add(segment)

    @computed_field
    @property
    def segments(self) -> list[TranscriptSegment]:
        """按开始时间排序后的转写片段列表。

        返回:
            list[TranscriptSegment]: 排序后的 TranscriptSegment 列表。
        """
        return sorted(self._segments, key=lambda s: s.start)

    @property
    def text(self) -> str:
        """返回转写的完整文本。

        返回:
            str: 所有片段文本拼接后的字符串。
        """
        return " ".join([segment.text for segment in self.segments])

    @property
    def speakers(self) -> set[str]:
        """返回转写中不重复的说话人集合。

        返回:
            set[str]: 不重复的说话人标识集合。
        """
        return {
            segment.speaker for segment in self.segments if segment.speaker is not None
        }

    def after(self, seconds: float) -> "Transcript":
        """返回仅包含给定秒数之后片段的转写副本。"""
        filtered = [s for s in self.segments if s.start > seconds]
        return Transcript(segments=filtered)

    def before(self, seconds: float) -> "Transcript":
        """返回仅包含给定秒数之前片段的转写副本。"""
        filtered = [s for s in self.segments if s.end < seconds]
        return Transcript(segments=filtered)

    def with_role(self, role: SpeakerRole) -> "Transcript":
        """返回仅包含指定角色片段的转写副本。"""
        filtered = [s for s in self.segments if s.role == role]
        return Transcript(segments=filtered)

    def compact(self, max_gap: float = 0.5) -> "Transcript":
        """返回合并相邻片段后的转写副本。

        同一说话人且角色相同、且间隔不超过 max_gap 的片段会合并为一条。

        参数:
            max_gap (float): 可合并片段之间的最大间隔（秒）。

        返回:
            Transcript: 合并后的新 Transcript 对象。
        """
        compacted: list[TranscriptSegment] = []

        for segment in self.segments:
            if (
                compacted
                and compacted[-1].speaker == segment.speaker
                and compacted[-1].role == segment.role
                and segment.start - compacted[-1].end <= max_gap
            ):
                last_segment = compacted[-1]
                compacted[-1] = TranscriptSegment(
                    text=last_segment.text + " " + segment.text,
                    start=last_segment.start,
                    end=segment.end,
                    speaker=last_segment.speaker,
                    role=last_segment.role,
                )
            else:
                compacted.append(segment)

        return Transcript(segments=compacted)


class VideoSnapshot(BaseModel):
    """会议视频画面的一帧快照。

    属性:
        data (bytes): 原始图像数据。
        media_type (Literal["image/jpeg", "image/png"]): 图像的媒体类型。
    """

    data: bytes
    media_type: Literal["image/jpeg", "image/png"] = "image/jpeg"


class MeetingChatMessage(BaseModel):
    """表示会议中的一条聊天消息。

    属性:
        text (str): 消息内容。
        timestamp (str | None): 发送时间戳。
        sender (str | None): 发送者，若可得。
    """

    text: str
    timestamp: str | None = None
    sender: str | None = None

    model_config = ConfigDict(frozen=True)


class MeetingChatHistory(BaseModel):
    """表示会议的聊天历史。"""

    messages: list[MeetingChatMessage] = Field(default_factory=list)


class MeetingParticipant(BaseModel):
    """表示会议中的一名参与者。

    属性:
        name (str): 参与者名称。
        email (str | None): 电子邮箱。
        infos (list[str]): 关于参与者的其他信息。
    """

    name: str
    email: str | None = None
    infos: list[str] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True)


class MeetingParticipantList(RootModel):
    """表示会议参与者列表。

    属性:
        root (list[MeetingParticipant]): MeetingParticipant 对象列表。
    """

    root: list[MeetingParticipant] = Field(default_factory=list)


class ServiceUsage(BaseModel):
    """保存某项服务用量统计的数据模型。"""

    usage: dict[str, int | float]
    meta: dict[str, str | int | float] = Field(default_factory=dict)

    def add(self, usage: "ServiceUsage") -> None:
        """将另一份 ServiceUsage 的统计累加到当前对象。

        参数:
            usage: 包含要累加统计的另一 ServiceUsage 实例。
        """
        for key, value in usage.usage.items():
            self.usage[key] = self.usage.get(key, 0) + value
        for key, value in usage.meta.items():
            self.meta[key] = value

    def __str__(self) -> str:
        """返回 ServiceUsage 的字符串表示。"""
        usage_str = ", ".join(
            f"{(v if isinstance(v, int) else f'{v:.4f}')} {k.replace('_', ' ')}"
            for k, v in self.usage.items()
        )
        meta_str = ", ".join(f"{k}={v}" for k, v in self.meta.items())
        return f"{usage_str} [{meta_str}]"


class Usage(RootModel):
    """保存整体用量统计的数据模型。"""

    root: dict[str, ServiceUsage] = Field(default_factory=dict)

    def add(
        self,
        service: str,
        usage: ServiceUsage | dict[str, int | float],
        meta: dict[str, str | int | float] | None = None,
    ) -> None:
        """为指定服务添加用量统计。

        参数:
            service: 服务名称。
            usage: ServiceUsage 实例或用量字典。
            meta: 与用量关联的可选元数据。
        """
        service_usage = (
            ServiceUsage(usage=usage, meta=meta or {})
            if isinstance(usage, dict)
            else usage
        )
        if service not in self.root:
            self.root[service] = service_usage
        else:
            self.root[service].add(service_usage)

    def merge(self, other: "Usage") -> "Usage":
        """与另一份 Usage 合并，返回新副本。

        参数:
            other: 要合并的另一 Usage 实例。

        返回:
            Usage: 合并统计后的新 Usage 实例。
        """
        merged = Usage()
        for service, usage in self.root.items():
            merged.add(service, usage)
        for service, usage in other.root.items():
            merged.add(service, usage)
        return merged

    def __str__(self) -> str:
        """返回 Usage 的字符串表示。"""
        return "\n".join(f"{service}: {usage}" for service, usage in self.root.items())


UITarget = Literal["overlay", "camera"]


UIAnimation = Literal["thinking", "busy"]


class UIAnimationContent(BaseModel):
    """预定义的动画内容。设为 None 表示停止动画。"""

    type: Literal["animation"] = "animation"
    animation: UIAnimation | None = None
    target: Literal["overlay"] = "overlay"


class UICsp(BaseModel):
    """HTML 内容的 CSP 限制（与 MCP Apps 规范对齐）。"""

    connect_domains: list[str] = Field(default_factory=list, alias="connectDomains")
    resource_domains: list[str] = Field(default_factory=list, alias="resourceDomains")
    frame_domains: list[str] = Field(default_factory=list, alias="frameDomains")

    model_config = ConfigDict(populate_by_name=True)


class UIHtmlContent(BaseModel):
    """自定义 HTML 内容。设为 None 表示清除内容。"""

    type: Literal["html"] = "html"
    html: str | None = None
    target: UITarget = "overlay"
    csp: UICsp | None = None


UIContent = Annotated[
    UIAnimationContent | UIHtmlContent,
    Field(discriminator="type"),
]


class UIUpdate(BaseModel):
    """UI 更新通知。"""

    content: UIContent
