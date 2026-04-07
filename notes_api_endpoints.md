# Discovery Engine API Endpoints for Observability

## 1. documents.list — Count indexed documents
GET https://discoveryengine.googleapis.com/v1/projects/{PROJECT_ID}/locations/global/collections/default_collection/dataStores/{DATA_STORE_ID}/branches/default_branch/documents
- Paginated response with nextPageToken
- Returns Document objects
- Use page_size=1 and count total via pagination or use metadata

## 2. operations.list — Track LRO queue depth
GET https://discoveryengine.googleapis.com/v1/projects/{PROJECT_ID}/locations/global/collections/default_collection/dataStores/{DATA_STORE_ID}/operations
- Returns list of operations with name, metadata, done status
- Operation names like: import-documents-{id}
- Metadata type: google.cloud.discoveryengine.v1.ImportDocumentsMetadata
- Operations kept for ~30 days after completion

## 3. servingConfigs.search — Synthetic latency probe
POST https://discoveryengine.googleapis.com/v1/projects/{PROJECT_ID}/locations/global/collections/default_collection/dataStores/{DATA_STORE_ID}/servingConfigs/default_search:search
- Use to measure search latency
- Time the round-trip of a standard query

## 4. Cloud Monitoring Custom Metrics
- Metric descriptor: custom.googleapis.com/vertex_ai_search/{metric_name}
- Use google-cloud-monitoring Python client
- Write time series data points
- Monitored resource type: "global" or "generic_task"

## Python Client Libraries Needed
- google-cloud-discoveryengine
- google-cloud-monitoring
- google-cloud-logging (optional)
