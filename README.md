# 数字档案长期保存服务

仅使用 Python 3.11+ 标准库实现的独立档案保存项目，支持清单校验、真实 SHA-256 内容校验、多个离线副本、损坏检测与自动修复、格式迁移、保留期限和访问控制，并提供**到期处置台**：处置批次、审计保全冻结、执行前期限/冻结复核、处置后只读留存。

## 代码结构（规则、数据、请求入口分开维护）

| 文件 | 职责 |
| --- | --- |
| `rules.py` | 纯业务规则：到期判定、阻塞项、处置条目状态机、执行闸。无 IO，可独立单测 |
| `errors.py` | 跨层错误类型（HTTP 状态码、错误代码、结构化 details） |
| `store.py` | 数据层：SQLite 表结构、事务、多副本校验/修复/迁移、处置批次、冻结与审计 |
| `app.py` | 请求入口：HTTP 路由、JSON 编解码、静态页面，不含业务判定 |
| `web/index.html` `web/style.css` `web/app.js` | 处置台页面：结构 / 样式 / 逻辑（请求统一经 `api` 入口） |

## 运行

```bash
python3 app.py --init --seed   # seed 会写入两个已到期和一个未到期演示档案
python3 app.py
```

服务地址为 <http://127.0.0.1:8102>，默认数据库 `preservation.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`owner`、`archivist`、`auditor`、`outsider`。API 使用 `X-User-Id`。文件通过 Base64 提交，单文件上限 10 MiB；这是为了保持示例自包含，生产部署应换成对象存储和流式上传。

## 到期处置台规则

条目状态机：**待确认 pending → 可处置 ready → 已处置 disposed**；任意阶段可被审计员置为**已冻结 frozen**。

1. 管理员（owner/archivist）建立处置批次，把已到期档案纳入批次；未到期档案纳入后停留在「待确认」。
2. 审计员（auditor）对有争议档案做**保全冻结**（须填写理由，写入审计记录）；只有审计员且为档案成员可冻结/解冻。
3. 管理员可「核对」批次：按当前保留期限与冻结状态重算阻塞项；
   - 期限快照与当前不一致（期限被延长）或出现冻结 → 条目退回「待确认/已冻结」，确认标记清空；
   - 无变化才允许进入/保持「可处置」。
4. 执行处置时服务端**再核对一次**：批次内只要存在非 ready 条目，整体拒绝执行（HTTP 409，返回阻塞明细），核对结果落库；全部 ready 才执行。
5. 执行后档案标记 `disposed_at` 进入只读：版本、副本、迁移与审计记录全部保留可查，版本写入/副本创建/迁移等变更被拒绝。
6. 页面按「待确认 / 可处置 / 已冻结 / 已处置」分组，列出每个阻塞项及批次/条目的最近核对时间。

阻塞项：`retention_not_due`（期限未到）、`retention_changed`（确认后期限变化）、`frozen`（保全冻结）、`already_disposed`（已在别处处置）。

## 处置台接口

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `GET /api/disposition/console` | 全部授权角色 | 处置台总览：四分组、阻塞项、批次及最近核对时间 |
| `GET /api/disposition/candidates` | 全部授权角色 | 已到期、未处置、未在打开批次中的候选（无权不可见） |
| `POST /api/disposition/batches` | owner/archivist | 建批次：`{name, archive_ids}` |
| `GET /api/disposition/batches` | 全部授权角色 | 批次列表 |
| `GET /api/disposition/batches/{id}` | 全部授权角色 | 批次详情（分组、阻塞项、最近核对时间） |
| `POST /api/disposition/batches/{id}/archives` | owner/archivist | 追加到期档案：`{archive_ids}` |
| `DELETE /api/disposition/batches/{id}/items/{itemId}` | owner/archivist | 移出条目（已冻结须先解冻） |
| `POST /api/disposition/batches/{id}/items/{itemId}/freeze` | auditor | 保全冻结：`{reason}` |
| `POST /api/disposition/batches/{id}/items/{itemId}/unfreeze` | auditor | 解除冻结 |
| `POST /api/disposition/batches/{id}/check` | 全部授权角色 | 执行前核对期限与冻结状态 |
| `POST /api/disposition/batches/{id}/confirm` | owner/archivist | 确认勾选条目：`{item_ids}`，有阻塞即退回 |
| `POST /api/disposition/batches/{id}/execute` | owner/archivist | 执行处置（内部再次核对，409 返回阻塞明细） |
| `POST /api/archives/{id}/retention` | owner/archivist（写权限） | 向后调整保留期限，打开批次中的 ready 条目退回待确认 |

## 原有接口

- `POST /api/archives`：创建受限档案。
- `POST /api/archives/{id}/members`：所有者授予 read/write 权限。
- `POST /api/archives/{id}/versions`：提交文件清单，服务端重新计算哈希和大小。
- `GET /api/versions/{id}`：查看版本、文件清单和副本状态。
- `POST /api/versions/{id}/copies`：创建独立副本内容。
- `POST /api/copies/{id}/verify`：校验副本；发现损坏时从健康副本修复。
- `POST /api/copies/{id}/simulate-corruption`：演示/测试介质损坏，仅 owner 或 archivist 可用。
- `POST /api/versions/{id}/migrate`：生成格式迁移后的新版本并保留派生关系。
- `GET /api/archives/{id}/status`：保留期限、剩余天数、冻结状态、处置状态、版本和审计记录。

档案路径拒绝绝对路径和 `..`；同一版本副本位置唯一；没有健康副本时版本标记为 `degraded`；所有变更（授权、版本、副本、迁移、期限调整、纳入/冻结/解冻/核对/确认/执行）写入审计日志。
