# QQ课程AI助手 — 模块详细规格

> 共 13 个模块，按数据流从上到下编号。每个模块包含：职责、输入输出、依赖、关键接口、技术选型。

---

## M01 Qclaw 接入层

### 职责
系统与 QQ 的唯一通信入口，负责所有消息的收发。

### 输入
- QQ 群消息事件（文本、文件上传）
- QQ 私聊消息事件（文本、文件）
- QQ 频道消息事件（助教专属频道）

### 输出
- 发送群消息
- 发送私聊消息
- 发送频道消息
- 下载群文件 / 私聊文件

### 依赖
- Qclaw SDK

### 关键接口
```python
class QclawAdapter:
    def on_group_message(handler)        # 注册群消息回调
    def on_private_message(handler)      # 注册私聊回调
    def on_channel_message(handler)      # 注册频道回调
    def send_private(qq, text)           # 私聊发送
    def send_group(group_id, text)       # 群消息发送
    def send_channel(channel_id, text)   # 频道消息发送
    def download_file(file_id) -> path   # 文件下载
    def get_group_members(group_id)      # 获取群成员列表
    def get_member_role(group_id, qq)    # 获取成员角色（教师/助教/学生）
```

### 技术选型
- Qclaw（QQ 协议框架）

---

## M02 意图识别路由

### 职责
解析用户消息，识别操作意图，路由到对应处理模块。管理多轮会话状态。

### 输入
- 用户消息（文本）
- 消息来源（群/私聊/频道）
- 发送者身份（教师/助教/学生）
- 会话上下文

### 输出
- 意图标签 + 参数提取结果
- 路由目标模块

### 依赖
- M01（消息来源）

### 意图分类

| 意图 | 触发条件 | 路由目标 |
|------|----------|----------|
| UPLOAD_COURSEWARE | 群文件 + 标签 `#课件` | M05 |
| SET_RUBRIC | 群文件 + 标签 `#评分细则` | M04 |
| SET_ASSIGNMENT | 群文件/消息 + 标签 `#作业要求` | M04 |
| SUBMIT_HOMEWORK | 私聊文件（学生） | M06 |
| ASK_QUESTION | 私聊/群内 @机器人 提问 | M05 → M07 |
| APPEAL | 私聊回复「申诉」 | M11 |
| VIEW_REPORT | 私聊「查看报告」 | M12 |
| TA_APPROVE | 频道消息「同意」/「1」 | M09 → M11 |
| TA_REJECT | 频道消息「驳回」+ 原因 | M09 → M11 |
| QUERY_PROGRESS | 教师/助教查询「进度」 | M10 |

### 关键接口
```python
class IntentRouter:
    def classify(message, context, sender_role) -> Intent
    def get_session(user_qq) -> Session
    def set_session(user_qq, state)       # 设置多轮状态
    def clear_session(user_qq)            # 清除会话
```

### 会话状态机（关键场景）

**作业提交多轮对话**：
```
IDLE → WAITING_COURSE_CONFIRM → SUBMITTED
         ↑ 学生发文件        ↑ 学生选课程号
```

**申诉多轮对话**：
```
GRADED → WAITING_APPEAL_REASON → APPEAL_SUBMITTED
           ↑ 学生发「申诉」      ↑ 学生提交理由
```

---

## M03 文件解析引擎

### 职责
将各种格式的文件统一解析为纯文本 + 结构化元数据，供下游模块使用。

### 输入
- 文件本地路径
- 文件 MIME 类型 / 扩展名

### 输出
```python
@dataclass
class ParsedFile:
    text: str                    # 提取的纯文本
    pages: list[str]             # 按页分割（PDF有效）
    code_files: dict[str, str]   # zip内的代码文件 {filename: content}
    metadata: dict               # 文件名、大小、格式、页数等
```

### 依赖
- 无（底层工具模块）

### 关键接口
```python
class FileParser:
    def parse(file_path) -> ParsedFile
    def parse_docx(path) -> str
    def parse_pdf(path) -> list[str]       # 返回每页文本
    def parse_zip(path) -> dict[str, str]  # 返回 {文件名: 内容}
    def parse_code(path) -> str            # 代码文件，带语言标注
```

### 技术选型
- python-docx（Word）
- pdfplumber（PDF）
- zipfile（ZIP 压缩包）
- pygments（代码语法识别，辅助提取）

### 注意事项
- PDF 扫描件（纯图片 PDF）v1 暂不支持 OCR，记录到日志并通知学生
- ZIP 内超过 50 个文件时截断，避免解析炸弹
- 代码文件保留文件名，便于批改时定位

---

## M04 评分细则解析

### 职责
将教师发送的评分细则和作业要求文档，解析为结构化的评分配置。

### 输入
- M03 解析后的文本
- 标签类型（`#评分细则` / `#作业要求`）

### 输出
```python
@dataclass
class Rubric:
    assignment_id: str
    course_id: str
    total_score: int
    deadline: datetime
    dimensions: list[Dimension]       # 评分维度
    hard_deductions: list[HardRule]   # 硬性扣分项
    version: int
    created_at: datetime

@dataclass
class Dimension:
    name: str           # "问题分析"
    max_score: int      # 30
    criteria: str       # "是否准确识别核心问题，是否结合课件概念"

@dataclass
class HardRule:
    condition: str      # "抄袭"
    penalty: int        # -100（负数代表扣分）
```

### 依赖
- M03（文件解析）

### 关键接口
```python
class RubricParser:
    def parse(text, tag_type, course_id) -> Rubric
    def try_structured_parse(text) -> Rubric | None   # 优先尝试结构化解析
    def llm_fallback_parse(text) -> Rubric            # LLM 兜底解析
```

### 解析策略（双层）
1. **结构化解析**（优先）：识别 Markdown 表格、JSON、编号列表
2. **LLM 语义解析**（兜底）：用 prompt 将自然语言转为结构化数据

### 存储规则
- 同一 assignment_id 的细则可多次更新，版本号自增
- 批改时始终使用最新版本
- 历史版本保留，教师可查看变更

---

## M05 知识库管理

### 职责
管理课件的向量化存储与检索，按 course_id 隔离。支持增量更新。

### 输入
- M03 解析后的课件文本
- course_id
- 学生提问文本（检索时）

### 输出
- 检索结果：top-K 相关段落 + 来源标注（章节/页码）

### 依赖
- M03（文件解析）
- 向量数据库

### 关键接口
```python
class KnowledgeBase:
    def add_courseware(course_id, parsed_file, metadata)   # 增量添加
    def update_courseware(course_id, file_id, parsed_file) # 更新已有课件
    def search(course_id, query, top_k=5) -> list[Result]  # 检索
    def delete_courseware(course_id, file_id)              # 删除
    def list_courseware(course_id) -> list[Metadata]       # 列出已有课件
```

### Result 结构
```python
@dataclass
class SearchResult:
    text: str           # 匹配的文本段落
    source: str         # "第3章-数据库设计.pptx"
    page: int | None    # 页码（如有）
    chapter: str | None # 章节标题（如有）
    score: float        # 相似度分数
```

### 技术选型
- Chroma（轻量，适合开发和小规模部署）
- Qdrant（生产级，支持 namespace 隔离）
- Embedding 模型：text-embedding-3-small / bge-large-zh

### 切片策略
- 按章节标题分块（优先）
- 回退按 512 token 滑动窗口分块
- 每块保留元数据（文件名、章节、页码）

---

## M06 作业接收与归档

### 职责
处理学生私聊提交作业的完整流程：归属确认、文件存储、状态初始化。

### 输入
- 学生私聊文件
- 学生 QQ 号
- 课程选择结果

### 输出
- 提交确认消息
- 触发批改任务

### 依赖
- M01（文件下载）
- M03（文件解析）
- M02（会话状态管理）
- M13（存储）

### 关键接口
```python
class HomeworkReceiver:
    def receive_file(student_qq, file_path) -> Confirmation
    def confirm_course(student_qq, course_choice) -> str    # 返回确认消息
    def check_duplicate(student_qq, assignment_id) -> bool  # 是否重复提交
    def check_deadline(assignment_id) -> DeadlineStatus     # 是否超时
    def archive(submission) -> str                          # 存储落盘，返回 submission_id
```

### 去重策略
- 同一学生 + 同一 assignment_id：允许覆盖提交（截止时间前）
- 覆盖时旧文件保留，标记为 superseded

### 截止时间处理
- 截止时间前：正常提交
- 截止时间后：允许提交但标记 `late=True`，批改报告中注明
- 教师可配置迟交扣分规则（如每迟1天扣5分）

---

## M07 AI 批改引擎

### 职责
系统核心。接收作业文本，结合知识库和评分细则，输出结构化批改结果。

### 输入
- 作业文本（来自 M06）
- 评分细则（来自 M04）
- 知识库检索结果（来自 M05）

### 输出
```python
@dataclass
class GradingResult:
    submission_id: str
    total_score: int
    dimensions: list[DimensionResult]
    confidence: str           # "high" / "medium" / "low"
    grading_context: str      # AI 使用的完整上下文（用于申诉时回溯）

@dataclass
class DimensionResult:
    name: str
    score: int
    max_score: int
    gain_points: list[str]           # 得分点
    deductions: list[Deduction]      # 扣分点
    dimension_comment: str           # 该维度总评

@dataclass
class Deduction:
    point: str          # 扣分描述
    deduct: int         # 扣分值
    evidence: str       # 引用依据（课件/细则）
    evidence_source: str # 来源标注（"课件P12" / "评分细则"）
```

### 依赖
- M04（评分细则）
- M05（知识库检索）
- LLM API

### 关键接口
```python
class GradingEngine:
    def grade(submission, rubric, course_id) -> GradingResult
    def grade_objective(answers, answer_key) -> DimensionResult
    def grade_subjective(text, rubric_dim, course_id) -> DimensionResult
    def regrade_with_appeal(submission, original_result, appeal_reason, course_id) -> GradingResult
```

### 主观题批改 Prompt 结构
```
你是一位课程助教，请根据以下信息批改学生的作业。

【作业要求】
{assignment_requirements}

【评分细则 - {dimension_name}维度】
满分：{max_score}
评分标准：{criteria}

【课件参考资料】
{rag_results_with_sources}

【学生作业内容】
{student_text}

请严格按以下格式输出：
1. 得分点（列出学生做得好的地方）
2. 扣分点（每项包含：扣分描述、扣分值、引用依据、来源标注）
3. 该维度得分
4. 置信度（high/medium/low）
```

### 置信度规则
- `high`：课件中有明确对应内容，评分细则描述清晰
- `medium`：课件相关内容间接，需要推理
- `low`：课件无直接相关内容，评分依据薄弱 → 自动标记，推送给助教

### 重评逻辑（申诉场景）
- 保留原始 `grading_context`（AI 当时看到的所有上下文）
- 新增 `appeal_reason` 作为额外输入
- Prompt 要求 AI 说明"哪些分项变了，为什么变"
- 输出 `regrade_diff`：`{dimension: {old: x, new: y, reason: "..."}}`

---

## M08 批改结果通知

### 职责
将批改结果以文字摘要形式私信发送给学生，并提供申诉入口。

### 输入
- M07 的 GradingResult
- 学生 QQ 号

### 输出
- 私信消息（文字摘要）
- 更新会话状态（学生可回复「申诉」）

### 依赖
- M01（私信发送）
- M02（会话状态管理）

### 关键接口
```python
class ResultNotifier:
    def format_summary(result) -> str           # 格式化文字摘要
    def send_to_student(qq, result)             # 私信发送
    def on_student_reply(qq, message) -> str    # 处理学生后续回复（申诉/查看报告）
```

### 消息模板
```
【作业批改结果】{course_name} - {assignment_name}

总分：{total} / {max}

分项得分：
{dimension_lines}

主要扣分原因：
{top_deductions}

如需查看完整批改报告（含课件依据）：
→ 回复「报告」

如有异议，回复「申诉」并说明理由。
```

---

## M09 助教频道管理

### 职责
在助教专属频道推送待审申诉，解析助教操作指令。

### 输入
- M11 的申诉记录
- 助教频道消息（批准/驳回指令）

### 输出
- 申诉推送消息
- 助教操作结果（传回 M11）

### 依赖
- M01（频道消息收发）
- M02（意图识别）
- M11（申诉状态管理）

### 关键接口
```python
class TAChannelManager:
    def push_appeal(appeal, original_result, regrade_result)   # 推送申诉到频道
    def parse_ta_instruction(message) -> TAInstruction         # 解析助教指令
    def confirm_decision(appeal_id, decision, note)            # 确认决定
```

### 推送消息模板
```
【申诉待审】学生 {name} | {course} - {assignment}
原分：{old_score} → AI重评：{new_score}（{diff:+d}分）
申诉理由：{reason}
变更说明：{change_description}

回复「同意」或「1」→ 批准
回复「驳回 + 原因」→ 驳回
```

### 助教指令解析
```
"同意" / "1" / "批准" → TAInstruction(action="approve")
"驳回 不够充分" / "2 不够充分" → TAInstruction(action="reject", note="不够充分")
```

---

## M10 教师统计与预警

### 职责
向教师推送作业统计信息，以及异常预警。

### 输入
- 作业提交记录
- 批改结果（含置信度）
- 抄袭检测结果

### 输出
- 统计推送消息（群内或私信）
- 预警消息

### 依赖
- M01（消息发送）
- M13（数据查询）

### 关键接口
```python
class TeacherStats:
    def get_progress(course_id, assignment_id) -> ProgressInfo
    def format_progress(info) -> str
    def push_progress(teacher_qq, info)          # 推送统计
    def check_anomalies(course_id, assignment_id) -> list[Alert]
    def push_alert(teacher_qq, alert)            # 推送预警
```

### 预警类型

| 预警 | 触发条件 | 级别 |
|------|----------|------|
| 低置信度 | confidence == "low" | 提醒 |
| 疑似抄袭 | 相似度 > 80% 且不同学生 | 警告 |
| 未提交率高 | 截止前24h 提交率 < 30% | 提醒 |

### 推送消息模板
```
【交作业进度】{course} - {assignment}
截止时间：{deadline}
已提交：{submitted}/{total}（{rate}%）
未提交：{not_submitted}人
批改完成：{graded}/{submitted}
待批改：{pending}份
申诉中：{appealing}份

⚠️ 低置信度批改：{low_confidence_count}份，建议人工复核
```

---

## M11 申诉处理

### 职责
管理申诉的完整生命周期：接收、AI 重评、助教审核、终态确认。

### 输入
- 学生申诉理由
- 原始批改结果
- 助教操作结果

### 输出
- 重评结果
- 终态分数
- 通知消息

### 依赖
- M07（AI 重评）
- M09（助教频道交互）
- M08（通知学生）
- M13（数据持久化）

### 状态机
```
GRADED ──[学生申诉]──→ APPEALING ──[AI重评]──→ UNDER_REVIEW ──[助教批准]──→ APPEAL_APPROVED
                                                    │
                                                    └──[助教驳回]──→ APPEAL_REJECTED
```

### 关键接口
```python
class AppealHandler:
    def submit_appeal(submission_id, student_qq, reason) -> AppealRecord
    def validate_appeal(submission_id, student_qq) -> bool  # 是否可申诉（1次限制+48h窗口）
    def trigger_regrade(appeal_id) -> RegradeResult
    def apply_ta_decision(appeal_id, decision, note) -> FinalResult
    def get_appeal_history(submission_id) -> list[AppealRecord]
```

### 申诉限制
- 每次作业限申诉 1 次
- 申诉窗口：批改完成后 48 小时内
- 超出窗口提示：「申诉窗口已关闭」

### 终态规则
- 助教批准 → 新分数写入，替换原分
- 助教驳回 → 原分维持
- 两种情况都私信通知学生

---

## M12 PDF 报告生成

### 职责
按需生成详细的 PDF 批改报告，包含完整的得分点、扣分点和课件依据。

### 输入
- GradingResult
- 申诉历史（如有）
- 学生信息

### 输出
- PDF 文件路径（用于发送或链接）

### 依赖
- M07（批改结果）
- M11（申诉历史）
- M13（文件存储）

### 关键接口
```python
class ReportGenerator:
    def generate_pdf(grading_result, appeal_history=None) -> str  # 返回文件路径
    def get_or_generate(submission_id) -> str                     # 懒加载：有缓存直接返回
    def invalidate_cache(submission_id)                           # 申诉重评后使缓存失效
```

### PDF 内容结构
```
1. 封面：课程名、作业名、学生信息、提交时间、批改时间
2. 总分概览：雷达图（各维度得分率）
3. 分项详情：
   - 每个维度的得分/满分
   - 得分点列表
   - 扣分点列表（含引用依据和来源页码）
4. 申诉记录（如有）：
   - 申诉理由
   - AI 重评结果
   - 助教决定
5. 附录：完整评分细则
```

### 技术选型
- WeasyPrint（HTML→PDF，灵活排版）
- ReportLab（纯 Python PDF，更底层）

### 懒加载策略
- 批改完成时不自动生成 PDF
- 学生请求时生成并缓存
- 申诉重评后使缓存失效，下次请求重新生成

---

## M13 数据与存储层

### 职责
统一管理所有持久化存储，为上层模块提供数据访问接口。

### 存储分区

| 存储 | 内容 | 隔离方式 | 选型 |
|------|------|----------|------|
| 向量数据库 | 课件 embedding | course_id namespace | Chroma / Qdrant |
| 关系数据库 | 作业、评分、申诉、配置 | course_id 字段 | SQLite / PostgreSQL |
| 文件存储 | 学生作业原文、PDF 报告 | 目录按 course_id 组织 | 本地 / MinIO |
| 配置与缓存 | 会话状态、临时数据 | user_qq 为 key | Redis / 内存字典 |

### 关系数据库表结构

```sql
-- 课程与群绑定
CREATE TABLE course_groups (
    course_id  TEXT NOT NULL,
    group_id   TEXT NOT NULL,
    group_name TEXT,
    PRIMARY KEY (course_id, group_id)
);

-- 作业定义
CREATE TABLE assignments (
    id         TEXT PRIMARY KEY,  -- assignment_id
    course_id  TEXT NOT NULL,
    title      TEXT NOT NULL,
    deadline   DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 评分细则（版本化）
CREATE TABLE rubrics (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id      TEXT NOT NULL,
    assignment_id  TEXT NOT NULL,
    content_json   TEXT NOT NULL,  -- 序列化的 Rubric 对象
    version        INTEGER NOT NULL,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(assignment_id, version)
);

-- 作业提交
CREATE TABLE submissions (
    id             TEXT PRIMARY KEY,
    student_qq     TEXT NOT NULL,
    course_id      TEXT NOT NULL,
    assignment_id  TEXT NOT NULL,
    file_path      TEXT NOT NULL,
    file_type      TEXT,           -- docx/pdf/zip/code
    submitted_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_late        BOOLEAN DEFAULT FALSE,
    status         TEXT DEFAULT 'submitted',  -- submitted/grading/graded/appealing/appeal_approved/appeal_rejected
    score          INTEGER,
    report_path    TEXT
);

-- 批改详情
CREATE TABLE grading_details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   TEXT NOT NULL REFERENCES submissions(id),
    dimension_json  TEXT NOT NULL,   -- 序列化的 list[DimensionResult]
    confidence      TEXT NOT NULL,   -- high/medium/low
    grading_context TEXT,            -- AI 使用的完整上下文（用于申诉回溯）
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 申诉记录
CREATE TABLE appeals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   TEXT NOT NULL REFERENCES submissions(id),
    student_reason  TEXT NOT NULL,
    original_score  INTEGER NOT NULL,
    ai_new_score    INTEGER,
    regrade_detail  TEXT,            -- 序列化的 regrade diff
    ta_decision     TEXT,            -- approved/rejected
    ta_note         TEXT,
    decided_at      DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 群成员角色
CREATE TABLE group_members (
    group_id   TEXT NOT NULL,
    qq         TEXT NOT NULL,
    role       TEXT NOT NULL,  -- teacher/ta/student
    name       TEXT,
    PRIMARY KEY (group_id, qq)
);
```

### 文件存储目录结构
```
data/
├── courses/
│   ├── C001/
│   │   ├── courseware/          # 课件原文
│   │   ├── submissions/         # 学生作业
│   │   │   ├── {submission_id}/
│   │   │   │   ├── original.ext # 原始提交文件
│   │   │   │   └── report.pdf   # 批改报告
│   │   └── rubrics/             # 评分细则原文
│   └── C002/
└── vector_db/                   # 向量数据库数据目录
```

---

## 模块依赖关系总结

```
M01 (Qclaw接入)
 └→ M02 (意图路由)
     ├→ M04 (细则解析) ──→ M03 (文件解析)
     ├→ M05 (知识库)   ──→ M03
     ├→ M06 (作业归档) ──→ M03
     ├→ M07 (批改引擎) ──→ M04 + M05 + M06
     │   ├→ M08 (结果通知)
     │   ├→ M09 (助教频道)
     │   └→ M10 (教师统计)
     ├→ M11 (申诉处理) ──→ M07 + M09 + M08
     └→ M12 (PDF报告)  ──→ M07 + M11

M13 (存储层) ← 被所有模块依赖
```

## 建议开发顺序

```
Phase 1: 基础骨架（让数据能流转）
  M13 → M01 → M02 → M03

Phase 2: 知识入库（让 AI 有知识可用）
  M05 → M04

Phase 3: 批改闭环（核心价值）
  M06 → M07 → M08

Phase 4: 申诉与报告（体验完善）
  M11 → M09 → M12

Phase 5: 教师端（锦上添花）
  M10
```
