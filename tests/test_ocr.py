from pathlib import Path

from quality.parsing import parse_material


def test_real_cpu_ocr_preserves_model_and_box():
    image = Path("data/demo/materials/demo-label.png").read_bytes()
    result = parse_material(image, ".png")
    assert result["engine"] == "rapidocr-onnxruntime-1.4.4"
    assert result["status"] == "parsed"
    model = next(field for field in result["fields"] if field["name"] == "model")
    assert model["value"] == "K170"
    assert model["location"]["kind"] == "image"
    assert len(model["location"]["box"]) == 4
    assert 0 < model["confidence"] <= 1
