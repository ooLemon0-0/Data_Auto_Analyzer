from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Decision = Literal["correct", "incorrect", "invalid"]


@dataclass(slots=True)
class SourceItem:
    source_key: str
    recognition_text: str
    image_path: Path | None = None
    image_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
