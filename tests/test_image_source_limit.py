import io

import pytest
from fastapi import HTTPException
from PIL import Image
from quality.vision import canonical_image


def test_source_can_exceed_transport_limit_when_jpeg_normalization_fits():
    # 真实OFF 6413467524905原图4.57MB→JPEG90约0.99MB；此回归无需下载外部数据。
    stream = io.BytesIO()
    with Image.new("RGB", (1600, 1000), "white") as image:
        image.save(stream, format="PNG", compress_level=0)
    raw = stream.getvalue()
    assert 4 * 1024 * 1024 < len(raw) < 12 * 1024 * 1024
    normalized, metadata = canonical_image(raw)
    assert len(normalized) <= 4 * 1024 * 1024
    assert (metadata["width"], metadata["height"]) == (1600, 1000)


def test_source_upload_bound_is_still_enforced():
    with pytest.raises(HTTPException) as error:
        canonical_image(b"x" * (12 * 1024 * 1024 + 1))
    assert error.value.status_code == 413
