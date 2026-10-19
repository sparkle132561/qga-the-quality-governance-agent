"""模型只选原文行号；程序生成精确引文和字符位置，不做模糊修复。"""

from pydantic import BaseModel, ConfigDict, Field


class LineSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


def numbered_text(text: str):
    return "\n".join(f"{i}: {line}" for i, line in enumerate(text.splitlines(), 1))


def selected_quote(text: str, selection: dict):
    reference = LineSelection.model_validate(selection)
    lines = text.splitlines(keepends=True)
    if not 1 <= reference.line_start <= reference.line_end <= len(lines):
        raise ValueError("证据行号超出原文或倒置")
    start = sum(len(line) for line in lines[: reference.line_start - 1])
    end = sum(len(line) for line in lines[: reference.line_end])
    # 只裁剪所选边界的空白，不改变内部字符；位置仍对应传入原文。
    segment = text[start:end]
    start += len(segment) - len(segment.lstrip())
    end -= len(segment) - len(segment.rstrip())
    quote = text[start:end]
    if not quote or len(quote) > 300:
        raise ValueError("所选证据为空或超过300字符")
    return {"quote": quote, "char_start": start, "char_end": end}


def resolve_quote(text: str, value: dict):
    if "line_start" in value or "line_end" in value:
        return selected_quote(text, value)
    # 历史客户端的精确quote仍由原有校验检查，绝不按近似文本自动对齐。
    return value
