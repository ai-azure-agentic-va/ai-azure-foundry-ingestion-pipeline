"""Base parser interface and result model."""

from dataclasses import dataclass, field
from abc import ABC, abstractmethod


@dataclass
class ParseResult:
    """Result of parsing a document."""
    full_text: str
    pages: list[dict] = field(default_factory=list)
    page_count: int = 1
    metadata: dict = field(default_factory=dict)


class BaseParser(ABC):
    """Abstract base for all document parsers."""

    @abstractmethod
    def parse(self, file_bytes: bytes) -> ParseResult:
        """Parse document bytes and return extracted text."""
        ...

    @property
    @abstractmethod
    def supported_extensions(self) -> list[str]:
        """Return list of supported file extensions (e.g., ['.pdf'])."""
        ...
