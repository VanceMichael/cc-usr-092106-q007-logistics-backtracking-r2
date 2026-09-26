"""跨小组材料冻结账本。

场景：不同地区小组并行扩线时，**同一批材料不得被重复冻结**。

规则：

- 一批材料由其内容（排序后的材料标识集合）的 SHA-256 唯一定位，
  与提交顺序、批次命名无关；
- 同一内容已被他组冻结 → ``DuplicateFreeze``，拒绝并指明持有人；
- 本组重复提交完全相同的集合 → 幂等返回既有冻结记录；
- 部分重叠不阻断（不同批次本就可能共享个别材料），但返回 ``warnings``，
  列明与哪些既有冻结重叠，留痕可查；
- 账本仅追加，跨进程用 fcntl 互斥。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


def batch_fingerprint(material_keys: list[str] | tuple[str, ...] | set[str]) -> str:
    """一批材料的内容指纹：排序去重后取 SHA-256。"""
    normalized = sorted({k.strip() for k in material_keys if k.strip()})
    if not normalized:
        raise ValueError("冻结材料集合不能为空")
    payload = "\n".join(normalized).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class FreezeRecord:
    freeze_id: str
    case_id: str
    team_id: str
    fingerprint: str
    material_keys: tuple[str, ...]
    reason: str
    frozen_at: str


@dataclass(frozen=True)
class FreezeResult:
    record: FreezeRecord
    created: bool                       # False 表示命中本组幂等记录
    warnings: tuple[str, ...]          # 与他组批次的部分重叠提示


class DuplicateFreeze(Exception):
    """同一批材料已被其他小组冻结。"""

    def __init__(self, existing: FreezeRecord):
        self.existing = existing
        super().__init__(
            f"材料批次（{existing.fingerprint[:12]}…）已被小组 {existing.team_id} "
            f"于 {existing.frozen_at} 冻结（冻结号 {existing.freeze_id}），不得重复冻结"
        )


class FreezeLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._records: list[FreezeRecord] = []
        self._by_fingerprint: dict[str, FreezeRecord] = {}
        if self.path.exists():
            self._reload()

    def __enter__(self) -> "FreezeLedger":
        self._lock_fh = open(self._lock_path, "w")
        if fcntl is not None:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX)
        self._reload()  # 拿锁后重读，避免与并发写入错过
        return self

    def __exit__(self, *exc) -> None:
        if fcntl is not None:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
        self._lock_fh.close()

    def _reload(self) -> None:
        self._records = []
        self._by_fingerprint = {}
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            rec = FreezeRecord(
                freeze_id=d["freeze_id"],
                case_id=d["case_id"],
                team_id=d["team_id"],
                fingerprint=d["fingerprint"],
                material_keys=tuple(d["material_keys"]),
                reason=d.get("reason", ""),
                frozen_at=d["frozen_at"],
            )
            self._records.append(rec)
            self._by_fingerprint.setdefault(rec.fingerprint, rec)

    def freeze(
        self,
        *,
        case_id: str,
        team_id: str,
        material_keys: list[str] | tuple[str, ...] | set[str],
        reason: str = "",
        frozen_at: str | None = None,
    ) -> FreezeResult:
        """登记一次冻结；调用方应在 ``with ledger`` 内使用以保证跨进程互斥。"""
        keys = tuple(sorted({k.strip() for k in material_keys if k.strip()}))
        fp = batch_fingerprint(keys)

        existing = self._by_fingerprint.get(fp)
        if existing is not None:
            if existing.team_id == team_id:
                return FreezeResult(record=existing, created=False, warnings=())
            raise DuplicateFreeze(existing)

        warnings = self._overlap_warnings(team_id, set(keys))
        record = FreezeRecord(
            freeze_id=f"FZ-{len(self._records) + 1:05d}",
            case_id=case_id,
            team_id=team_id,
            fingerprint=fp,
            material_keys=keys,
            reason=reason,
            frozen_at=frozen_at or datetime.now(timezone.utc).isoformat(),
        )
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.__dict__, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._records.append(record)
        self._by_fingerprint[fp] = record
        return FreezeResult(record=record, created=True, warnings=tuple(warnings))

    def _overlap_warnings(self, team_id: str, new_keys: set[str]) -> list[str]:
        warnings: list[str] = []
        for rec in self._records:
            if rec.team_id == team_id:
                continue
            overlap = new_keys & set(rec.material_keys)
            if overlap:
                warnings.append(
                    f"与 {rec.team_id} 的冻结 {rec.freeze_id} 部分重叠 "
                    f"{len(overlap)} 项（如 {sorted(overlap)[0]}）；"
                    "非整批重复，已留痕，建议线下协调避免重复取证"
                )
        return warnings

    # ------------------------------------------------------------- 查询

    def records_of(self, team_id: str) -> list[FreezeRecord]:
        return [r for r in self._records if r.team_id == team_id]

    def all_records(self) -> list[FreezeRecord]:
        return list(self._records)
