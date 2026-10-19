"""生成项目自建模拟材料，不冒用真实企业或证书。"""

import io
from pathlib import Path

from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

root = Path(__file__).resolve().parents[1]
target = root / "data" / "demo" / "materials"
target.mkdir(parents=True, exist_ok=True)

image = Image.new("RGB", (1200, 650), "white")
draw = ImageDraw.Draw(image)
font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 48)
for index, line in enumerate(
    [
        "DEMO ONLY - NOT A REAL PRODUCT",
        "MODEL: K170",
        "POWER: 1800W",
        "DATE: 2025-03-01",
        "CERTIFICATE: DEMO-001",
    ]
):
    draw.text((45, 45 + index * 110), line, fill="black", font=font)
image.save(target / "demo-label.png")

book = Workbook()
book.active.title = "模拟检测资料"
for row in [
    ("字段", "值"),
    ("型号", "K170"),
    ("功率", "1800W"),
    ("证书编号", "DEMO-001"),
    ("声明", "仅用于演示，不代表真实检测结果"),
]:
    book.active.append(row)
book.save(target / "demo-report.xlsx")
(target / "demo-products.csv").write_text(
    "型号,功率,声明\nK170,1800W,仅用于演示\n", encoding="utf-8-sig"
)

writer = PdfWriter()
page = writer.add_blank_page(width=600, height=800)
page[NameObject("/Resources")] = DictionaryObject(
    {
        NameObject("/Font"): DictionaryObject(
            {
                NameObject("/F1"): DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Font"),
                        NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject("/Helvetica"),
                    }
                )
            }
        )
    }
)
content = DecodedStreamObject()
content.set_data(
    b"BT /F1 22 Tf 40 730 Td (DEMO ONLY - NOT A REAL CERTIFICATE) Tj "
    b"0 -65 Td (MODEL: K170) Tj 0 -65 Td (POWER: 1800W) Tj "
    b"0 -65 Td (CERTIFICATE: DEMO-001) Tj ET"
)
page[NameObject("/Contents")] = writer._add_object(content)
output = io.BytesIO()
writer.write(output)
(target / "demo-certificate.pdf").write_bytes(output.getvalue())
print("Created 4 clearly labelled simulated materials")
