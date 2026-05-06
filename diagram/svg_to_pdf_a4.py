#!/usr/bin/env python3
"""生成适合A4打印的PDF版本"""

import os
from svglib.svglib import svg2rlg
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.graphics import renderPDF

# 定义路径
svg_path = r"D:\QQ课程AI助手\diagram\qq-course-assistant-flowchart.svg"
pdf_path_a4 = r"D:\QQ课程AI助手\diagram\qq-course-assistant-flowchart-A4.pdf"

print("正在读取SVG...")
drawing = svg2rlg(svg_path)

if drawing is None:
    print("❌ SVG读取失败")
    exit(1)

# SVG原始尺寸
svg_width = drawing.width
svg_height = drawing.height
print(f"SVG尺寸: {svg_width:.0f} x {svg_height:.0f}")

# A4纸张尺寸 (mm转points, 1mm = 2.834645669 points)
a4_width_pt = A4[0]   # 595.275590551
a4_height_pt = A4[1]  # 841.88976378

# 计算缩放比例（保持宽高比）
scale_x = a4_width_pt / svg_width
scale_y = a4_height_pt / svg_height
scale = min(scale_x, scale_y) * 0.95  # 留5%边距

print(f"A4尺寸: {a4_width_pt:.0f} x {a4_height_pt:.0f} pt")
print(f"缩放比例: {scale:.2%}")

# 缩放drawing
drawing.width = svg_width * scale
drawing.height = svg_height * scale
drawing.scale(scale, scale)

# 创建A4 PDF
c = canvas.Canvas(pdf_path_a4, pagesize=A4)

# 将drawing居中绘制
x_offset = (a4_width_pt - drawing.width) / 2
y_offset = (a4_height_pt - drawing.height) / 2

renderPDF.draw(drawing, c, x_offset, y_offset)
c.save()

# 验证
if os.path.exists(pdf_path_a4):
    size_kb = os.path.getsize(pdf_path_a4) / 1024
    print(f"\n✅ A4版本PDF生成成功!")
    print(f"   路径: {pdf_path_a4}")
    print(f"   大小: {size_kb:.1f} KB")
    print(f"   页面: A4横向 (210 x 297 mm)")
else:
    print("❌ PDF生成失败")
