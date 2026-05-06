# QQ课程AI助手 PRD

版本：v1.0
日期：2026-05-06
状态：初稿

---

## Problem Statement

构建一个基于 QQ 生态的智能课程助手，解决教师、助教、学生三方在作业批改场景下的核心痛点。

**教师面临的困境：**

作业批改耗时巨大。一门课程往往有 30-100 名学生，每次作业人工批改需要 3-8 小时，且容易因疲劳出错。评分标准难以统一，不同学生、不同维度的评分尺度可能不一致，导致学生质疑公正性。缺乏数据支撑，无法快速了解整体交作业情况、哪些学生需要关注。

**助教面临的困境：**

申诉处理繁琐，需要反复查看学生作业原文、回忆评分依据，效率低下。QQ 群消息混杂，难以专注处理作业相关事务。缺乏系统化流程，申诉记录散落在聊天记录中，难以追溯和统计。

**学生面临的困境：**

黑箱评分困惑，只知道分数，不知道为什么扣分，难以针对性地改进。反馈周期长，作业提交后往往需要等待 3-7 天才能收到反馈。申诉门槛高，申诉流程不透明，担心被驳回就不敢申诉。课后学习孤立，课件看完就忘，遇到问题时无处提问，只能自己琢磨或等下次上课。

---

## Solution

让教师、助教、学生在熟悉的 QQ 环境中完成：

教师：上传课件 → 配置评分规则 → 查看统计

学生：提交作业 → 收到带依据的批改 → 如有异议发起申诉 → 群内 @机器人随时提问

助教：收到申诉推送 → 一键审批 → 全程留痕

核心理念：批改过程可溯源、评分依据全透明、申诉流程自动化、课后学习有助手。

---

## User Stories

**教师用户故事：**

1. As a teacher, I want to upload course materials and have them automatically vectorized, so that AI can grade assignments and answer questions based on course content.

2. As a teacher, I want to configure grading rubrics using document tags (such as #grading-rules), so that I don't need to learn new tools and can use familiar document formats.

3. As a teacher, I want to view assignment submission progress statistics, so that I can timely remind students who haven't submitted.

4. As a teacher, I want to receive low-confidence grading alerts, so that I can focus on assignments where AI is uncertain.

5. As a teacher, I want to view all appeal records and final decisions, so that I can understand student feedback and improve grading rubrics.

**助教用户故事：**

6. As a teaching assistant, I want to receive appeal notifications in a dedicated channel instead of being overwhelmed by QQ group messages.

7. As a teaching assistant, I want to approve or reject appeals with one click, simply by replying "approve" or "reject + reason".

8. As a teaching assistant, I want AI to automatically regrade and provide change explanations, so that I can quickly understand the adjustment rationale before approving.

**学生用户故事：**

9. As a student, I want to submit assignments via private message and receive immediate confirmation, without having to @ the bot in the group.

10. As a student, I want to receive grading results with deduction rationale, where each deduction cites the original course material, so that I know how to improve.

11. As a student, I want to appeal unreasonable grades by simply replying "appeal + reason", with a transparent process.

12. As a student, I want to optionally get detailed PDF grading reports containing complete scoring points.

13. As a student, I want to @ the bot in QQ groups to ask course-related questions and get answers based on uploaded course materials, not search engine results.

14. As a student, I want the bot to cite original course material when answering questions, so that I can refer back to the corresponding location in the course materials.

15. As a student, I want the bot to answer comprehensive questions by combining multiple course materials, not just single content.

---

## Core Features

**F1: Knowledge Base Management**

教师在群内发送文件加上 #course-materials 标签，自动解析并向量化入库。教师发送文件加上 #grading-rules 标签，自动解析为结构化 grading rules。教师发送文件加上 #assignment-requirements 标签，绑定本次作业。支持 Word、PDF、Markdown、纯文本格式。按 course_id 隔离，不同课程的数据完全独立。

**F2: Assignment Submission and Reception**

学生私聊机器人发送作业文件，避免群消息污染。学生发送文件后，机器人询问归属课程，学生回复序号确认。超时提交标记为 late，报告中注明；教师可配置迟交扣分规则。截止时间前允许覆盖提交，旧文件保留并标记为 superseded。

**F3: AI Grading Engine**

主观题批改结合作业要求、评分细则、课件参考资料，三层上下文叠加评分。客观题批改精确匹配加容错（同义词、大小写、单位写法变体）。每处扣分必须引用课件或评分细则的具体来源。high/medium/low 置信度标注，low 置信度自动推送给助教关注。批改结果包含总分、分项得分、得分点、扣分点、证据引用。

**F4: Grading Result Notification**

批改完成后立即发送文字摘要，包含总分、主要扣分原因。按需生成详细 PDF 报告，包含完整得分点、扣分点、课件依据、申诉历史。摘要中提示回复申诉加理由，进入申诉流程。

**F5: Appeal Process**

接收学生申诉理由、原始作业、原批改结果，AI 自动重新检索课件并评分。AI 输出分项变更及原因。申诉进入助教专属频道，包含原分、AI 重评分、申诉理由、变更说明。助教回复同意或驳回加原因，完成终裁。所有操作记录入库，教师可查、可统计。

**F6: Teacher Statistics Dashboard**

进度统计显示已提交、未提交、批改完成、待批改、申诉中。异常预警包括低置信度、疑似抄袭、未提交率高。可配置定时推送或按需查询。

**F7: Course Materials Q&A**

学生在 QQ 群内 @机器人提问，基于课件知识库回答。回答中引用课件原文，标注来源文件、章节、页码。跨多份课件检索并综合回答，支持第三章和第五章的区别是什么类问题。问答仅基于当前课程群对应的 course_id 知识库。支持学生私聊机器人提问。

---

## AI Capabilities

**A1: Document Intelligent Parsing**

多格式解析，Word、PDF、Markdown、纯文本统一转为纯文本。结构化提取，识别 Markdown 表格、编号列表，提取评分维度、满分、评分要点。LLM 兜底，结构化解析失败时用 LLM 将自然语言转为结构化数据。代码识别，ZIP 包内代码文件保留文件名，便于定位。

**A2: RAG Knowledge Retrieval**

向量语义检索，学生作业批改时检索 top-5 相关课件段落作为参考。检索结果附带来源文件名、章节、页码，直接作为扣分依据。课件可增量添加或更新，不丢失历史关联。course_id 作为 namespace，不同课程知识库完全隔离。

**A3: AI Grading and Confidence Assessment**

三层上下文叠加，作业要求加评分细则为最高权重，其次评分维度细则，再次课件参考。扣分溯源强制，每处扣分必须引用来源，无来源的扣分降低置信度。置信度自动评估，high 为依据充分、medium 为需推理、low 为依据薄弱，自动预警助教。

**A4: AI-Assisted Appeal Regrading**

上下文扩展，原始 grading_context 加上申诉理由、学生指出的原文位置。差异化输出，分项变更说明哪些分变了、为什么变。建议置信度，重评结果附带置信度供助教参考。

**A5: Course Materials Q&A**

语义检索，学生提问时从向量数据库检索最相关的课件段落 top-3 到 top-5。原文引用标注，回答中内嵌引用标注来源。跨多文档检索并整合，支持比较型、总结型提问。支持多轮追问，保留最近 3 轮上下文。兜底回答，当检索结果不足以回答时明确告知相关内容未在课件中找到。

---

## Implementation Decisions

**Module Design**

Qclaw 接入层负责 QQ 消息收发入口。意图识别路由负责解析意图、管理会话状态含问答上下文。文件解析引擎负责 Word、PDF、ZIP 解析为文本。评分细则解析负责文档到结构化评分配置。知识库管理负责课件向量化存储与检索 RAG。作业接收归档负责学生提交、归属确认、存储。AI 批改引擎负责核心主观题批改加置信度。结果通知负责私信摘要加申诉入口。助教频道负责申诉推送加指令解析。教师统计负责进度统计加预警。申诉处理负责状态机加 AI 重评。PDF 报告负责按需生成详细报告。课件问答负责学生提问到 RAG 检索到 AI 回答加引用。

**Technical Stack**

QQ接入使用 Qclaw。文件解析使用 python-docx、pdfplumber、zipfile。向量数据库使用 Chroma 开发版、Qdrant 生产版。关系数据库使用 SQLite 开发版、PostgreSQL 生产版。LLM 使用 DeepSeek 或 OpenAI。PDF 生成使用 WeasyPrint。

**Key Design Decisions**

部署模式为一机器人管多群，运维成本低，course_id 隔离数据。作业提交入口为私聊机器人，避免群消息污染便于确认归属。助教操作界面为助教专属 QQ 频道，无需额外系统 QQ 生态内闭环。申诉终态为 AI 重评加助教确认，AI 给建议人做终裁平衡效率与公平。批改结果呈现为文字摘要加按需 PDF，快速获取结论深度查阅按需。评分细则输入为群内发文档加标签触发，教师无需额外工具流程自然。

---

## Testing Decisions

**Test Principles**

核心功能必须有自动化测试覆盖。批改引擎输出格式必须符合 GradingResult 数据结构。知识库检索结果必须包含来源标注。

**Test Scope**

文件解析模块需要测试 Word、PDF、ZIP 三种格式。意图路由需要测试所有支持的 hashtag 标签识别。批改引擎需要测试置信度评估逻辑。申诉流程需要测试状态机转换完整性。

---

## Out of Scope

以下功能不在 v1.0 范围内：

图片作业支持，手写作业拍照识别 OCR 待后续版本。教师端 Web 后台，纯 QQ 内操作不提供独立管理界面。多语言课件支持，暂只支持中文课件。代码自动运行测试，代码批改只做静态分析不实际运行。实时语音视频交互。作业查重数据库，仅检测同一课程内的相似度不跨课程。

---

## Further Notes

**Deployment Architecture**

系统分为三层。接入层为 Qclaw 机器人实例，连接课程群 A、课程群 B、助教频道、学生私聊。核心处理层包含意图路由、文件解析、细则解析、知识库、作业归档、批改引擎、申诉处理、统计预警。存储层包含关系数据库 SQLite、向量数据库 Chroma、文件存储。

**Core Interaction Flows**

教师配置作业流程为发送文件加课件标签触发文件解析和向量入库，发送文件加评分细则标签触发细则解析入库版本化，发送文件加作业要求标签触发创建作业记录。学生提交作业流程为私聊发送文件后机器人询问归属课程，学生回复序号确认后归档并触发批改，批改完成后私信发送摘要。申诉审批流程为学生回复申诉加理由触发 AI 自动重评，重评结果推送助教频道，助教回复同意或驳回加原因后新分数写入并私信通知学生。课件智能问答流程为学生在 QQ 群 @机器人提问，意图识别后 RAG 检索相关课件段落，回答并引用原文位置后群内回复学生。助教授课 QQ 号为 3419307872，发送 C2C 私聊需要用户的 openid 而非 QQ 号，机器人必须先被用户添加才能收发消息。

---

*本文档由 AI 基于项目设计文档和模块规格自动生成，如有疑问请联系产品负责人。*
