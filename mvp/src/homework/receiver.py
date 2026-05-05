"""
M06 — 作业接收与归档
MVP 阶段用函数调用模拟 QQ 消息交互，不做真实 Qclaw 接入。
"""

import uuid
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from contracts.models import Submission
from storage import db
from parser.file_parser import parse as parse_file


# ── 文件存储根目录 ─────────────────────────────────────────

DATA_DIR = Path(__file__).parent.parent / "data" / "submissions"


def receive_file(student_qq: str, file_path: str, course_id: str,
                 assignment_id: str) -> Submission:
    """
    接收学生提交的作业文件。
    1. 生成 submission_id
    2. 复制文件到归档目录
    3. 检查是否迟交
    4. 保存到数据库
    """
    submission_id = f"sub_{uuid.uuid4().hex[:8]}"
    ext = Path(file_path).suffix.lower()

    # 创建归档目录
    archive_dir = DATA_DIR / submission_id
    archive_dir.mkdir(parents=True, exist_ok=True)

    # 复制原始文件
    dest = archive_dir / f"original{ext}"
    shutil.copy2(file_path, dest)

    # 检查是否迟交
    rubric = db.get_latest_rubric(assignment_id)
    is_late = False
    if rubric and rubric.deadline and datetime.now() > rubric.deadline:
        is_late = True

    # 文件类型
    file_type_map = {
        ".docx": "docx", ".pdf": "pdf", ".zip": "zip",
        ".py": "code", ".java": "code", ".cpp": "code", ".c": "code",
        ".js": "code", ".ts": "code", ".go": "code",
    }
    file_type = file_type_map.get(ext, "unknown")

    sub = Submission(
        id=submission_id,
        student_qq=student_qq,
        course_id=course_id,
        assignment_id=assignment_id,
        file_path=str(dest),
        file_type=file_type,
        submitted_at=datetime.now(),
        is_late=is_late,
        status="submitted",
        score=None,
    )

    db.save_submission(sub)
    return sub


def get_submission_text(submission_id: str) -> str:
    """获取提交作业的解析文本。"""
    sub = db.get_submission(submission_id)
    if not sub:
        raise ValueError(f"提交记录不存在：{submission_id}")

    parsed = parse_file(sub.file_path)
    return parsed.text
