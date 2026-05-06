"""
评分标准解析器
支持自然语言描述 → 结构化 Rubric
"""
from __future__ import annotations
import re
from typing import List, Optional
from contracts.models import Rubric, RubricDimension


class RubricParser:
    """
    解析教师输入的评分标准文本

    支持格式:
    1. 结构化文本格式:
       维度名:权重:描述
       内容准确性:0.4:答案是否正确
       逻辑性:0.3:推理是否合理
       表达:0.3:语言是否清晰
       硬扣分:迟到扣10分

    2. 自然语言格式（简单解析）
    """

    def parse(self, text: str, title: str = "默认评分标准") -> Rubric:
        """解析评分标准文本"""
        dimensions: List[RubricDimension] = []
        hard_rules: List[str] = []
        normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized_text.strip().split("\n")

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # 尝试结构化解析: 名称:权重:描述
            if ":" in line or "：" in line:
                parts = re.split(r"[:：]", line, maxsplit=2)
                name = parts[0].strip()

                # 检查是否是硬扣分规则
                if name in ("硬扣分", "硬规则", "扣分规则", "hard_rule"):
                    # 后续部分为规则描述
                    rule_text = parts[1].strip() if len(parts) > 1 else ""
                    if rule_text:
                        hard_rules.append(rule_text)
                    continue

                # 尝试解析权重
                weight = 0.25  # 默认权重
                if len(parts) > 1:
                    try:
                        weight = float(parts[1].strip().rstrip("%"))
                        if weight > 1:   # 百分比 → 比例
                            weight /= 100
                    except ValueError:
                        pass

                desc = parts[2].strip() if len(parts) > 2 else ""
                dimensions.append(RubricDimension(
                    name=name, weight=weight, description=desc
                ))

            else:
                # 自然语言行 → 作为一个维度
                dimensions.append(RubricDimension(
                    name=line, weight=1.0 / max(len(lines), 1),
                    description=""
                ))

        # 如果没有解析到维度，使用默认
        if not dimensions:
            dimensions = [
                RubricDimension(name="内容准确性", weight=0.4),
                RubricDimension(name="逻辑性", weight=0.3),
                RubricDimension(name="表达", weight=0.3),
            ]

        # 归一化权重
        total_w = sum(d.weight for d in dimensions)
        if total_w > 0 and abs(total_w - 1.0) > 0.01:
            for d in dimensions:
                d.weight = round(d.weight / total_w, 4)

        import uuid
        return Rubric(
            id=f"rubric_{uuid.uuid4().hex[:8]}",
            title=title,
            dimensions=dimensions,
            hard_rules=hard_rules,
        )

    def format_rubric(self, rubric: Rubric) -> str:
        """将评分标准格式化为可读文本"""
        lines = [f"📋 评分标准: {rubric.title}\n"]
        for d in rubric.dimensions:
            lines.append(
                f"  • {d.name}: 权重 {d.weight:.0%}"
                + (f" — {d.description}" if d.description else "")
            )
        if rubric.hard_rules:
            lines.append("\n硬扣分规则:")
            for r in rubric.hard_rules:
                lines.append(f"  ⚠️ {r}")
        lines.append(f"\n总分: {rubric.total_score}")
        return "\n".join(lines)
