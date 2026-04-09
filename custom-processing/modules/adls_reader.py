"""ADLS Gen2 / Blob Storage reader using azure-storage-blob SDK.
Reads documents from ADLS and manages failed containers."""

import json
import logging
import os
from datetime import datetime, timezone

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)


class AdlsReader:
    """Read files from ADLS Gen2 and manage failed documents."""

    def __init__(
        self,
        account_name: str | None = None,
        container_raw: str | None = None,
        container_failed: str | None = None,
    ):
        self.account_name = account_name or os.environ.get("ADLS_ACCOUNT_NAME")
        self.container_raw = container_raw or os.environ.get("ADLS_CONTAINER_RAW", "raw-documents")
        self.container_failed = container_failed or os.environ.get("ADLS_CONTAINER_FAILED", "raw-documents-failed")

        account_url = f"https://{self.account_name}.blob.core.windows.net"
        credential = DefaultAzureCredential()
        self.blob_service = BlobServiceClient(account_url=account_url, credential=credential)

        logger.info(f"[AdlsReader] Initialized for account={self.account_name}")

    def read_blob(self, container: str, blob_path: str) -> bytes:
        """Read a blob as bytes."""
        blob_client = self.blob_service.get_blob_client(container=container, blob=blob_path)
        data = blob_client.download_blob().readall()
        logger.debug(f"[AdlsReader] Read {len(data)} bytes from {container}/{blob_path}")
        return data

    def read_blob_metadata(self, container: str, blob_path: str) -> dict:
        """Read the blob's native Azure metadata properties (key-value pairs set on the blob itself)."""
        try:
            blob_client = self.blob_service.get_blob_client(container=container, blob=blob_path)
            props = blob_client.get_blob_properties()
            return dict(props.metadata) if props.metadata else {}
        except Exception as e:
            logger.debug(f"[AdlsReader] Could not read blob metadata for {blob_path}: {e}")
            return {}

    def read_metadata_sidecar(self, container: str, blob_path: str) -> dict:
        """Read the .metadata.json sidecar for a document."""
        metadata_path = f"{blob_path}.metadata.json"
        try:
            data = self.read_blob(container, metadata_path)
            return json.loads(data)
        except Exception as e:
            logger.debug(f"[AdlsReader] No metadata sidecar for {blob_path}: {e}")
            return {}

    def move_to_failed(self, blob_path: str, error_message: str):
        """Copy a document to the failed container with error info."""
        try:
            source_client = self.blob_service.get_blob_client(container=self.container_raw, blob=blob_path)
            dest_client = self.blob_service.get_blob_client(container=self.container_failed, blob=blob_path)

            # Copy blob
            dest_client.start_copy_from_url(source_client.url)

            # Write error info
            error_blob = self.blob_service.get_blob_client(
                container=self.container_failed,
                blob=f"{blob_path}.error.json",
            )
            error_blob.upload_blob(
                json.dumps({"error": error_message, "timestamp": datetime.now(timezone.utc).isoformat()}),
                overwrite=True,
            )
            logger.info(f"[AdlsReader] Moved {blob_path} to failed container")
        except Exception as e:
            logger.error(f"[AdlsReader] Failed to move {blob_path} to failed: {e}")

