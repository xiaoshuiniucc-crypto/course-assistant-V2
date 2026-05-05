# QQ课程AI助手 — 双人分工方案

> 基于模块依赖关系，最大化并行度，最小化阻塞等待。
> **你（前端/交互背景）** 负责交互侧，**小伙伴（NLP/LLM背景）** 负责 AI 核心侧。

---

## 一、分工原则

1. **按数据流分层**：小伙伴负责数据生产侧（解析→知识→AI），你负责数据消费侧（交互→通知→审核）
2. **接口契约先行**：两人共享接口定义文件，各自开发时用 Mock 数据解耦
3. **关键路径优先**：小伙伴的 M07（批改引擎）是系统核心，必须最优先完成
4. **合流点明确**：Phase 3 开始两人协作，此时基础模块都已就绪

---

## 二、角色分配

### 🧑 你 — 交互流程 + 业务闭环（前端/交互背景）

| 优先级 | 模块 | 核心任务 | 预估复杂度 |
|--------|------|----------|-----------|
| P0 | M01 Qclaw 接入层 | 消息监听、私聊/群聊/频道收发、文件下载 | 高 |
| P0 | M02 意图识别路由 | hashtag 规则 + 会话状态机 + 参数提取 | 中 |
| P1 | M06 作业接收与归档 | 多轮对话、去重、迟交标记、触发批改 | 中 |
| P1 | M08 批改结果通知 | 文字摘要格式化、申诉入口、私信发送 | 低 |
| P1 | M09 助教频道管理 | 推送模板、指令解析（同意/驳回） | 低 |
| P2 | M10 教师统计与预警 | 进度查询、异常预警格式化 | 低 |

**你的关键交付物**：
- `QclawAdapter` — 完整的 QQ 消息收发适配器
- `IntentRouter.classify()` — 意图识别 + 路由
- `HomeworkReceiver.receive_file()` → `confirm_course()` — 完整提交流程
- `ResultNotifier.format_summary()` — 批改结果格式化
- `TAChannelManager.push_appeal()` + `parse_ta_instruction()` — 助教频道推送与指令解析

### 🤝 小伙伴 — 基础设施 + AI 核心（NLP/LLM 背景）

| 优先级 | 模块 | 核心任务 | 预估复杂度 |
|--------|------|----------|-----------|
| P0 | M13 数据存储层 | SQL 表建表、CRUD 接口、文件目录结构 | 中 |
| P0 | M03 文件解析引擎 | docx/pdf/zip/code 四种格式解析 | 中 |
| P1 | M05 知识库管理 | Chroma/Qdrant 接入、切片策略、course_id 隔离 | 高 |
| P1 | M04 评分细则解析 | 双层解析（结构化 + LLM 兜底） | 中 |
| P2 | M07 AI 批改引擎 | Prompt 工程、评分逻辑、置信度、重评 | **极高** |
| P2 | M11 申诉处理 | 状态机、AI 重评、终态确认 | 中 |
| P2 | M12 PDF 报告生成 | 懒加载、缓存失效、HTML→PDF | 中 |

**小伙伴的关键交付物**：
- `KnowledgeBase.search()` — 给定 course_id + query，返回 top-K 课件段落
- `RubricParser.parse()` — 给定文本，输出结构化 Rubric
- `GradingEngine.grade()` — 给定作业 + 细则 + 知识库结果，输出 GradingResult
- `GradingEngine.regrade_with_appeal()` — 申诉重评
- `ReportGenerator.generate_pdf()` — PDF 报告生成

---

## 三、并行开发时间线

```
Week 1                    Week 2                    Week 3
────┬─────────────────────┬─────────────────────┬────────────────────
 你 │ M01(3d) → M02(2d)  │ M06(3d) → M08(2d)   │ M09(2d) 助教频道
    │                     │                     │
小伙伴│ M13(2d) → M03(3d)  │ M05(3d) → M04(2d)   │ M07(5d) 批改引擎
────┴─────────────────────┴─────────────────────┴────────────────────

Week 4                    Week 5
────┬─────────────────────┬────────────────────
 你 │ M10统计(2d) + 集成测试 │ 联调+修复+部署
    │                       │
小伙伴│ M11申诉(3d)→M12报告(2d)│ 联调+修复+部署
────┴─────────────────────┴────────────────────
```

### 里程碑

| 节点 | 时间 | 验收标准 |
|------|------|----------|
| M1: 基础就绪 | Week 1 末 | M13+M03 可解析文件；M01+M02 可识别意图并路由 |
| M2: 数据可通 | Week 2 末 | 课件可入库检索；作业可提交归档；通知可发送 |
| M3: 核心闭环 | Week 3 末 | 作业提交→AI批改→结果通知 全链路跑通 |
| M4: 功能完整 | Week 4 末 | 申诉+报告+统计全部就绪 |
| M5: 可交付 | Week 5 末 | 端到端测试通过，部署上线 |

---

## 四、跨人依赖与解耦方案

### 依赖矩阵

| 你依赖小伙伴 | 小伙伴依赖你 |
|--------------|---------------|
| M06 需要 M03 解析作业文件 | M07 无外部依赖（纯计算模块） |
| M08 需要 M07 的 GradingResult | — |
| M09 需要 M11 申诉记录 | M11 需要 M08 通知学生 |
| M11 需要 M07 重评能力 | — |
| M12 需要 M07 批改结果 | — |

### 解耦策略：接口契约 + Mock

**第一步**：两人一起在 `contracts/` 目录下定义所有接口（Python abstract class）：

```
contracts/
├── knowledge_base.py    # KnowledgeBase 抽象类
├── grading_engine.py    # GradingEngine 抽象类
├── file_parser.py       # FileParser 抽象类
├── qclaw_adapter.py     # QclawAdapter 抽象类
├── intent_router.py     # IntentRouter 抽象类
└── models.py            # 所有 dataclass 定义（Rubric, GradingResult, AppealRecord...）
```

**第二步**：各自开发时使用 Mock 实现：

```python
# 你在开发 M08 时，不需要等小伙伴的 M07 完成
from contracts.grading_engine import GradingEngine

class MockGradingEngine(GradingEngine):
    def grade(self, submission, rubric, course_id):
        return GradingResult(
            submission_id="mock_001",
            total_score=78,
            dimensions=[...],  # 假数据
            confidence="high",
            grading_context="mock"
        )
```

```python
# 小伙伴开发 M07 时，不需要等你的 M01 完成
# M07 是纯计算模块，输入来自函数参数而非 QQ 消息
# 直接用单元测试驱动开发即可
```

### 关键数据模型共享

**`contracts/models.py` 必须第一时间定义好**，这是两人协作的"合同"：

```python
from dataclasses import dataclass
from datetime import datetime
from typing import list, dict, int, str, Optional

@dataclass
class ParsedFile:
    text: str
    pages: list[str]
    code_files: dict[str, str]
    metadata: dict

@dataclass
class Rubric:
    assignment_id: str
    course_id: str
    total_score: int
    deadline: datetime
    dimensions: list["Dimension"]
    hard_deductions: list["HardRule"]
    version: int

@dataclass
class GradingResult:
    submission_id: str
    total_score: int
    dimensions: list["DimensionResult"]
    confidence: str
    grading_context: str

@dataclass
class AppealRecord:
    id: int
    submission_id: str
    student_reason: str
    original_score: int
    ai_new_score: Optional[int]
    ta_decision: Optional[str]   # approved/rejected
    ta_note: Optional[str]
    status: str   # APPEALING/UNDER_REVIEW/APPEAL_APPROVED/APPEAL_REJECTED
```

---

## 五、协作规范

### 1. Git 工作流

```
main (保护分支)
 ├── dev (开发基线)
 │    ├── feature/you-M01-qclaw
 │    ├── feature/you-M02-intent
 │    ├── feature/you-M06-homework
 │    ├── feature/you-M08-notify
 │    ├── feature/you-M09-tachannel
 │    ├── feature/you-M10-stats
 │    ├── feature/mate-M13-storage
 │    ├── feature/mate-M03-parser
 │    ├── feature/mate-M05-knowledge
 │    ├── feature/mate-M07-grading
 │    ├── feature/mate-M11-appeal
 │    └── feature/mate-M12-report
```

- 每个 feature 分支独立开发，通过 PR 合入 `dev`
- `dev` 每日至少合并一次，确保不会大幅偏离
- 两人互相 review PR（你 review 小伙伴的，小伙伴 review 你的）

### 2. 目录结构约定

```
qq-course-assistant/
├── contracts/          # 共享接口和数据模型（最先建立）
├── mocks/              # Mock 实现，用于独立开发
├── src/
│   ├── adapter/        # M01 Qclaw 接入（你）
│   ├── router/         # M02 意图路由（你）
│   ├── parser/         # M03 文件解析（小伙伴）
│   ├── rubric/         # M04 细则解析（小伙伴）
│   ├── knowledge/      # M05 知识库（小伙伴）
│   ├── homework/       # M06 作业归档（你）
│   ├── grading/        # M07 批改引擎（小伙伴）
│   ├── notify/         # M08 结果通知（你）
│   ├── ta_channel/     # M09 助教频道（你）
│   ├── stats/          # M10 教师统计（你）
│   ├── appeal/         # M11 申诉处理（小伙伴）
│   ├── report/         # M12 PDF 报告（小伙伴）
│   └── storage/        # M13 数据存储（小伙伴）
├── tests/              # 单元测试
├── data/               # 运行时数据目录
└── config/             # 配置文件
```

### 3. 每日同步

- **站会**（15分钟）：每人说三件事：昨天做了什么、今天做什么、有没有阻塞
- **接口变更**：修改 `contracts/` 下的文件必须通知对方，PR 标注 `breaking`
- **集成检查**：每周五在 `dev` 分支上跑一次端到端手动测试

---

## 六、Phase 3 合流后的模块归属

| 模块 | 主力 | 协助 | 原因 |
|------|------|------|------|
| M11 申诉处理 | 小伙伴 | 你 | 涉及 M07 重评逻辑，小伙伴更熟悉；你协助 M09 对接 |
| M12 PDF 报告 | 小伙伴 | — | 依赖 M07 批改结果结构，小伙伴对数据结构最清楚 |
| M10 教师统计 | 你 | — | 纯查询 + 格式化，与你的通知模块风格一致 |
| 集成测试 | 两人一起 | — | 端到端流程需要双方模块联调 |

---

## 七、风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| Qclaw SDK 文档不足 | 你的 M01 可能卡住 | 提前研读源码，备选方案用 NoneBot2 |
| M07 Prompt 调优耗时 | 整体进度延迟 | 小伙伴预留 buffer，v1 用简单 prompt 先跑通，迭代优化 |
| M05 向量库选型纠结 | 小伙伴的进度受阻 | v1 直接用 Chroma（零配置），后续再换 Qdrant |
| 接口定义频繁变更 | 两人互相影响 | contracts/ 加 CI 校验，变更必须走 PR |
| 两人进度不一致 | 合流时一方等另一方 | 慢的一方优先完成接口层（可以内部未实现），让对方先对接 |

---

## 八、启动清单

**Day 1 两人一起做的事**：

- [ ] 建仓库、拉分支、定目录结构
- [ ] 完成 `contracts/models.py` 所有关键数据结构
- [ ] 完成 `contracts/` 下所有接口的 abstract class
- [ ] 各自搭好开发环境（Python venv、依赖安装）
- [ ] 确定第一个 feature 分支

**你的 Day 2-3**：M01 → M02
**小伙伴的 Day 2-3**：M13 → M03
