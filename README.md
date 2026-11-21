# 主数据清洗与黄金记录平台

面向多源主数据的匹配识别、冲突消解、合并清洗与黄金记录管理的主数据治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 来源系统注册 | `source_system` | 来源系统 | 系统编号、系统名称、系统类型 |
| 实体模型定义 | `entity_model` | 实体模型 | 模型编号、模型名称、业务域 |
| 匹配规则引擎 | `match_rule` | 匹配规则 | 规则编号、实体模型、匹配字段 |
| 疑似重复簇 | `dup_cluster` | 重复簇 | 簇编号、实体模型、簇大小 |
| 合并追溯日志 | `merge_log` | 合并记录 | 记录编号、黄金ID、来源记录 |
| 黄金记录管理 | `golden_record` | 黄金记录 | 黄金ID、实体模型、关联来源 |
| 字段标准化清洗 | `field_cleanse` | 清洗规则 | 规则编号、实体模型、目标字段 |
| 优胜策略配置 | `survivorship_rule` | 优胜策略 | 策略编号、实体模型、选择字段 |
| 数据入库规则 | `data_import_rule` | 入库规则 | 规则编号、来源系统、实体模型 |
| 批次导入管理 | `import_batch` | 导入批次 | 批次编号、来源系统、实体模型 |
| 记录级血缘 | `record_lineage` | 血缘记录 | 血缘编号、黄金ID、来源记录 |
| 主数据分发同步 | `mdm_sync` | 同步配置 | 配置编号、黄金实体、目标系统 |
| 黄金记录版本 | `mdm_version` | 版本记录 | 版本编号、黄金ID、版本号 |
| 自动匹配任务 | `auto_match_job` | 匹配任务 | 任务编号、实体模型、匹配规则 |
| 主数据质量统计 | `mdm_quality` | 质量统计 | 统计编号、实体模型、完备率 |
| 参考数据管理 | `mdm_reference` | 参考数据 | 数据编号、数据类别、标准编码 |
| 主数据查询接口 | `mdm_api` | 查询接口 | 接口编号、实体模型、查询字段 |
| 主数据治理看板 | `mdm_dashboard` | 看板配置 | 看板编号、看板名称、监控实体 |

## 批量匹配性能

`app/services/match_rule.py` 的 `run_match_round` 是一轮批量匹配的唯一入口：

- **规则只解析一次**：轮次开始时把规则复制成 `MatchRuleSpec` 快照，整轮打分都使用这份
  快照（`rule_lookups == 1`），不再为每个记录对重新读取规则。
- **先算上界再打分**：对每个字段用长度上界和字符多重集重叠上界求相似度上界，
  四舍五入后仍低于阈值的组合直接跳过，不调用 `SequenceMatcher`。上界是严格成立的，
  因此不会漏掉任何达标组合。
- **口径不变**：保留下来的组合仍按原公式打分，`分数 >= 阈值`（临界分数算达标）
  建立连边，再对连边做连通分量，所以 A 与 C 即使不直接达标，经 B 连通仍在同一簇。
- **一轮一个口径**：规则与输入都在锁定状态下复制快照；计算期间运营人员更正阈值，
  本轮仍按开始时的快照输出，任务会被标记 `可供审核=不可审核`，审核人员不会拿到
  混用两种参数的簇。

相关接口（`app/routers/match.py`）：

| 接口 | 用途 |
| --- | --- |
| `POST /api/match/round` | 执行一轮匹配，返回簇、规则/输入签名和实做评分次数 |
| `POST /api/match/jobs` | 调度自动匹配任务，并把簇成员/簇数登记到疑似重复簇 |
| `POST /api/match/dedup` | 整批去重：旧实现与优化实现对照后再登记簇 |
| `GET /api/match/jobs/{id}/review` | 取审核名单；任务不可审核时返回原因 |
| `POST /api/match/rules/{id}/parameters` | 运营人员更正规则阈值 |

自检脚本（在 `backend` 目录）：

```bash
.venv/bin/python app/tests/test_match_optimization.py   # 8 项口径/并发/性能测试
.venv/bin/python app/tests/demo_match_optimization.py   # abc / abcd / bcd 实操演示
```

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
