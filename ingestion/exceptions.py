"""Custom exception hierarchy for the ingestion pipeline."""


class IngestionError(Exception):
    """Base exception for all ingestion pipeline errors."""
    pass


class ParseError(IngestionError):
    """Raised when document parsing fails."""
    pass


class ChunkError(IngestionError):
    """Raised when text chunking fails."""
    pass


class EmbeddingError(IngestionError):
    """Raised when embedding generation fails."""
    pass


class SearchPushError(IngestionError):
    """Raised when pushing to Azure AI Search fails."""
    pass


class PIIScanError(IngestionError):
    """Raised when PII scanning/redaction fails."""
    pass
