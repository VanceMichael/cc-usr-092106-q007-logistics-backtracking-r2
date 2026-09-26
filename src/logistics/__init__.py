"""跨承运方物流倒查领域模型。

本包只做资料读取、校验与分析，不连接任何外部系统。
所有标识（``id``）由案卷资料给定，业务服务沿用相同标识与版本约定：

- 运单字段一经录入只能追加 ``correction`` 事件，原值永久保留；
- 同手机号/同地址只产生“候选关联”，系统不提供任何把两个主体合并为一人的接口。
"""

from .casefile import CaseFile, load_casefile
from .events import (
    EVENT_TYPES,
    LIFECYCLE_TERMINAL,
    REGISTERED_FIELDS,
    SCAN_KINDS,
    field_versions,
    latest_fields,
    ordered_events,
    waybill_status,
)
from .associations import CandidateBook, LinkDecision, build_candidate_book
from .freezes import FreezeConflict, FreezeRegistry
from .replay import Gap, GapKind, ReplayResult, replay_sale

__all__ = [
    "CaseFile",
    "load_casefile",
    "EVENT_TYPES",
    "LIFECYCLE_TERMINAL",
    "REGISTERED_FIELDS",
    "SCAN_KINDS",
    "field_versions",
    "latest_fields",
    "ordered_events",
    "waybill_status",
    "CandidateBook",
    "LinkDecision",
    "build_candidate_book",
    "FreezeConflict",
    "FreezeRegistry",
    "Gap",
    "GapKind",
    "ReplayResult",
    "replay_sale",
]
