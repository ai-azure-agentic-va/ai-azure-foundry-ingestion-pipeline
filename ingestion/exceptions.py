"""Custom exception hierarchy for the ingestion pipeline."""


class IngestionError(Exception):
    pass


class ParseError(IngestionError):
    pass


class ChunkError(IngestionError):
    pass


class EmbeddingError(IngestionError):
    pass


class SearchPushError(IngestionError):
    pass


class PIIScanError(IngestionError):
    pass
