"""扫描PDF整页渲染；PDFium全局互斥，所有坐标绑定渲染像素与指纹。"""
import hashlib
import io
import math
import threading

import pypdfium2 as pdfium

_render_lock = threading.Lock()
MAX_RENDER_PIXELS = 16_000_000
RENDER_DPI = 144


def render_pdf_page(data: bytes, page_number: int) -> tuple[bytes, dict]:
    with _render_lock:
        document = pdfium.PdfDocument(data)
        try:
            page = document[page_number - 1]
            try:
                width, height = page.get_size()
                scale = RENDER_DPI / 72
                if (not math.isfinite(width * height) or width <= 0 or height <= 0
                        or math.ceil(width * scale) * math.ceil(height * scale)
                        > MAX_RENDER_PIXELS):
                    raise ValueError("PDF页面渲染超过1600万像素或尺寸无效")
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil()
                    try:
                        output = io.BytesIO()
                        image.save(output, format="PNG")
                        rendered = output.getvalue()
                        return rendered, {"width": image.width, "height": image.height,
                                          "dpi": RENDER_DPI,
                                          "rendered_sha256": hashlib.sha256(rendered).hexdigest(),
                                          "renderer": "pypdfium2-4.30.0"}
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            document.close()
