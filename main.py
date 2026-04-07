"""
Vertex AI Search — Observability Sidecar Cloud Function
========================================================
Deployed as a Cloud Function (2nd gen) triggered by Cloud Scheduler every 5 minutes.

This function collects three categories of metrics from the Discovery Engine API
and writes them as custom time-series data to Google Cloud Monitoring:

  1. Ingestion Velocity  — Total indexed document count per Data Store.
  2. LRO Queue Depth     — Count of pending vs. completed import operations.
  3. Synthetic Latency   — Round-trip search latency (ms) for a canary query.

References:
  - Discovery Engine documents.list API:
    https://docs.cloud.google.com/generative-ai-app-builder/docs/reference/rest/v1/projects.locations.collections.dataStores.branches.documents/list
  - Discovery Engine operations.list API:
    https://docs.cloud.google.com/generative-ai-app-builder/docs/long-running-operations
  - Cloud Monitoring custom metrics:
    https://docs.cloud.google.com/monitoring/custom-metrics/creating-metrics
"""

import os
import time
import logging
import functions_framework
from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import monitoring_v3
from google.protobuf import timestamp_pb2
from google.api import metric_pb2, monitored_resource_pb2

# ─── Configuration via Environment Variables ───────────────────────────────────
PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
LOCATION = os.environ.get("LOCATION", "global")
DATA_STORE_IDS = os.environ.get("DATA_STORE_IDS", "").split(",")  # Comma-separated
CANARY_QUERY = os.environ.get("CANARY_QUERY", "test search query")
COLLECTION = "default_collection"
BRANCH = "default_branch"
SERVING_CONFIG = "default_search"

# ─── Metric Names ─────────────────────────────────────────────────────────────
METRIC_DOC_COUNT = "custom.googleapis.com/vertex_ai_search/indexed_document_count"
METRIC_LRO_PENDING = "custom.googleapis.com/vertex_ai_search/lro_pending_count"
METRIC_LRO_COMPLETED = "custom.googleapis.com/vertex_ai_search/lro_completed_count"
METRIC_SEARCH_LATENCY = "custom.googleapis.com/vertex_ai_search/search_latency_ms"
METRIC_SEARCH_SUCCESS = "custom.googleapis.com/vertex_ai_search/search_success"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ─── Metric 1: Indexed Document Count ─────────────────────────────────────────
def get_document_count(data_store_id: str) -> int:
    """
    Counts the total number of indexed documents in a Data Store by paginating
    through the documents.list API.

    For very large stores (millions of docs), this uses a page_size of 1000
    and counts pages. At 61M docs this would be slow — see the note below
    about using the estimatedTotalSize from a search response instead.
    """
    client = discoveryengine.DocumentServiceClient()
    parent = (
        f"projects/{PROJECT_ID}/locations/{LOCATION}"
        f"/collections/{COLLECTION}/dataStores/{data_store_id}"
        f"/branches/{BRANCH}"
    )

    # FAST PATH: Use a search request with page_size=0 to get totalSize
    # This is much faster than paginating documents.list for large stores.
    try:
        search_client = discoveryengine.SearchServiceClient()
        serving_config = (
            f"projects/{PROJECT_ID}/locations/{LOCATION}"
            f"/collections/{COLLECTION}/dataStores/{data_store_id}"
            f"/servingConfigs/{SERVING_CONFIG}"
        )
        request = discoveryengine.SearchRequest(
            serving_config=serving_config,
            query="*",
            page_size=1,
        )
        response = search_client.search(request=request)
        total = response.total_size
        if total and total > 0:
            logger.info(f"[{data_store_id}] Document count (via search totalSize): {total}")
            return total
    except Exception as e:
        logger.warning(f"[{data_store_id}] Fast path failed, falling back to documents.list: {e}")

    # SLOW PATH: Paginate through documents.list
    count = 0
    request = discoveryengine.ListDocumentsRequest(
        parent=parent,
        page_size=1000,
    )
    try:
        page_result = client.list_documents(request=request)
        for _ in page_result:
            count += 1
    except Exception as e:
        logger.error(f"[{data_store_id}] Error counting documents: {e}")
        return -1

    logger.info(f"[{data_store_id}] Document count (via list): {count}")
    return count


# ─── Metric 2: LRO Queue Depth ────────────────────────────────────────────────
def get_lro_counts(data_store_id: str) -> dict:
    """
    Lists all long-running operations for a Data Store and categorizes them
    as pending (done=False) or completed (done=True).

    Operations are retained for ~30 days after completion.
    Ref: https://docs.cloud.google.com/generative-ai-app-builder/docs/long-running-operations
    """
    from google.api_core import client_options
    from google.longrunning import operations_client

    # The Discovery Engine operations endpoint
    api_endpoint = (
        f"{LOCATION}-discoveryengine.googleapis.com"
        if LOCATION != "global"
        else "discoveryengine.googleapis.com"
    )
    opts = client_options.ClientOptions(api_endpoint=api_endpoint)
    ops_client = operations_client.OperationsClient(
        transport=operations_client.OperationsGrpcTransport(
            host=api_endpoint,
        )
    )

    # Use REST-based approach instead for simplicity
    import google.auth
    import google.auth.transport.requests
    import requests as http_requests

    credentials, project = google.auth.default()
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)

    url = (
        f"https://discoveryengine.googleapis.com/v1/projects/{PROJECT_ID}"
        f"/locations/{LOCATION}/collections/{COLLECTION}"
        f"/dataStores/{data_store_id}/operations"
    )
    headers = {"Authorization": f"Bearer {credentials.token}"}

    pending = 0
    completed = 0
    next_page_token = None

    try:
        while True:
            params = {"pageSize": 100}
            if next_page_token:
                params["pageToken"] = next_page_token

            resp = http_requests.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

            for op in data.get("operations", []):
                if op.get("done", False):
                    completed += 1
                else:
                    pending += 1

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

    except Exception as e:
        logger.error(f"[{data_store_id}] Error listing operations: {e}")
        return {"pending": -1, "completed": -1}

    logger.info(f"[{data_store_id}] LRO pending={pending}, completed={completed}")
    return {"pending": pending, "completed": completed}


# ─── Metric 3: Synthetic Search Latency ───────────────────────────────────────
def measure_search_latency(data_store_id: str) -> dict:
    """
    Fires a canary search query and measures the round-trip time in milliseconds.
    Returns both the latency and a success boolean.

    Ref: https://docs.cloud.google.com/generative-ai-app-builder/docs/preview-search-results
    """
    client = discoveryengine.SearchServiceClient()
    serving_config = (
        f"projects/{PROJECT_ID}/locations/{LOCATION}"
        f"/collections/{COLLECTION}/dataStores/{data_store_id}"
        f"/servingConfigs/{SERVING_CONFIG}"
    )

    request = discoveryengine.SearchRequest(
        serving_config=serving_config,
        query=CANARY_QUERY,
        page_size=5,
    )

    start_time = time.time()
    success = True
    try:
        response = client.search(request=request)
        # Force evaluation of the first page
        _ = list(response.results) if hasattr(response, 'results') else None
    except Exception as e:
        logger.error(f"[{data_store_id}] Search probe failed: {e}")
        success = False

    latency_ms = (time.time() - start_time) * 1000
    logger.info(f"[{data_store_id}] Search latency={latency_ms:.1f}ms, success={success}")
    return {"latency_ms": latency_ms, "success": success}


# ─── Write Metrics to Cloud Monitoring ─────────────────────────────────────────
def write_metric(metric_type: str, value: float, data_store_id: str, value_type: str = "double"):
    """
    Writes a single custom metric data point to Cloud Monitoring.

    Ref: https://docs.cloud.google.com/monitoring/custom-metrics/creating-metrics
    """
    client = monitoring_v3.MetricServiceClient()
    project_name = f"projects/{PROJECT_ID}"

    now = time.time()
    seconds = int(now)
    nanos = int((now - seconds) * 10**9)

    interval = monitoring_v3.TimeInterval(
        end_time={"seconds": seconds, "nanos": nanos}
    )

    if value_type == "int":
        point = monitoring_v3.Point(
            interval=interval,
            value=monitoring_v3.TypedValue(int64_value=int(value)),
        )
    elif value_type == "bool":
        point = monitoring_v3.Point(
            interval=interval,
            value=monitoring_v3.TypedValue(bool_value=bool(value)),
        )
    else:
        point = monitoring_v3.Point(
            interval=interval,
            value=monitoring_v3.TypedValue(double_value=float(value)),
        )

    series = monitoring_v3.TimeSeries()
    series.metric.type = metric_type
    series.metric.labels["data_store_id"] = data_store_id
    series.resource.type = "global"
    series.points = [point]

    try:
        client.create_time_series(
            request={"name": project_name, "time_series": [series]}
        )
        logger.info(f"Wrote metric {metric_type} = {value} for {data_store_id}")
    except Exception as e:
        logger.error(f"Failed to write metric {metric_type}: {e}")


# ─── Main Entry Point ─────────────────────────────────────────────────────────
@functions_framework.http
def observability_probe(request):
    """
    HTTP-triggered Cloud Function entry point.
    Called by Cloud Scheduler every 5 minutes.
    """
    if not PROJECT_ID:
        return ("GCP_PROJECT_ID environment variable not set", 500)
    if not DATA_STORE_IDS or DATA_STORE_IDS == [""]:
        return ("DATA_STORE_IDS environment variable not set", 500)

    results = {}

    for ds_id in DATA_STORE_IDS:
        ds_id = ds_id.strip()
        if not ds_id:
            continue

        logger.info(f"=== Probing Data Store: {ds_id} ===")
        ds_results = {}

        # ── Metric 1: Document Count ──
        doc_count = get_document_count(ds_id)
        if doc_count >= 0:
            write_metric(METRIC_DOC_COUNT, doc_count, ds_id, value_type="int")
        ds_results["document_count"] = doc_count

        # ── Metric 2: LRO Queue Depth ──
        lro = get_lro_counts(ds_id)
        if lro["pending"] >= 0:
            write_metric(METRIC_LRO_PENDING, lro["pending"], ds_id, value_type="int")
            write_metric(METRIC_LRO_COMPLETED, lro["completed"], ds_id, value_type="int")
        ds_results["lro_pending"] = lro["pending"]
        ds_results["lro_completed"] = lro["completed"]

        # ── Metric 3: Search Latency ──
        latency = measure_search_latency(ds_id)
        write_metric(METRIC_SEARCH_LATENCY, latency["latency_ms"], ds_id)
        write_metric(METRIC_SEARCH_SUCCESS, 1.0 if latency["success"] else 0.0, ds_id)
        ds_results["search_latency_ms"] = round(latency["latency_ms"], 1)
        ds_results["search_success"] = latency["success"]

        results[ds_id] = ds_results

    logger.info(f"Probe complete: {results}")
    return (results, 200)
