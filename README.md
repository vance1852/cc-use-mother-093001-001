# 白塔杯参赛资格与投稿治理服务

在文化创意赛事基础能力（机构、操作者、场所、参考资料、角色权限、请求幂等、SQLite 事务、哈希串联审计）之上，
为「白塔杯」提供一套完整的**参赛资格与投稿治理**服务。它把自然人、组织、监护/授权代理、团队成员、
作品版本、赛道名额和报名窗口纳入同一套可追溯规则。

## 解决的问题

- 同一创作团队用**个人、工作室、代理人**身份分别投递，无法识别重复占位；
- 未成年作者由**监护人代签**，代理关系、授权范围与有效期说不清；
- 临近截止把作品从「西城纹韵」**换赛道**到「西城好物」，资格与名额如何迁移；
- 说不清**截止前后**的修改是否有效；
- 迟到材料可能**偷偷改写**原申请；
- 重复点击 / 网络重试产生第二份报名；
- 服务重启后审核进度丢失；
- 无法回答「这件作品在某一天为什么有效 / 被拒 / 等待补正」。

## 核心不变量

1. **受益创作主体唯一**：一切身份（个人、工作室、团队、被代理的未成年人）都归一到一个自然人
   `beneficiary_person_id`。工作室绑定受益控制人；团队必须显式指定受益成员；监护/授权代理的
   被代理人即为受益人。
2. **互斥资格**：同一受益主体在同一窗口、同一互斥组内只能持有一个生效名额
   （部分唯一索引 `uq_qualification_active` 强约束，应用层再按互斥组判定）。换赛道在单事务内
   「释放旧占位→获取新占位」，失败整体回滚。重复占位尝试会被拒绝，且**冲突依据独立留痕**
   （业务数据回滚，`conflict_records` 与审计事件保留）。
3. **不可覆盖记录**：提交、补正、撤回、换赛道、成员变更、审核、冻结、申诉全部 append-only 写入
   `entry_events` 事件账本与 `submission_versions` 版本链，同时进入全局哈希审计链
   （`audit_events`，可离线逐条验链）。任何 UPDATE 都不删除历史。
4. **窗口与原子冻结**：开放时间前、截止时间后拒绝写入；`freeze` 在单事务内把每件未撤回作品当时的
   生效版本、赛道、状态与团队名册快照到 `freeze_snapshots`。冻结后补正/撤回/换赛道一律拒绝。
5. **迟到材料只能申诉**：截止后的新材料只能进入 `appeals`，绝不产生新版本、不改写冻结快照；
   申诉成立后审核任务以 `appeal_id` 标记重新排队复核，来源可追溯。
6. **幂等**：所有写接口以 `request_id` 去重，重复请求回放原始回执（优先于窗口/状态校验，因此
   截止后重试原请求也安全），不会产生第二份报名。
7. **可续审**：审核任务、队列、决定都持久化在 SQLite，服务重启后审核员继续处理未决任务。
8. **时间旅行**：`explain?at=` 重放事件账本，解释作品在任意历史时点为何
   `pending / awaiting_correction / approved / rejected / withdrawn`，并给出生效版本、冻结版本、
   资格占位与冲突依据。

## 角色与可见材料

| 角色 | 身份头 | 能力 |
| --- | --- | --- |
| 管理员 / 操作员 | `X-Actor-Id` | 配置赛道、窗口、主体、代理关系，冻结窗口 |
| 资格审核员 | `X-Actor-Id` | 看待审队列、材料全文、冲突依据，做通过/拒绝/补正决定，处理申诉 |
| 审计员 | `X-Actor-Id` | 只读：作品、事件、版本/材料**哈希**、申诉进度、审计链；不可见材料全文、不可写 |
| 参赛人 | `X-Participant-Token` | 建团队、投稿、补正、撤回、换赛道、成员变更、申诉；只能看与自己相关的作品 |

参赛令牌为 `ptk_...`，仅在创建时返回明文，库内只存其 SHA-256 哈希。

## 主要 HTTP 接口

员工接口（`X-Actor-Id`）：

- `POST /gov/tracks`、`POST /gov/windows`、`POST /gov/windows/freeze`
- `POST /gov/persons`、`POST /gov/organizations`
- `POST /gov/representations`、`POST /gov/representations/revoke`
- `POST /gov/participant-tokens`
- `GET  /gov/review-queue`、`POST /gov/reviews`
- `GET  /gov/submissions`、`GET /gov/conflicts`、`GET /gov/occupancy?window_id=`
- `GET  /gov/appeals`、`POST /gov/appeals/decide`

参赛人接口（`X-Participant-Token`）：

- `POST /teams`、`POST /teams/beneficiary`、`POST /teams/members`
- `POST /submissions`、`POST /submissions/correct`、`POST /submissions/withdraw`、
  `POST /submissions/switch-track`
- `POST /appeals`、`GET /me/submissions`

时间旅行（两种身份均可，各自只能访问有权查看的作品）：

- `GET /submissions/{id}/timeline`
- `GET /submissions/{id}/explain?at=2026-10-05T12:00:00Z`

所有写请求体都需要 `request_id`；成功返回 201，幂等回放返回 200 且 `replayed=true`。

## 目录

- `src/creative_program_foundation/`
  - `models.py` / `errors.py` / `clock.py` / `domain.py`：数据对象、业务异常、可替换时钟、资料类别
  - `storage.py`：SQLite 连接、建表、短事务（含资格部分唯一索引与全部治理表）
  - `audit.py`：哈希串联审计日志与验链
  - `service.py`：基础登记服务（机构/操作者/场所/资料）
  - `governance.py`：资格、投稿、版本、名额、窗口、冻结、审核、申诉、时间旅行
  - `api.py`：标准库 HTTP/JSON 边界与角色分派
  - `acceptance.py`：基础登记链 + 白塔杯治理全流程离线验收
- `tests/`：存储、服务、治理规则、HTTP 角色分离、离线验收测试

## 环境

- Linux，Python 3.11+
- 运行时仅使用 Python 标准库与 SQLite

## 测试 / 构建 / 验收

    PYTHONPATH=src python3 -m unittest discover -s tests -v
    python3 -m compileall -q src tests
    PYTHONPATH=src python3 -m creative_program_foundation.acceptance

验收通过时输出一行 `status` 为 `ok` 的 JSON（含 `duplicate_blocked`、`conflicts`、
`past_status`、`final_status`、`frozen_version`、`appeal_preserved_original` 等断言字段），
退出码 0。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database baitabei.sqlite3 --host 127.0.0.1 --port 8080

健康检查 `GET /health` 同时返回审计链校验结果。服务重启后，SQLite 中的主体、作品、版本、
资格占位、审核任务、申诉与审计历史继续保留。
