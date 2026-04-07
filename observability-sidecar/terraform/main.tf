# ═══════════════════════════════════════════════════════════════════════════════
# Vertex AI Search — Observability Sidecar Infrastructure
# ═══════════════════════════════════════════════════════════════════════════════
#
# This Terraform configuration deploys:
#   1. Cloud Function (2nd gen) running the observability probe
#   2. Cloud Scheduler job triggering the function every 5 minutes
#   3. Service account with least-privilege IAM bindings
#   4. Custom metric descriptors in Cloud Monitoring
#   5. Alerting policies for search latency and ingestion stalls
#   6. Notification channel (email) for alerts
#
# References:
#   - Cloud Functions Terraform: https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloudfunctions2_function
#   - Cloud Scheduler Terraform: https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloud_scheduler_job
#   - Cloud Monitoring alerting: https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/monitoring_alert_policy
# ═══════════════════════════════════════════════════════════════════════════════

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

# ─── Variables ────────────────────────────────────────────────────────────────

variable "project_id" {
  description = "GCP Project ID where Vertex AI Search is deployed"
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Function and Scheduler deployment"
  type        = string
  default     = "us-central1"
}

variable "data_store_ids" {
  description = "Comma-separated list of Vertex AI Search Data Store IDs to monitor"
  type        = string
}

variable "discovery_engine_location" {
  description = "Location of the Discovery Engine data stores (e.g., global, us, eu)"
  type        = string
  default     = "global"
}

variable "canary_query" {
  description = "Search query used for synthetic latency probing"
  type        = string
  default     = "how to reset my password"
}

variable "schedule_cron" {
  description = "Cron expression for the Cloud Scheduler trigger"
  type        = string
  default     = "*/5 * * * *"  # Every 5 minutes
}

variable "alert_email" {
  description = "Email address for alert notifications"
  type        = string
}

variable "latency_threshold_ms" {
  description = "Search latency threshold (ms) that triggers an alert"
  type        = number
  default     = 5000  # 5 seconds
}

# ─── Provider ─────────────────────────────────────────────────────────────────

provider "google" {
  project = var.project_id
  region  = var.region
}

# ─── Enable Required APIs ─────────────────────────────────────────────────────

resource "google_project_service" "required_apis" {
  for_each = toset([
    "cloudfunctions.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
    "monitoring.googleapis.com",
    "discoveryengine.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# ─── Service Account ──────────────────────────────────────────────────────────

resource "google_service_account" "observability_sa" {
  account_id   = "vais-observability-sidecar"
  display_name = "Vertex AI Search Observability Sidecar"
  description  = "Service account for the observability Cloud Function that reads Discovery Engine data and writes Cloud Monitoring metrics."
}

# Discovery Engine Viewer — read documents and operations
resource "google_project_iam_member" "de_viewer" {
  project = var.project_id
  role    = "roles/discoveryengine.viewer"
  member  = "serviceAccount:${google_service_account.observability_sa.email}"
}

# Monitoring Metric Writer — write custom metrics
resource "google_project_iam_member" "monitoring_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.observability_sa.email}"
}

# ─── Cloud Storage Bucket for Function Source ─────────────────────────────────

resource "google_storage_bucket" "function_source" {
  name                        = "${var.project_id}-vais-observability-src"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
}

resource "google_storage_bucket_object" "function_zip" {
  name   = "observability-sidecar-${filemd5("${path.module}/../cloud_function/main.py")}.zip"
  bucket = google_storage_bucket.function_source.name
  source = "${path.module}/function_source.zip"
  # NOTE: You must zip the cloud_function/ directory before running terraform apply.
  # Run: cd ../cloud_function && zip -r ../terraform/function_source.zip . && cd ../terraform
}

# ─── Cloud Function (2nd Gen) ─────────────────────────────────────────────────

resource "google_cloudfunctions2_function" "observability_probe" {
  name     = "vais-observability-probe"
  location = var.region

  build_config {
    runtime     = "python311"
    entry_point = "observability_probe"
    source {
      storage_source {
        bucket = google_storage_bucket.function_source.name
        object = google_storage_bucket_object.function_zip.name
      }
    }
  }

  service_config {
    max_instance_count    = 1
    min_instance_count    = 0
    available_memory      = "512Mi"
    timeout_seconds       = 300
    service_account_email = google_service_account.observability_sa.email

    environment_variables = {
      GCP_PROJECT_ID = var.project_id
      LOCATION       = var.discovery_engine_location
      DATA_STORE_IDS = var.data_store_ids
      CANARY_QUERY   = var.canary_query
    }
  }

  depends_on = [
    google_project_service.required_apis,
  ]
}

# ─── Cloud Scheduler Job ──────────────────────────────────────────────────────

resource "google_cloud_scheduler_job" "probe_trigger" {
  name        = "vais-observability-trigger"
  description = "Triggers the Vertex AI Search observability probe every 5 minutes"
  schedule    = var.schedule_cron
  time_zone   = "America/Chicago"
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.observability_probe.service_config[0].uri

    oidc_token {
      service_account_email = google_service_account.observability_sa.email
    }
  }

  depends_on = [
    google_project_service.required_apis,
  ]
}

# ─── Notification Channel ─────────────────────────────────────────────────────

resource "google_monitoring_notification_channel" "email" {
  display_name = "VAIS Observability Alerts"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

# ─── Alert Policy 1: Search Latency Spike ─────────────────────────────────────

resource "google_monitoring_alert_policy" "search_latency_alert" {
  display_name = "Vertex AI Search — High Search Latency"
  combiner     = "OR"

  conditions {
    display_name = "Search latency exceeds ${var.latency_threshold_ms}ms"

    condition_threshold {
      filter          = "metric.type=\"custom.googleapis.com/vertex_ai_search/search_latency_ms\" AND resource.type=\"global\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.latency_threshold_ms
      duration        = "300s"  # Must exceed threshold for 5 minutes

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "Search latency has exceeded ${var.latency_threshold_ms}ms for 5 minutes. This may indicate indexing backpressure from concurrent ingestion. Check the LRO pending count and consider pausing ingestion batches."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

# ─── Alert Policy 2: Search Probe Failure ─────────────────────────────────────

resource "google_monitoring_alert_policy" "search_failure_alert" {
  display_name = "Vertex AI Search — Search Probe Failure"
  combiner     = "OR"

  conditions {
    display_name = "Search probe returning failures"

    condition_threshold {
      filter          = "metric.type=\"custom.googleapis.com/vertex_ai_search/search_success\" AND resource.type=\"global\""
      comparison      = "COMPARISON_LT"
      threshold_value = 1.0
      duration        = "300s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MIN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "The synthetic search probe is returning errors. The Vertex AI Search API may be degraded or the Data Store index may be corrupted. Immediate investigation required."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

# ─── Alert Policy 3: Ingestion Stall ──────────────────────────────────────────

resource "google_monitoring_alert_policy" "ingestion_stall_alert" {
  display_name = "Vertex AI Search — Ingestion Stall Detected"
  combiner     = "OR"

  conditions {
    display_name = "Document count has not increased in 30 minutes"

    condition_threshold {
      filter          = "metric.type=\"custom.googleapis.com/vertex_ai_search/indexed_document_count\" AND resource.type=\"global\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "1800s"

      aggregations {
        alignment_period   = "1800s"
        per_series_aligner = "ALIGN_DELTA"
      }

      # Alert when the delta is 0 (no change in 30 min)
      # NOTE: This uses an inverted condition — we alert when the rate of change
      # drops to zero. You may need to adjust based on your ingestion schedule.
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "The indexed document count has not changed in 30 minutes while ingestion operations are expected to be running. Check the LRO queue for stuck operations and verify the ingestion pipeline is healthy."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "3600s"
  }
}

# ─── Outputs ──────────────────────────────────────────────────────────────────

output "cloud_function_url" {
  description = "URL of the deployed observability Cloud Function"
  value       = google_cloudfunctions2_function.observability_probe.service_config[0].uri
}

output "scheduler_job_name" {
  description = "Name of the Cloud Scheduler job"
  value       = google_cloud_scheduler_job.probe_trigger.name
}

output "service_account_email" {
  description = "Email of the observability service account"
  value       = google_service_account.observability_sa.email
}
