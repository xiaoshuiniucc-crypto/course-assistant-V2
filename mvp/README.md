# QQ课程AI助手 — MVP

> 6小时可运行的 MVP 原型，跑通核心链路。

## 已实现模块

| 模块 | 文件 | 状态 |
|------|------|------|
| M13 数据存储 | `src/storage/db.py` | ✅ SQLite 建表 + CRUD |
| M03 文件解析 | `src/parser/file_parser.py` | ✅ docx/pdf/zip/code |
| M04 细则解析 | `src/rubric/rubric_parser.py` | ✅ Markdown表格 + LLM兜底 |
| M07 批改引擎 | `src/grading/engine.py` | ✅ 逐维度批改 + 置信度 |
| M06 作业接收 | `src/homework/receiver.py` | ✅ 文件归档 + 迟交标记 |
| M08 结果通知 | `src/notify/result_notifier.py` | ✅ 文字摘要格式化 |
| M11 申诉处理 | `src/appeal/handler.py` | ✅ 状态机 + AI重评 + 交互式助教审核 |
| 数据模型 | `src/contracts/models.py` | ✅ 共享 dataclass |

## 快速开始

```bash
cd D:\QQ课程AI助手\mvp\src

# 1. 安装依赖
pip install -r ../requirements.txt

# 2. 设置 API Key
set OPENAI_API_KEY=sk-xxx
# 或使用兼容 API：
set OPENAI_BASE_URL=https://your-api-endpoint

# 3. 运行端到端演示
python main.py
```

## 端到端流程

```
上传课件 → 设置评分细则 → 学生提交作业 → AI批改 → 查看结果 → 申诉重评
```

## 项目结构

```
mvp/
├── requirements.txt
├── src/
│   ├── main.py              # 端到端演示入口
│   ├── contracts/
│   │   └── models.py        # 共享数据模型
│   ├── storage/
│   │   └── db.py            # M13 SQLite 存储层
│   ├── parser/
│   │   └── file_parser.py   # M03 文件解析
│   ├── rubric/
│   │   └── rubric_parser.py # M04 细则解析
│   ├── grading/
│   │   └── engine.py        # M07 AI批改引擎
│   ├── homework/
│   │   └── receiver.py      # M06 作业接收
│   ├── notify/
│   │   └── result_notifier.py # M08 结果通知
│   └── appeal/
│       └── handler.py       # M11 申诉处理
└── data/                    # 运行时数据（自动创建）
```

## 待完成（Phase 2）

- [ ] 真实 Qclaw 接入（M01）
- [ ] 向量库 RAG 检索（M05，当前用文本匹配）
- [ ] 意图识别路由（M02）
- [ ] 助教频道管理（M09，当前用控制台）
- [ ] PDF 报告生成（M12）
- [ ] 教师统计预警（M10）
- [ ] 多课程/多群隔离
