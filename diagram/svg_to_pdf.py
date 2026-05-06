#!/usr/bin/env python3
"""使用svglib将SVG流程图转换为PDF - 正确方法"""

import os
from svglib.svglib import svg2rlg
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.graphics import renderPDF
from io import BytesIO

# 定义路径
svg_path = r"D:\QQ课程AI助手\diagram\qq-course-assistant-flowchart.svg"
pdf_path = r"D:\QQ课程AI助手\diagram\qq-course-assistant-flowchart.pdf"

print(f"正在读取SVG: {svg_path}")

# 读取SVG
drawing = svg2rlg(svg_path)

if drawing is None:
    print("❌ SVG读取失败")
    exit(1)

print(f"SVG尺寸: {drawing.width:.0f} x {drawing.height:.0f}")

# 创建PDF，使用自定义页面尺寸
page_width = drawing.width
page_height = drawing.height

c = canvas.Canvas(pdf_path, pagesize=(page_width, page_height))

# 使用renderPDF将SVG drawing绘制到PDF
renderPDF.draw(drawing, c, 0, 0)

c.save()

# 验证
if os.path.exists(pdf_path):
    size_kb = os.path.getsize(pdf_path) / 1024
    print(f"✅ PDF生成成功!")
    print(f"   路径: {pdf_path}")
    print(f"   大小: {size_kb:.1f} KB")
    print(f"   尺寸: {page_width:.0f} x {page_height:.0f} px (适合打印)")
else:
    print("❌ PDF生成失败")
