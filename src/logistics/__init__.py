"""跨承运方物流倒查后台（领域层）。

模块划分：

- models:   不可变事件/版本模型（运单版本、扫描、重量、箱盒、批次、授权、回执……）
- legal:    合法调取范围校验
- store:    仅追加卷宗存储（JSONL + 文件锁）
- links:    候选关联（只给候选，不做主体同一认定）
- freeze:   跨小组材料冻结账本（同一批材料不得重复冻结）
- replay:   从售药种子重放归集/拆分/跨省/合并/签收并列出缺口
- load:     从 JSON 样例装载卷宗
"""

from .models import (
    Authorization,
    BoxEvent,
    QueryReceipt,
    SaleSeed,
    ScanEvent,
    VehicleBatch,
    WaybillVersion,
    WeightObservation,
)
from .store import Dossier, LegalAccessError
from .links import CandidateLink, LinkEngine
from .freeze import FreezeLedger, DuplicateFreeze, FreezeResult
from .replay import Replayer, ReplayResult

__all__ = [
    "Authorization",
    "BoxEvent",
    "QueryReceipt",
    "SaleSeed",
    "ScanEvent",
    "VehicleBatch",
    "WaybillVersion",
    "WeightObservation",
    "Dossier",
    "LegalAccessError",
    "CandidateLink",
    "LinkEngine",
    "FreezeLedger",
    "DuplicateFreeze",
    "FreezeResult",
    "Replayer",
    "ReplayResult",
]
