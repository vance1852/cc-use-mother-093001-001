# 锁定跨赛道参赛资格协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

## 参赛资格与投稿治理模块

`creative_program_foundation.eligibility_*` 面向赛事报名治理，把以下对象纳入同一套可追溯规则：

- **主体档案**：自然人、外部机构（工作室）、监护关系、授权代理关系（含有效期）、受益创作主体集群（支持事后并案）、创作团队与成员历史；
- **赛道与窗口**：赛道名额与互斥组、报名窗口开放区间、截止后原子冻结；
- **投稿生命周期**：提交、补正（版本只增不改）、撤回、换赛道、成员变更、评审结论、申诉，全部写入只追加的 `eg_submission_events`；
- **互斥占位**：同一受益创作主体在同一窗口的同一互斥赛道组内至多保留一份未撤回投稿，个人、工作室、代理人等关联身份不能重复占位；冲突件随接口返回依据；
- **冻结与迟到材料**：截止时逐件锁定生效版本；冻结后原申请不可改写，迟到材料只能进入申诉，采纳申诉只记录独立的申诉生效版本号；
- **历史时点解释**：`GET /eg/submissions/<id>/explain?at=<ISO>` 按事件时间线回放任意时点的状态与理由（有效/被拒/等待补正/在审/不存在），并给出当时的互斥依据；
- **角色可见性**：工作人员（admin/operator/reviewer/auditor）走内部账号，参赛者使用 `person:<id>` / `org:<id>` 令牌；参赛者只能看到与自己相关的材料，审核员看到评审队列与冲突依据，审计员可只读全部材料；
- **幂等与恢复**：所有写操作要求 `request_id`，重放返回原始回执且不产生第二份报名；评审任务持久化，服务重启后未完成审核继续可见。

## 目录

- src/creative_program_foundation/：基础服务（领域模型、SQLite 存储、权限服务、审计链、HTTP 路由、离线验收）与治理模块（`eligibility_storage.py`、`eligibility_service.py`、`eligibility_api.py`、`eligibility_acceptance.py`）；
- tests/：基础规则、事务边界、接口路由、治理规则与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m creative_program_foundation.acceptance
    PYTHONPATH=src python3 -m creative_program_foundation.eligibility_acceptance

治理验收会在临时 SQLite 数据库中演示：个人/工作室/代理人重复占位被拦截、未成年人监护代签、临近截止补正与换赛道、截止原子冻结、迟到材料只入申诉不回写原申请、重启后续审、以及任意历史时点解释；成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.eligibility_api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 `X-Actor-Id` 标识操作者（内部账号 id 或 `person:<id>` / `org:<id>` 参与者令牌），治理接口统一位于 `/eg/*`，与基础服务接口共库共存。服务重启后 SQLite 中的业务状态、只追加事件与审计历史继续保留。
