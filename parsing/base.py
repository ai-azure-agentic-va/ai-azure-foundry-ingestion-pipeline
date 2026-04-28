"""Base parser interface and result model."""

from dataclasses import dataclass, field
from abc import ABC, abstractmethod


@dataclass
class ParseResult:
    full_text: str
    pages: list[dict] = field(default_factory=list)
    page_count: int = 1
    metadata: dict = field(default_factory=dict)


class BaseParser(ABC):

    @abstractmethod
    def parse(self, file_bytes: bytes) -> ParseResult:
        ...

    @property
    @abstractmethod
    def supported_extensions(self) -> list[str]:
        ...
