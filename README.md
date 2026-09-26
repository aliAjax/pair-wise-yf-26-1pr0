# 数字档案长期保存服务

仅使用 Python 3.11+ 标准库实现的独立档案保存项目，支持清单校验、真实 SHA-256 内容校验、多个离线副本、损坏检测与自动修复、格式迁移、保留期限和访问控制。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

服务地址为 <http://127.0.0.1:8102>，默认数据库 `preservation.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`owner`、`archivist`、`auditor`、`outsider`。API 使用 `X-User-Id`。文件通过 Base64 提交，单文件上限 10 MiB；这是为了保持示例自包含，生产部署应换成对象存储和流式上传。

## 主要接口

- `POST /api/archives`：创建受限档案。
- `POST /api/archives/{id}/members`：所有者授予 read/write 权限。
- `POST /api/archives/{id}/versions`：提交文件清单，服务端重新计算哈希和大小。
- `GET /api/versions/{id}`：查看版本、文件清单和副本状态。
- `POST /api/versions/{id}/copies`：创建独立副本内容。
- `POST /api/copies/{id}/verify`：校验副本；发现损坏时从健康副本修复。
- `POST /api/copies/{id}/simulate-corruption`：演示/测试介质损坏，仅 owner 或 archivist 可用。
- `POST /api/versions/{id}/migrate`：生成格式迁移后的新版本并保留派生关系。
- `GET /api/archives/{id}/status`：保留期限、版本状态和审计记录。

## 到期处置台

处置台把"到期 → 处置"收敛成可追溯的批次流程，代码按职责分为三个模块：

- `disposition_rules.py`：纯业务规则（到期判定、人工核对、执行前闸门、分组），不依赖数据库。
- `disposition_repo.py`：数据层，维护处置批次、处置项、保全冻结表与事务。
- `app.py`：只新增 HTTP 请求入口，不内嵌规则判断。

处置项状态：`pending`（待确认）→ `ready`（可处置）→ `disposed`（已处置）；审计员可在任何阶段置入 `frozen`（已冻结），解冻后退回待确认。

接口：

- `GET /api/disposition/due`：列出已到期且未在未完结批次中的档案。
- `POST /api/disposition/batches`：owner/archivist 建立批次并纳入到期档案，新项一律待确认（`{"archive_ids":[1,2],"note":""}`）。
- `GET /api/disposition/batches`、`GET /api/disposition/batches/{id}`：批次列表与处置台视图，按**待确认 / 可处置 / 已冻结 / 已处置**分组，含阻塞项（`blockers`）与批次、每项的最近核对时间（`last_checked_at`）。
- `POST /api/disposition/items/{id}/recheck`：人工核对当前期限与冻结状态；仍到期且未冻结才进入可处置，同时以当前期限为确认基线。
- `POST /api/disposition/items/{id}/freeze` / `.../release`：仅 auditor 可保全冻结（需理由）或解除冻结，解冻退回待确认。
- `POST /api/disposition/batches/{id}/execute`：批次执行前对每项再过一次闸门——保留期限与确认基线不一致、或已被冻结的项**退回待确认**（409 `batch_blocked`，响应体携带退回后的批次状态），只有未变化的可处置项执行。

执行处置只把档案标记为 `disposed`，**不删除任何内容**：版本、文件清单、离线副本、格式迁移关系与全部审计记录仍可通过 `GET /api/versions/{id}`、`GET /api/archives/{id}/status` 查看；处置后所有写接口（授权、入库、加副本、迁移、损坏模拟）返回 409 `archive_disposed`。

`python3 app.py --init --seed` 会额外生成三个已到期演示档案，页面 `/` 即到期处置台。

档案路径拒绝绝对路径和 `..`；同一版本副本位置唯一；没有健康副本时版本标记为 `degraded`；所有变更写入审计日志。
