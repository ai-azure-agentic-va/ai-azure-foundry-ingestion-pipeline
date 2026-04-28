"""ADLS Gen2 / Blob Storage reader."""

import json
import logging
from datetime import datetime, timezone

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)


class AdlsReader:

    def __init__(
        self,
        account_name: str | None = None,
        container_raw: str | None = None,
        container_failed: str | None = None,
    ):
        from .config import settings

        self.account_name = account_name or settings.ADLS_ACCOUNT_NAME
        self.container_raw = container_raw or settings.ADLS_CONTAINER_RAW
        self.container_failed = container_failed or settings.ADLS_CONTAINER_FAILED

        account_url = f"https://{self.account_name}.blob.core.windows.net"
        credential = DefaultAzureCredential()
        self.blob_service = BlobServiceClient(
            account_url=account_url, credential=credential
        )

        logger.info(f"[AdlsReader] Initialized for account={self.account_name}")

    def read_blob(self, container: str, blob_path: str) -> bytes:
        from .config import settings
        max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024

        blob_client = self.blob_service.get_blob_client(container=container, blob=blob_path)
        props = blob_client.get_blob_properties()
        if props.size and props.size > max_bytes:
            raise ValueError(
                f"File too large: {blob_path} is {props.size / 1024 / 1024:.0f}MB "
                f"(limit: {settings.MAX_FILE_SIZE_MB}MB)"
            )

        data = blob_client.download_blob().readall()
        logger.debug(f"[AdlsReader] Read {len(data)} bytes from {container}/{blob_path}")
        return data

    def read_blob_metadata(self, container: str, blob_path: str) -> dict:
        try:
            blob_client = self.blob_service.get_blob_client(container=container, blob=blob_path)
            props = blob_client.get_blob_properties()
            metadata = dict(props.metadata) if props.metadata else {}

            if props.last_modified:
                metadata["last_modified"] = props.last_modified.strftime("%Y-%m-%dT%H:%M:%SZ")
            if props.content_settings and props.content_settings.content_type:
                metadata.setdefault("content_type", props.content_settings.content_type)

            if metadata:
                logger.info(f"[AdlsReader] Blob metadata for {blob_path}: {list(metadata.keys())}")
            return metadata
        except Exception as e:
            logger.warning(f"[AdlsReader] Error reading blob metadata for {blob_path}: {e}")
            return {}

    def move_to_failed(self, blob_path: str, error_message: str):
        try:
            source_client = self.blob_service.get_blob_client(
                container=self.container_raw, blob=blob_path
            )
            dest_client = self.blob_service.get_blob_client(
                container=self.container_failed, blob=blob_path
            )
            dest_client.start_copy_from_url(source_client.url)

            error_blob = self.blob_service.get_blob_client(
                container=self.container_failed,
                blob=f"{blob_path}.error.json",
            )
            error_blob.upload_blob(
                json.dumps({
                    "error": error_message,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }),
                overwrite=True,
            )
            logger.info(f"[AdlsReader] Moved {blob_path} to failed container")
        except Exception as e:
            logger.error(f"[AdlsReader] Failed to move {blob_path} to failed: {e}")
