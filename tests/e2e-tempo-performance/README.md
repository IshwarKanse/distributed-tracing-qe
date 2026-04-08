# Tempo Performance Tests

End-to-end performance tests for TempoStack using the Tempo Operator's built-in t-shirt sizes.

## Prerequisites

- OpenShift cluster with Tempo Operator installed
- OpenTelemetry Operator installed
- Chainsaw test framework
- Cluster resources per size (with gateway + Jaeger UI):

| Size | CPU | Memory |
|------|-----|--------|
| 1x.pico | 3.4 vCPUs | 8.9 Gi |
| 1x.extra-small | 5.1 vCPUs | 22.3 Gi |
| 1x.small | 8.8 vCPUs | 30.4 Gi |
| 1x.medium | 28.1 vCPUs | 47.4 Gi |

## Test Sizes

Each test uses the Tempo Operator's `spec.size` field for automatic resource allocation.
Ingestion rates match the [documented sizing recommendations](https://github.com/openshift/openshift-docs/pull/107838).

| Size | Ingestion Rate | TPS | Spans/Trace | Runtime | Parallelism | Storage |
|------|---------------|-----|-------------|---------|-------------|---------|
| **1x.pico** | ~50GB/day (~0.6 MB/s) | 20 | 50 | 5min | 1 pod | 10Gi |
| **1x.extra-small** | ~100GB/day (~1.2 MB/s) | 40 | 50 | 5min | 1 pod | 20Gi |
| **1x.small** | ~500GB/day (~5.8 MB/s) | 100 | 50 | 5min | 2 pods | 50Gi |
| **1x.medium** | ~2TB/day (~23 MB/s) | 200 | 50 | 10min | 5 pods | 200Gi |

## Running Tests

```bash
# Run a specific size
chainsaw test --test-dir tests/e2e-tempo-performance/1x-pico/
chainsaw test --test-dir tests/e2e-tempo-performance/1x-extra-small/
chainsaw test --test-dir tests/e2e-tempo-performance/1x-small/
chainsaw test --test-dir tests/e2e-tempo-performance/1x-medium/

# Run all sizes
chainsaw test --test-dir tests/e2e-tempo-performance/

# Run without cleanup (for debugging)
chainsaw test --test-dir tests/e2e-tempo-performance/1x-pico/ --skip-delete
```

## Test Flow

Each test executes the following steps:

1. **Deploy MinIO** - S3-compatible storage backend
2. **Deploy TempoStack** - Using operator t-shirt size with multitenancy (OpenShift mode)
3. **Deploy OTel Collector** - Receives traces via OTLP gRPC and forwards to Tempo gateway
4. **Generate Load** - Honeycomb loadgen sends traces at the target TPS rate
5. **Verify Traces** - Queries both Jaeger API and TraceQL to confirm traces were ingested
6. **Verify Metrics** - Scrapes Tempo `/metrics` endpoints directly and asserts:
   - Spans received > 0
   - Zero discarded spans
   - Zero refused spans
   - Traces created > 0
7. **Verify Query Performance** - Measures query response times and asserts they are below 30s threshold

## Assertions

- No dropped or rejected spans (zero tolerance)
- All traces queryable via Jaeger and TraceQL APIs
- Query response times below 30s threshold
- All Tempo components healthy throughout the test

## Architecture

```
loadgen --> OTel Collector --> Tempo Gateway --> Tempo Distributor --> Tempo Ingester --> MinIO
                                    |
                              (bearer token auth)
                              (X-Scope-OrgID: dev)
```

Metrics are scraped directly from Tempo component `/metrics` endpoints (no dependency on cluster monitoring stack).
