# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、来源对账、意见冲突和状态机。
- `src/repository.py`：SQLite、事务、乐观版本和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources` 和 `POST /api/items/<id>/actions`。身份使用 `X-User-Id`、`X-Role` 请求头。

## 先对账再放行链路

外部门户会并发、晚到地推送观测。系统对来源记录、风险评估、运营方意见和规避动作实行一条单向联动链路：

1. **按来源标识取最新观测（对账）**：来源以 `source_type + external_id` 标识，
   同一标识只保留每个观测时刻的记录；更旧观测时间的晚到记录被拒绝
   （`409 stale_observation`）。`reconcile_sources` 取每个来源的最新一批观测，
   按逆协方差（精度）加权融合出距离与协方差，并生成随来源内容变化的指纹。
   融合是演示模型，不替代真实轨道力学。
2. **并发重复提交幂等**：对账、去重、失效重算在同一个 SQLite 写事务
   （`BEGIN IMMEDIATE`）内完成。同一来源、同一观测时刻、同一负载的并发/重试提交
   只落一条记录，返回 `200` 且实体版本不变；新记录返回 `201`。同一时刻不同负载
   返回 `409 source_conflict`。
3. **来源变化即失效并重算**：任一有效来源使融合指纹变化时，既有风险评估、
   运营方意见（含冲突标记）和已批准的机动建议全部清空；若实体曾评估过则立即重算，
   状态回到 `assessed`。尚未评估的待处理实体只刷新基线数据。
4. **重算失败可恢复**：重算失败（如轨道过期）时来源记录仍已落库，实体进入
   `assessment_state = failed`，期间禁止评估、批准和执行；分析/协调方可调用
   `recover_assessment` 动作，完全从来源记录重建并重算。
5. **全部运营方确认前不放行**：协调方 `approve` 后进入 `maneuver_pending`，
   每个运营方提交 `record_opinion`。名单内所有运营方都 `approve` 才自动释放到
   `coordinating` 并允许 `execute`；任一 `reject`/`request_review` 或尚有未确认方
   都停在待确认状态。运营方不能用不同意见覆盖已提交意见；相同意见重复提交幂等。
   协调方可重新 `approve` 开启新一轮确认。来源变化也会把待确认/协调中的机动建议
   打回，必须重新评估、批准并重新收齐确认。

状态机：`pending → assessed → maneuver_pending → coordinating → executing → resolved`，
任意前序状态可由协调方 `cancel`。终态（`resolved`/`cancelled`）不再接收来源观测。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、待确认与全部确认放行、执行、解决、重复告警、权限、版本冲突、
过期轨道、意见冲突、按来源取最新、多来源精度加权融合、并发幂等（含真实 HTTP 线程）、
晚到来源使链路失效重算、重算失败后从来源记录恢复、旧库唯一约束迁移。
数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和
空间交通协调服务。
