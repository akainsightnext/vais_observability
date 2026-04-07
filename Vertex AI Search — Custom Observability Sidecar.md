# Vertex AI Search — Custom Observability Sidecar

This repository contains the complete infrastructure-as-code and application logic to deploy a custom observability layer for Vertex AI Search. 

It addresses the "black box" nature of the Discovery Engine API by continuously probing the system and writing custom metrics to Google Cloud Monitoring.

## What it Does

A Cloud Scheduler job triggers a 2nd Gen Cloud Function every 5 minutes. The function queries the Discovery Engine API and writes the following custom metrics to Cloud Monitoring:

1. **Ingestion Velocity (`indexed_document_count`)**  
   Tracks the exact number of documents successfully indexed in the Data Store.
2. **LRO Queue Depth (`lro_pending_count`, `lro_completed_count`)**  
   Tracks the number of pending vs. completed long-running import operations.
3. **Search Latency (`search_latency_ms`, `search_success`)**  
   Fires a synthetic "canary" search query and measures the Time to First Token (TTFT) in milliseconds.

These metrics allow you to correlate massive batch ingestion runs with live search latency to detect infrastructure contention.

## Repository Structure

```
.
├── cloud_function/
│   ├── main.py            # The Python observability probe logic
│   ├── requirements.txt   # Python dependencies
│   └── env.yaml           # Local environment variables template
├── terraform/
│   ├── main.tf            # Deploys Function, Scheduler, IAM, and Alerts
│   ├── terraform.tfvars.example
│   └── dashboard.json     # Cloud Monitoring Dashboard template
└── docs/
    └── README.md          # This file
```

## Deployment Instructions

### Prerequisites
- Google Cloud SDK (`gcloud`) installed and authenticated.
- Terraform installed (>= 1.5).
- A GCP Project with Vertex AI Search enabled.

### 1. Prepare the Terraform Variables

Navigate to the `terraform` directory and copy the example variables file:

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` and update the values with your specific GCP Project ID, Data Store IDs, and alert email address.

### 2. Package the Cloud Function

The Terraform configuration expects a zipped version of the Cloud Function source code.

```bash
cd ../cloud_function
zip -r ../terraform/function_source.zip .
cd ../terraform
```

### 3. Deploy the Infrastructure

Initialize and apply the Terraform configuration. This will create the service account, storage bucket, Cloud Function, Cloud Scheduler job, and the alerting policies.

```bash
terraform init
terraform apply
```

### 4. Import the Cloud Monitoring Dashboard

The `dashboard.json` file contains a pre-built Looker/Cloud Monitoring dashboard that visualizes all the custom metrics. You can import this directly via the `gcloud` CLI:

```bash
gcloud monitoring dashboards create --config-from-file=dashboard.json --project=YOUR_PROJECT_ID
```

## Alerting Policies Created

The Terraform script automatically provisions three alerting policies:

1. **High Search Latency:** Alerts if the synthetic search probe takes longer than 5 seconds for 5 consecutive minutes. This is the primary indicator of ingestion-vs-query contention.
2. **Search Probe Failure:** Alerts immediately if the Vertex AI Search API returns an error for the canary query.
3. **Ingestion Stall:** Alerts if the total indexed document count does not increase for 30 minutes.

## References
[1] [Create user-defined metrics with the API | Cloud Monitoring](https://docs.cloud.google.com/monitoring/custom-metrics/creating-metrics)
[2] [Monitor long-running operations | Vertex AI Search](https://docs.cloud.google.com/generative-ai-app-builder/docs/long-running-operations)
[3] [Method: projects.locations.collections.dataStores.branches.documents.list](https://docs.cloud.google.com/generative-ai-app-builder/docs/reference/rest/v1/projects.locations.collections.dataStores.branches.documents/list)
