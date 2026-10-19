import pytest
from quality.evidence_selection import selected_quote


def test_selected_evidence_preserves_newlines_and_repeated_text_positions():
    text = "品牌\n万利达\n品牌\n万利达\n"
    result = selected_quote(text, {"line_start": 3, "line_end": 4})
    assert result == {"quote": "品牌\n万利达", "char_start": 7, "char_end": 13}
    assert text[result["char_start"] : result["char_end"]] == result["quote"]


@pytest.mark.parametrize("start,end", [(0, 1), (2, 1), (1, 3)])
def test_invalid_selection_is_rejected(start, end):
    with pytest.raises(ValueError):
        selected_quote("品牌\n万利达", {"line_start": start, "line_end": end})


def test_quote_and_lines_cannot_be_mixed_to_smuggle_fabricated_evidence():
    with pytest.raises(ValueError):
        selected_quote("万利达", {"line_start": 1, "line_end": 1, "quote": "伪造"})
