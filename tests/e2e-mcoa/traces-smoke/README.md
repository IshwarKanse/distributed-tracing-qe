# MCOA Traces Smoke

End-to-end smoke test for the MCOA (`multicluster-observability-addon`) traces
capability. Validates that when a hub cluster has MCOA installed and a tracing
"stanza" is applied, MCOA renders an `OpenTelemetryCollector` on the spoke
cluster (`mcoa-instance` in namespace `mcoa-opentelemetry`) and that traces
sent into it reach a central `TempoStack`.

## What this test does

```text
chainsaw step                       resources touched
────────────────────────────────────────────────────────────────────────────────
00 install-minio-storage            PVC, Deployment, Service, Secret (MinIO)
01 install-tempostack               TempoStack/smoke (Tempo Operator reconciles)
02 apply-mcoa-traces-stanza         OpenTelemetryCollector/instance + Secret
                                    in open-cluster-management-observability;
                                    ManagedClusterAddOn on local-cluster
03 assert-mcoa-renders-otelcol      Wait for MCOA → rendered OTelCol Deployment
                                    mcoa-instance-collector becomes Ready in
                                    namespace mcoa-opentelemetry
04 generate-traces                  Job runs telemetrygen against
                                    mcoa-instance-collector
05 verify-traces-in-tempo           Query Tempo Jaeger API for the service
```

## Topology

Single-cluster ("hub self-managed"). The hub acts as both the OCM hub and the
spoke `local-cluster`. MCOA's `ManifestWork` is reconciled on the same cluster,
which keeps the test cheap to run.

```text
┌─────────────────────────── single OCP cluster ──────────────────────────────┐
│                                                                              │
│   namespace: open-cluster-management-observability                           │
│   ┌──────────────────────────────────────────────────────────────────────┐   │
│   │  MCOA addon-manager (Deployment)                                      │   │
│   │  ClusterManagementAddOn                                               │   │
│   │  AddOnDeploymentConfig (userWorkloadTracesCollection=otelcol...)      │   │
│   │  OpenTelemetryCollector/instance     ← stanza (applied by 02)         │   │
│   │  Secret/tracing-otlp-auth                                              │   │
│   │  ConfigMap/images-list               ← prereq (see below)             │   │
│   └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│   namespace: local-cluster                                                   │
│   ┌──────────────────────────────────────────────────────────────────────┐   │
│   │  ManagedClusterAddOn/multicluster-observability-addon (02)            │   │
│   │  ManifestWork/addon-multicluster-observability-addon-deploy-0          │   │
│   │      ├─ Namespace/mcoa-opentelemetry                                   │   │
│   │      ├─ Subscription/opentelemetry-product (OTel Operator install)    │   │
│   │      ├─ OpenTelemetryCollector/mcoa-instance ← THE RENDERED CR       │   │
│   │      └─ ClusterRole (klusterlet-work)                                  │   │
│   └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│   namespace: mcoa-opentelemetry                                              │
│   ┌──────────────────────────────────────────────────────────────────────┐   │
│   │  OpenTelemetryCollector/mcoa-instance         ← reconciled by OTel    │   │
│   │  Deployment/mcoa-instance-collector              Operator             │   │
│   │  Service/mcoa-instance-collector (4317, 4318)                          │   │
│   └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│   namespace: chainsaw-mcoa-traces-smoke (auto-managed by chainsaw)           │
│   ┌──────────────────────────────────────────────────────────────────────┐   │
│   │  MinIO                              ← 00                              │   │
│   │  TempoStack/smoke + components      ← 01                              │   │
│   │  Job/generate-traces (telemetrygen) ← 04                              │   │
│   │  Job/verify-traces (verification)   ← 05                              │   │
│   └──────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Prerequisites (NOT installed by this test)

The chainsaw test assumes the cluster is in a specific ready state. The
following must exist **before** running:

### Operators

1. **OpenShift** with cluster-admin access.
2. **cert-manager Operator for Red Hat OpenShift** (`stable-v1`).
3. **Red Hat build of OpenTelemetry** (`opentelemetry-product`, `stable`).
4. **Tempo Operator** (`tempo-product` `stable`; community `alpha` channel
   also works).
5. **OCM hub** (`ClusterManager` from
   `open-cluster-management-io/registration-operator`) with the `local-cluster`
   `ManagedCluster` registered, accepted, joined, and `Available=True`.

### MCOA addon-manager

Deployed from the upstream repo's `deploy/`:

```bash
git clone https://github.com/stolostron/multicluster-observability-addon
cd multicluster-observability-addon
make download-crds
# Note: deploy/crds/core.observatorium.io_observatoria.yaml may 404 — delete it
rm -f deploy/crds/core.observatorium.io_observatoria.yaml
oc apply --server-side --force-conflicts -f deploy/crds/
oc create ns open-cluster-management-observability || true
oc create ns open-cluster-management-global-set    || true
kustomize build deploy/ | oc apply --server-side --force-conflicts -f -
```

### MCOA-specific prereqs discovered during development

These are NOT obvious from MCOA's own README and were discovered empirically:

1. **`images-list` ConfigMap** in `open-cluster-management-observability`.
   MCOA reads it unconditionally even when only traces is enabled (the
   metrics handler runs regardless). In production this is supplied by MCO
   classic; for standalone:

   ```yaml
   apiVersion: v1
   kind: ConfigMap
   metadata:
     name: images-list
     namespace: open-cluster-management-observability
   data:
     prometheus_config_reloader: quay.io/prometheus-operator/prometheus-config-reloader:v0.83.0
     kube_rbac_proxy: quay.io/brancz/kube-rbac-proxy:v0.18.2
     obo_prometheus_rhel9_operator: quay.io/rhobs/obo-prometheus-operator:v0.83.0-rhobs1
     kube_state_metrics: registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.15.0
     node_exporter: quay.io/prometheus/node-exporter:v1.9.1
     prometheus: quay.io/prometheus/prometheus:v3.5.0
   ```

2. **`HostedCluster` CRD** (`hypershift.openshift.io/v1beta1`) must exist on
   the hub. MCOA's watcher controller waits for the cache to sync on this
   kind, even on non-hypershift clusters. A minimal stub CRD is enough.

3. **`ManagedCluster` vendor signal**. MCOA's traces capability is gated
   behind `IsOpenShiftVendor()`. Either label the cluster:

   ```bash
   oc label managedcluster local-cluster vendor=OpenShift --overwrite
   ```

   or use the dedicated e2e hook annotation MCOA exposes:

   ```bash
   oc annotate managedcluster local-cluster \
     mcoa-override-vendor=OpenShift --overwrite
   ```

   See `internal/addon/common/managedcluster.go` in MCOA upstream.

4. **`Placement` `global` in namespace `open-cluster-management-global-set`**
   with a `ManagedClusterSetBinding` for the `global` ClusterSet. MCOA's
   `ClusterManagementAddOn.spec.installStrategy.placements` references it:

   ```yaml
   apiVersion: cluster.open-cluster-management.io/v1beta2
   kind: ManagedClusterSetBinding
   metadata: { name: global, namespace: open-cluster-management-global-set }
   spec: { clusterSet: global }
   ---
   apiVersion: cluster.open-cluster-management.io/v1beta1
   kind: Placement
   metadata: { name: global, namespace: open-cluster-management-global-set }
   spec:
     clusterSets: [global]
     predicates:
       - requiredClusterSelector:
           labelSelector: {}
   ```

5. **`AddOnDeploymentConfig` `multicluster-observability-addon`** in
   `open-cluster-management-observability` with at minimum:

   ```yaml
   spec:
     customizedVariables:
       - name: userWorkloadTracesCollection
         value: opentelemetrycollectors.v1beta1.opentelemetry.io
   ```

   The default config shipped in MCOA's `deploy/resources/` enables all
   capabilities — strip to traces-only to avoid metrics/logs noise.

The Prow preset that hosts this test in CI is expected to provide the above.

## Running locally

```bash
# From repo root
chainsaw test --test-dir tests/e2e-mcoa/traces-smoke/ --config .chainsaw.yaml
```

For verbose run with logs of the rendered OTel Collector pod, and keeping
the namespace around for post-mortem inspection:

```bash
chainsaw test --test-dir tests/e2e-mcoa/traces-smoke/ \
              --config .chainsaw.yaml \
              --apply-timeout 30s \
              --assert-timeout 6m \
              --skip-delete
```

## Validated render shape

For documentation and to catch breakages on MCOA bumps, the rendered
`OpenTelemetryCollector` observed in MCOA v0.0.1 (chart `tracing-1.0.0`) is:

| Field                       | Value (rendered) |
|----------------------------|------------------|
| `metadata.name`            | `mcoa-instance` (always — chart hardcodes `mcoa-` prefix) |
| `metadata.namespace`       | `mcoa-opentelemetry` (always — chart hardcodes) |
| `metadata.labels.app`      | `tracing` |
| `metadata.labels.release`  | `multicluster-observability-addon` |
| `spec.managementState`     | `managed` (flipped from `unmanaged` in stanza) |
| `spec.mode`                | `deployment` |
| `spec.replicas`            | `1` (default) |
| `spec.upgradeStrategy`     | `automatic` (default) |
| OTel Operator-derived names | Deployment & Service `mcoa-instance-collector` |

If these names change in a future MCOA release, update both `03-assert-…yaml`
and `04-generate-traces.yaml` (collector endpoint).

## What we explicitly do NOT test here

- **MCO classic** (`MultiClusterObservability` CR). This smoke validates the
  MCOA layer in isolation; the MCO → MCOA → spoke flow is covered by ACM
  interop QE downstream.
- **Multi-cluster hub+spoke**. Single-cluster (`local-cluster` self-managed)
  is enough to validate the MCOA render contract. A second scenario can be
  added later under `tests/e2e-mcoa/traces-hub-spoke/`.
- **`Instrumentation` CR / auto-instrumentation**. Reserved for a separate
  `tests/e2e-mcoa/traces-instrumentation/` scenario.
- **mTLS to the Tempo gateway**. The smoke uses plaintext OTLP to the
  in-cluster distributor; gateway+RBAC variants live in
  `tests/e2e-disconnected/multitenancy/`.

## Tracking

- Jira: <https://redhat.atlassian.net/browse/TRACING-6088>
- MCOA repo: <https://github.com/stolostron/multicluster-observability-addon>
