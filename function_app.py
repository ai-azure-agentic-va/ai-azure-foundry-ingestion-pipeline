"""Azure Functions entry point — Event Grid, Queue, Blob, and HTTP health triggers."""

import json
import logging
import os
import threading

import azure.functions as func

from ingestion.config import settings as _cfg

ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".xlsm",
    ".pptx", ".ppt", ".csv", ".txt", ".md", ".markdown",
    ".json", ".xml", ".png", ".jpg", ".jpeg", ".tiff", ".bmp",
}


def _is_allowed_extension(blob_path: str) -> bool:
    ext = os.path.splitext(blob_path)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        logger.warning(f"[AllowList] Rejecting unsupported file: {blob_path} (ext: {ext or 'none'})")
        return False
    return True

app = func.FunctionApp()

logger = logging.getLogger(__name__)
logging.basicConfig(level=getattr(logging, _cfg.LOG_LEVEL))

_pipeline = None
_pipeline_lock = threading.Lock()


def _get_pipeline() -> "FoundryDocPipeline":
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                from ingestion.pipeline import FoundryDocPipeline
                _pipeline = FoundryDocPipeline()
                logger.info(f"[Functions] Pipeline initialized: {_pipeline.PIPELINE_NAME}")
    return _pipeline


def _extract_blob_info(data: dict) -> tuple[str, str, str, int] | None:
    blob_url = data.get("url", "")
    content_type = data.get("contentType", "")
    content_length = data.get("contentLength", 0)

    container = _cfg.ADLS_CONTAINER_RAW
    try:
        path_start = blob_url.index(f"/{container}/") + len(f"/{container}/")
        blob_path = blob_url[path_start:]
    except ValueError:
        logger.error(f"[Trigger] Cannot parse blob path from URL: {blob_url}")
        return None

    if blob_path.endswith(".error.json"):
        logger.debug(f"[Trigger] Skipping error file: {blob_path}")
        return None

    if content_length == 0:
        logger.debug(f"[Trigger] Skipping zero-byte file: {blob_path}")
        return None

    if blob_path.endswith("/"):
        logger.info(f"[Trigger] Skipping folder marker: {blob_path}")
        return None

    if not _is_allowed_extension(blob_path):
        return None

    return container, blob_path, content_type, content_length


@app.function_name("process_new_document")
@app.event_grid_trigger(arg_name="event")
def process_new_document(event: func.EventGridEvent):
    logger.info(f"[EventGrid] Received event: {event.event_type}, subject: {event.subject}")

    data = event.get_json()
    info = _extract_blob_info(data)
    if info is None:
        return

    container, blob_path, content_type, content_length = info
    logger.info(f"[EventGrid] Processing: {blob_path} ({content_length} bytes, {content_type})")

    pipeline = _get_pipeline()
    result = pipeline.process_document(container, blob_path)
    logger.info(f"[EventGrid] Result: {json.dumps(result)}")
    if result.get("status") == "error":
        logger.error(f"[EventGrid] Pipeline failed at stage={result.get('stage')}: {blob_path}")


@app.function_name("process_queue_document")
@app.queue_trigger(
    arg_name="msg",
    queue_name="%QUEUE_NAME%",
    connection="ADLS_QUEUE_CONNECTION",
)
def process_queue_document(msg: func.QueueMessage):
    body = msg.get_body().decode("utf-8")
    event_payload = json.loads(body)
    events = event_payload if isinstance(event_payload, list) else [event_payload]

    for event in events:
        data = event.get("data", event)
        info = _extract_blob_info(data)
        if info is None:
            continue

        container, blob_path, content_type, content_length = info
        logger.info(f"[Queue] Processing: {blob_path} ({content_length} bytes, {content_type})")

        pipeline = _get_pipeline()
        result = pipeline.process_document(container, blob_path)
        logger.info(f"[Queue] Result: {json.dumps(result)}")
        if result.get("status") == "error":
            logger.error(f"[Queue] Pipeline failed at stage={result.get('stage')}: {blob_path}")


@app.function_name("process_blob_document")
@app.blob_trigger(
    arg_name="blob",
    path="%ADLS_CONTAINER_RAW%/{name}",
    connection="ADLS_BLOB_CONNECTION",
)
def process_blob_document(blob: func.InputStream):
    blob_meta = blob.metadata or {}
    if blob_meta.get("hdi_isfolder") == "true":
        logger.info(f"[BlobTrigger] Skipping folder creation event: {blob.name}")
        return

    blob_name = blob.name or ""
    content_type = ""
    content_length = blob.length or 0

    logger.info(f"[BlobTrigger] Detected blob: {blob_name} ({content_length} bytes)")

    if blob_name.endswith(".error.json"):
        logger.debug(f"[BlobTrigger] Skipping error file: {blob_name}")
        return

    if content_length == 0:
        logger.debug(f"[BlobTrigger] Skipping zero-byte file: {blob_name}")
        return

    if not _is_allowed_extension(blob_name):
        return

    container = _cfg.ADLS_CONTAINER_RAW

    if blob_name.startswith(f"{container}/"):
        blob_path = blob_name[len(f"{container}/"):]
    else:
        blob_path = blob_name

    logger.info(f"[BlobTrigger] Processing: {blob_path} ({content_length} bytes)")

    pipeline = _get_pipeline()
    result = pipeline.process_document(container, blob_path)
    logger.info(f"[BlobTrigger] Result: {json.dumps(result)}")
    if result.get("status") == "error":
        logger.error(f"[BlobTrigger] Pipeline failed at stage={result.get('stage')}: {blob_path}")


@app.function_name("health_check")
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health_check(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({
            "status": "healthy",
            "service": _cfg.FUNCTION_APP_NAME,
            "processing_path": "AI_FOUNDRY_SERVICES",
            "trigger_mode": _cfg.TRIGGER_MODE,
        }),
        status_code=200,
        mimetype="application/json",
    )
