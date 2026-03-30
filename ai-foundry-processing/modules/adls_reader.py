"""ADLS Gen2 / Blob Storage reader using azure-storage-blob SDK.
Reads documents from ADLS and manages failed/state containers."""

import json
import logging
import os
from datetime import datetime, timezone

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)


class AdlsReader:
    """Read files from ADLS Gen2 and manage processing state."""

    def __init__(
        self,
        account_name: str | None = None,
        container_raw: str | None = None,
        container_failed: str | None = None,
        container_state: str | None = None,
    ):
        self.account_name = account_name or os.environ.get("ADLS_ACCOUNT_NAME")
        self.container_raw = container_raw or os.environ.get(
            "ADLS_CONTAINER_RAW", "raw-documents"
        )
        self.container_failed = container_failed or os.environ.get(
            "ADLS_CONTAINER_FAILED", "raw-documents-failed"
        )
        self.container_state = container_state or os.environ.get(
            "ADLS_CONTAINER_STATE", "processing-state"
        )

        account_url = f"https://{self.account_name}.blob.core.windows.net"
        credential = DefaultAzureCredential()
        self.blob_service = BlobServiceClient(
            account_url=account_url, credential=credential
        )

        logger.info(f"[AdlsReader] Initialized for account={self.account_name}")

    def read_blob(self, container: str, blob_path: str) -> bytes:
        """Read a blob as bytes."""
        blob_client = self.blob_service.get_blob_client(
            container=container, blob=blob_path
        )
        data = blob_client.download_blob().readall()
        logger.debug(
            f"[AdlsReader] Read {len(data)} bytes from {container}/{blob_path}"
        )
        return data

    def read_metadata_sidecar(self, container: str, blob_path: str) -> dict:
        """Read the .metadata.json sidecar for a document.

        Returns empty dict if sidecar doesn't exist (normal — not all docs have one).
        Logs a warning on unexpected errors (auth, network) instead of silently swallowing.
        """
        from azure.core.exceptions import ResourceNotFoundError

        metadata_path = f"{blob_path}.metadata.json"
        try:
            data = self.read_blob(container, metadata_path)
            return json.loads(data)
        except ResourceNotFoundError:
            logger.debug(f"[AdlsReader] No metadata sidecar for {blob_path}")
            return {}
        except Exception as e:
            logger.warning(f"[AdlsReader] Error reading metadata sidecar for {blob_path}: {e}")
            return {}

    def move_to_failed(self, blob_path: str, error_message: str):
        """Copy a document to the failed container with error info."""
        try:
            source_client = self.blob_service.get_blob_client(
                container=self.container_raw, blob=blob_path
            )
            dest_client = self.blob_service.get_blob_client(
                container=self.container_failed, blob=blob_path
            )

            # Copy blob
            dest_client.start_copy_from_url(source_client.url)

            # Write error info
            error_blob = self.blob_service.get_blob_client(
                container=self.container_failed,
                blob=f"{blob_path}.error.json",
            )
            error_blob.upload_blob(
                json.dumps(
                    {
                        "error": error_message,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ),
                overwrite=True,
            )
            logger.info(f"[AdlsReader] Moved {blob_path} to failed container")
        except Exception as e:
            logger.error(f"[AdlsReader] Failed to move {blob_path} to failed: {e}")

    def save_state(self, source_key: str, state: dict):
        """Save processing state/watermark for a source."""
        blob_client = self.blob_service.get_blob_client(
            container=self.container_state,
            blob=f"{source_key}/watermark.json",
        )
        blob_client.upload_blob(json.dumps(state), overwrite=True)
        logger.debug(f"[AdlsReader] Saved state for {source_key}")

    def load_state(self, source_key: str) -> dict:
        """Load processing state/watermark for a source.

        Returns empty dict if no prior state exists.
        """
        from azure.core.exceptions import ResourceNotFoundError

        try:
            data = self.read_blob(self.container_state, f"{source_key}/watermark.json")
            return json.loads(data)
        except ResourceNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"[AdlsReader] Error loading state for {source_key}: {e}")
            return {}

    def list_blobs(self, container: str, prefix: str = "") -> list[str]:
        """List blob paths in a container with optional prefix."""
        container_client = self.blob_service.get_container_client(container)
        blobs = container_client.list_blobs(name_starts_with=prefix)
        return [b.name for b in blobs if not b.name.endswith(".metadata.json")]
