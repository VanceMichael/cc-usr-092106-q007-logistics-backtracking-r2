# 跨省物流倒查协作

跨承运方的药品物流倒查后台（领域层），支持专案组从一次网络售药发货记录出发，
在合法调取范围内重放货物如何被**归集、拆分、跨省运输、外省再合并与签收**。

所有样例均为虚构数据（`fixtures/`），不含真实个人信息、账号或访问凭据。

## 核心原则

1. **只提候选，不认定同一**：面单寄件人、实际揽收网点、收药人网络昵称是三个独立
   主体引用。同手机号、同地址代号、同昵称文本只产生 `weak` 候选关联；同箱、同车次、
   同包裹跨单产生 `physical` 候选。候选恒带 `attribute_match_is_not_identity` 警示，
   系统不提供任何"认定同一主体/同案参与者"的接口。
2. **轨迹只追加，不覆盖**：承运方晚到更正、运单注销、转寄、拒收、部分签收都以更高
   版本号追加，旧版本永久保留，重放时全部出现在时间轴上。
3. **入库先过合法调取闸**：每条材料必须挂查询回执，回执须由有效授权支撑，且
   承运方 / 数据类别 / 列明运单都在授权范围内；授权撤销、过期、越范围一律拒绝入库。
4. **冻结不重复**：不同地区小组并行扩线时，同一批材料（按内容指纹）被他组冻结则
   拒绝；本组重复提交幂等；部分重叠只预警不阻断。跨进程用 `fcntl` 文件锁互斥。
5. **缺口显式化**：重放输出缺失的扫描节点、部分签收残件、无拆并箱记录的重量异常，
   并逐项标注缺口性质：`missing_authorization`（缺授权）、`missing_receipt`（缺回执）、
   `missing_authorization_and_receipt`、`awaiting_carrier_data`（手续齐、等承运方回传）。

## 模块

| 模块 | 职责 |
| --- | --- |
| `src/logistics/models.py` | 授权、回执、地址代号、运单版本、重量、扫描、箱盒事件、车辆批次、售药种子 |
| `src/logistics/legal.py` | 合法调取三闸校验与缺口法律状态判定 |
| `src/logistics/store.py` | 仅追加 JSONL 卷宗（跨进程文件锁、版本号约束、重放恢复） |
| `src/logistics/links.py` | 候选关联引擎（weak / physical，均不构成身份结论） |
| `src/logistics/freeze.py` | 跨小组材料冻结账本（SHA-256 内容指纹去重） |
| `src/logistics/replay.py` | 倒查重放：时间轴 + 缺口/残件/重量异常/授权分析 |
| `src/logistics/load.py` | 从 JSON 材料包装载卷宗 |

原有 `src/context.py`、`contracts/context.schema.json` 为领域背景资料，保持不变。

## 快速使用

```python
from src.logistics import Dossier, Replayer, LinkEngine, FreezeLedger
from src.logistics.load import load_bundle

d = Dossier("work/case.jsonl")
load_bundle("fixtures/logistics_case.json", d)

seed = d.seeds["sale-2026-001"]
result = Replayer(d).replay(seed)
print(result.narrative())          # 归集→拆分→跨省→再合并→签收/拒收/部分签收
for gap in result.missing_nodes:  # 缺节点及缺的是授权还是回执
    print(gap.subject, gap.node, gap.legal_status)

for link in LinkEngine(d).all_candidates():
    print(link.as_lead())          # 全部以"候选"口吻表述

with FreezeLedger("work/freeze.jsonl") as ledger:
    ledger.freeze(case_id="CASE-2026-031", team_id="team-dongjiang",
                  material_keys=["SSD-W001", "JYT-W002"])
```

## 本地校验

```bash
python -m unittest discover -s tests
```

测试（26 项）覆盖：端到端链条扩展与时间轴、晚到更正/注销/拒收/部分签收的版本
保留、append-only 与卷宗重启恢复、合法调取四类拒绝情形、弱候选不合并主体、
冻结去重/幂等/重叠预警/跨进程文件锁、残件与重量异常、缺口法律状态。

## 领域资料

`contracts/context.schema.json` 描述基础资料格式，`fixtures/context.json` 为背景样例；
`fixtures/logistics_case.json` 为完整虚构跨省案例（2 家承运方、4 份运单、3 个车次、
归集/拆箱/外省合箱、部分签收、拒收、注销、晚到更正）。
