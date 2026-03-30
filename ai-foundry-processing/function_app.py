"""Azure Functions entry point for ai-foundry-processing.

AI Foundry Services pipeline: Content Understanding, Azure Language PII.
Active when DOC_PROCESSING=AI_FOUNDRY_SERVICES (or this Function App is deployed).

Triggers (5 total, all configurable via env vars):
  1. process_new_document:       Event Grid (BlobCreated on ADLS) - direct mode
  2. process_queue_document:     Queue trigger (Event Grid → Queue → Function) - queue mode
  3. process_blob_document:      Blob trigger (direct blob storage polling) - blob mode
  4. health_check:               HTTP GET - health/readiness
  5. ensure_index:               HTTP POST - create search index if missing

Trigger mode is controlled by TRIGGER_MODE env var:
  - EVENTGRID_DIRECT:  Event Grid fires directly to process_new_document (queue/blob disabled)
  - EVENTGRID_QUEUE:   Event Grid → Queue → process_queue_document (direct/blob disabled)
  - BLOB:              Blob trigger polls storage directly (no Event Grid needed)

Configurable env vars for triggers:
  - QUEUE_NAME:                  Queue name (default: doc-processing-queue)
  - AzureWebJobs.process_new_document.Disabled:     true/false (enable/disable Event Grid trigger)
  - AzureWebJobs.process_queue_document.Disabled:    true/false (enable/disable Queue trigger)
  - AzureWebJobs.process_blob_document.Disabled:     true/false (enable/disable Blob trigger)
"""

import json
import logging
import os
import threading

import azure.functions as func

app = func.FunctionApp()

logger = logging.getLogger(__name__)
logging.basicConfig(level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO")))

# Lazy-loaded pipeline with thread-safe double-checked locking
_pipeline = None
_pipeline_lock = threading.Lock()


def _get_pipeline():
    """Get the FoundryDocPipeline instance (thread-safe)."""
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                from modules.pipeline import FoundryDocPipeline

                _pipeline = FoundryDocPipeline()
                logger.info(f"[Functions] Pipeline initialized: {_pipeline.PIPELINE_NAME}")
    return _pipeline


# ---------------------------------------------------------------------------
# Helper: Extract blob info from event payload (used by both triggers)
# ---------------------------------------------------------------------------
def _extract_blob_info(data: dict) -> tuple[str, str, str, int] | None:
    """Extract (container, blob_path, content_type, content_length) from event data.
    Returns None if the event should be skipped."""
    blob_url = data.get("url", "")
    content_type = data.get("contentType", "")
    content_length = data.get("contentLength", 0)

    container = os.environ.get("ADLS_CONTAINER_RAW", "raw-documents")
    try:
        path_start = blob_url.index(f"/{container}/") + len(f"/{container}/")
        blob_path = blob_url[path_start:]
    except ValueError:
        logger.error(f"[Trigger] Cannot parse blob path from URL: {blob_url}")
        return None

    # Skip metadata sidecar files
    if blob_path.endswith(".metadata.json") or blob_path.endswith(".error.json"):
        logger.debug(f"[Trigger] Skipping metadata file: {blob_path}")
        return None

    # Skip zero-byte files
    if content_length == 0:
        logger.debug(f"[Trigger] Skipping zero-byte file: {blob_path}")
        return None

    return container, blob_path, content_type, content_length


# ---------------------------------------------------------------------------
# 1. Event Grid Trigger: process new documents as soon as they land in ADLS
#    Active when TRIGGER_MODE=EVENTGRID_DIRECT.
#    Disabled via AzureWebJobs.process_new_document.Disabled=true when inactive.
# ---------------------------------------------------------------------------
@app.function_name("process_new_document")
@app.event_grid_trigger(arg_name="event")
def process_new_document(event: func.EventGridEvent):
    """Triggered by BlobCreated on ADLS raw-documents container.
    Uses FoundryDocPipeline (Content Understanding, Azure Language PII)."""

    logger.info(
        f"[EventGrid] Received event: {event.event_type}, subject: {event.subject}"
    )

    data = event.get_json()
    info = _extract_blob_info(data)
    if info is None:
        return

    container, blob_path, content_type, content_length = info

    logger.info(
        f"[EventGrid] Processing: {blob_path} ({content_length} bytes, {content_type})"
    )

    metadata = {
        "source_url": data.get("url", ""),
        "content_type": content_type,
        "file_size_bytes": content_length,
    }

    pipeline = _get_pipeline()
    result = pipeline.process_document(container, blob_path, metadata)

    logger.info(f"[EventGrid] Result: {json.dumps(result)}")


# ---------------------------------------------------------------------------
# 2. Queue Trigger: process documents via Event Grid → Queue → Function
#    Active when TRIGGER_MODE=EVENTGRID_QUEUE.
#    Disabled via AzureWebJobs.process_queue_document.Disabled=true when inactive.
# ---------------------------------------------------------------------------
@app.function_name("process_queue_document")
@app.queue_trigger(
    arg_name="msg",
    queue_name="%QUEUE_NAME%",
    connection="ADLS_QUEUE_CONNECTION",
)
def process_queue_document(msg: func.QueueMessage):
    """Triggered by messages in doc-processing-queue (Event Grid → Queue).
    Uses FoundryDocPipeline (Content Understanding, Azure Language PII).

    The queue message body is an Event Grid event JSON envelope.
    """
    try:
        # Event Grid sends an array of events to the queue
        body = msg.get_body().decode("utf-8")
        event_payload = json.loads(body)

        # Event Grid may send a single event or an array
        if isinstance(event_payload, list):
            events = event_payload
        else:
            events = [event_payload]

        for event in events:
            data = event.get("data", event)

            info = _extract_blob_info(data)
            if info is None:
                continue

            container, blob_path, content_type, content_length = info

            logger.info(
                f"[Queue] Processing: {blob_path} ({content_length} bytes, {content_type})"
            )

            metadata = {
                "source_url": data.get("url", ""),
                "content_type": content_type,
                "file_size_bytes": content_length,
            }

            pipeline = _get_pipeline()
            result = pipeline.process_document(container, blob_path, metadata)
            logger.info(f"[Queue] Result: {json.dumps(result)}")

    except Exception as e:
        logger.error(f"[Queue] Error processing queue message: {e}")
        raise  # Re-raise so the message goes back to queue for retry


# ---------------------------------------------------------------------------
# 3. Blob Trigger: process documents directly from blob storage
#    Active when TRIGGER_MODE=BLOB.
#    Disabled via AzureWebJobs.process_blob_document.Disabled=true when inactive.
# ---------------------------------------------------------------------------
@app.function_name("process_blob_document")
@app.blob_trigger(
    arg_name="blob",
    path="%ADLS_CONTAINER_RAW%/{name}",
    connection="ADLS_BLOB_CONNECTION",
)
def process_blob_document(blob: func.InputStream):
    """Triggered when a blob is created/updated in raw-documents container.
    Uses FoundryDocPipeline (Content Understanding, Azure Language PII).

    Simplest trigger mode -- no Event Grid or Queue infrastructure required.
    The blob trigger polls the container for changes directly.
    """
    # Skip folder creation events — only process actual files
    blob_meta = blob.metadata or {}
    if blob_meta.get("hdi_isfolder") == "true":
        logger.info(f"[BlobTrigger] Skipping folder creation event: {blob.name}")
        return

    blob_name = blob.name or ""
    content_type = ""
    content_length = blob.length or 0

    logger.info(f"[BlobTrigger] Detected blob: {blob_name} ({content_length} bytes)")

    # Skip metadata sidecar files
    if blob_name.endswith(".metadata.json") or blob_name.endswith(".error.json"):
        logger.debug(f"[BlobTrigger] Skipping metadata file: {blob_name}")
        return

    # Skip zero-byte files
    if content_length == 0:
        logger.debug(f"[BlobTrigger] Skipping zero-byte file: {blob_name}")
        return

    container = os.environ.get("ADLS_CONTAINER_RAW", "raw-documents")

    # blob.name includes container prefix from path binding: "raw-documents/{name}"
    # Strip container prefix if present
    if blob_name.startswith(f"{container}/"):
        blob_path = blob_name[len(f"{container}/") :]
    else:
        blob_path = blob_name

    logger.info(f"[BlobTrigger] Processing: {blob_path} ({content_length} bytes)")

    metadata = {
        "source_url": f"https://{os.environ.get('ADLS_ACCOUNT_NAME')}.blob.core.windows.net/{container}/{blob_path}",
        "content_type": content_type,
        "file_size_bytes": content_length,
    }

    pipeline = _get_pipeline()
    result = pipeline.process_document(container, blob_path, metadata)

    logger.info(f"[BlobTrigger] Result: {json.dumps(result)}")


# ---------------------------------------------------------------------------
# 4. HTTP Trigger: health check
# ---------------------------------------------------------------------------
@app.function_name("health_check")
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health_check(req: func.HttpRequest) -> func.HttpResponse:
    """Health check endpoint. Reports active processing path and trigger mode."""
    return func.HttpResponse(
        json.dumps(
            {
                "status": "healthy",
                "service": os.environ.get("FUNCTION_APP_NAME", "ai-foundry-processing"),
                "processing_path": "AI_FOUNDRY_SERVICES",
                "trigger_mode": os.environ.get("TRIGGER_MODE", "BLOB"),
            }
        ),
        status_code=200,
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# 5. HTTP Trigger: ensure search index exists (create if missing)
#    POST /api/ensure-index — call after deploying to a new environment
# ---------------------------------------------------------------------------
@app.function_name("ensure_index")
@app.route(route="ensure-index", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def ensure_index(req: func.HttpRequest) -> func.HttpResponse:
    """Create the AI Search index if it does not already exist.

    Uses the same schema defined in SearchPusher so the index is always
    consistent with what the ingestion pipeline expects.
    """
    try:
        from modules.search_pusher import SearchPusher

        pusher = SearchPusher()
        return func.HttpResponse(
            json.dumps(
                {
                    "status": "ok",
                    "index_name": pusher.index_name,
                    "endpoint": pusher.endpoint,
                    "message": f"Index '{pusher.index_name}' exists (created if missing)",
                }
            ),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        logger.error(f"[EnsureIndex] Failed to ensure index: {e}")
        return func.HttpResponse(
            json.dumps({"status": "error", "error": str(e)}),
            status_code=500,
            mimetype="application/json",
        )
