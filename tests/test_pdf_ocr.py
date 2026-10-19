import io
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter
from quality import parsing


def scanned_pdf(pages=1):
    with Image.open("data/demo/materials/demo-label.png") as source:
        image = source.convert("RGB")
        try:
            output = io.BytesIO()
            image.save(output, format="PDF", resolution=144, save_all=True,
                       append_images=[image] * (pages - 1))
            return output.getvalue()
        finally:
            image.close()


def test_image_only_pdf_uses_real_full_page_ocr_and_source_coordinates():
    data = scanned_pdf()
    assert not PdfReader(io.BytesIO(data)).pages[0].extract_text()
    result = parsing.parse_material(data, ".pdf")
    assert result["status"] == "parsed"
    assert "pypdfium2" in result["engine"]
    model = next(field for field in result["fields"] if field["name"] == "model")
    assert model["value"] == "K170"
    loc = model["location"]
    assert loc["kind"] == "pdf" and loc["page"] == 1 and loc["method"] == "ocr"
    assert loc["coordinate_system"] == "rendered_pixels"
    assert len(loc["rendered_sha256"]) == 64
    assert all(0 <= x <= loc["width"] and 0 <= y <= loc["height"] for x, y in loc["box"])
    assert Path("data/demo/materials/demo-label.png").is_file()


def test_partial_pdf_failure_preserves_successful_page_and_requires_review(monkeypatch):
    calls = 0

    def recognize(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("engine failure")
        return [([[0, 0], [100, 0], [100, 20], [0, 20]], "MODEL: K170", 0.9)], None

    monkeypatch.setattr(parsing, "ocr_engine", lambda: recognize)
    result = parsing.parse_material(scanned_pdf(2), ".pdf")
    assert result["status"] == "needs_info"
    assert result["fields"][0]["value"] == "K170"
    assert [page["status"] for page in result["pdf_pages"]] == ["parsed", "failed"]
    assert "第2页" in result["warning"]


def test_blank_and_oversized_scan_pages_do_not_claim_success(monkeypatch):
    monkeypatch.setattr(parsing, "ocr_engine", lambda: lambda _: (None, None))
    empty = parsing.parse_material(scanned_pdf(), ".pdf")
    assert empty["status"] == "needs_info" and not empty["units"]
    writer = PdfWriter()
    writer.add_blank_page(width=100000, height=100000)
    output = io.BytesIO()
    writer.write(output)
    enormous = parsing.parse_material(output.getvalue(), ".pdf")
    assert enormous["status"] == "needs_info"
    assert enormous["pdf_pages"][0]["status"] == "failed"


def test_scan_upload_roundtrip_uses_normal_material_permission_and_revision(client):
    from conftest import headers

    owner = headers(client)
    case = client.post("/api/cases", headers=owner,
                       json={"title": "扫描原件", "workflow": "qualification"}).json()
    data = scanned_pdf()
    path = f"/api/cases/{case['id']}/materials"
    response = client.post(path, headers=owner,
                           files={"file": ("scan.pdf", data, "application/pdf")})
    assert response.status_code == 201
    body = response.json()
    assert body["case_revision"] == 2 and body["status"] == "parsed"
    assert body["parsed"]["pdf_pages"][0]["method"] == "ocr"
    assert client.get(path, headers=headers(client, "merchant-b")).status_code == 404
    original = client.get(path + f"/{body['id']}/download", headers=owner)
    assert original.content == data
