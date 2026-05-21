# Local setup for `traces-smoke` on CRC

Steps to bring a stock CodeReady Containers (CRC) cluster to the "ready" state
this test assumes. Validated on `crc 2.59.0` (OpenShift 4.21.4, kube 1.34).

If your test target is a real OpenShift cluster (cluster-bot, ROSA, OSD),
sections §1–§5 still apply; §6 (TLS workaround) only matters on CRC.

---

## 1. Start CRC with enough resources

```bash
crc config set cpus 6
crc config set memory 32768   # MB
crc config set disk-size 80
crc start
eval $(crc oc-env)
oc login -u kubeadmin -p "$(crc console --credentials | awk '/-p/ {gsub(/.*-p /,""); print}')" \
  https://api.crc.testing:6443 --insecure-skip-tls-verify=true
```

Minimum tested: 6 CPU / 32 GB RAM / 80 GB disk. The full stack is tight on
CRC defaults (4 CPU / 9 GB).

## 2. Install operators via OperatorHub

```bash
cat <<'EOF' | oc apply -f -
apiVersion: v1
kind: Namespace
metadata: { name: cert-manager-operator }
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata: { name: cert-manager-operator, namespace: cert-manager-operator }
spec: { targetNamespaces: [cert-manager-operator] }
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata: { name: openshift-cert-manager-operator, namespace: cert-manager-operator }
spec:
  channel: stable-v1
  name: openshift-cert-manager-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
---
apiVersion: v1
kind: Namespace
metadata: { name: openshift-opentelemetry-operator }
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata: { name: openshift-opentelemetry-operator, namespace: openshift-opentelemetry-operator }
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata: { name: opentelemetry-product, namespace: openshift-opentelemetry-operator }
spec:
  channel: stable
  name: opentelemetry-product
  source: redhat-operators
  sourceNamespace: openshift-marketplace
EOF

# Tempo operator from community catalog — CRC OperatorHub UI works too.
# (Often already present on CRC images from previous sessions.)
```

Wait until CSVs reach `Succeeded`:

```bash
oc get csv -A | grep -E 'cert-manager|opentelemetry|tempo'
```

## 3. Install OCM hub + register the cluster as self-managed

```bash
# Hub
clusteradm init --wait --output-join-command-file /tmp/ocm-join.sh

# Self-join. Read the token from /tmp/ocm-join.sh and run the agent locally.
TOKEN=$(grep -oP 'eyJ[A-Za-z0-9_\.\-]+' /tmp/ocm-join.sh | head -1)
clusteradm join --hub-token "$TOKEN" \
                --hub-apiserver https://api.crc.testing:6443 \
                --cluster-name local-cluster \
                --wait --force-internal-endpoint-lookup
# (Exits with "unexpected watch event received" — benign; CSR was created.)

# §6 below: patch the bootstrap kubeconfig to skip TLS verify before approval.
# Then accept:
clusteradm accept --clusters local-cluster --wait
```

Verify:

```bash
oc get managedclusters
# NAME            HUB ACCEPTED   MANAGED CLUSTER URLS           JOINED   AVAILABLE
# local-cluster   true           https://api.crc.testing:6443   True     True
```

## 4. MCOA-specific prereqs

```bash
# 4a. images-list ConfigMap (required by metrics path even for traces-only).
oc create ns open-cluster-management-observability || true
cat <<'EOF' | oc apply -f -
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
EOF

# 4b. HostedCluster CRD stub (MCOA's watcher requires this kind in the
#     scheme; values are never read on non-hypershift clusters).
cat <<'EOF' | oc apply -f -
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: { name: hostedclusters.hypershift.openshift.io }
spec:
  group: hypershift.openshift.io
  names: { kind: HostedCluster, listKind: HostedClusterList, plural: hostedclusters, singular: hostedcluster, shortNames: [hc, hcs] }
  scope: Namespaced
  versions:
    - name: v1beta1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:   { type: object, x-kubernetes-preserve-unknown-fields: true }
            status: { type: object, x-kubernetes-preserve-unknown-fields: true }
      subresources: { status: {} }
EOF

# 4c. Vendor signal on the ManagedCluster.
oc label managedcluster local-cluster vendor=OpenShift --overwrite
oc annotate managedcluster local-cluster mcoa-override-vendor=OpenShift --overwrite

# 4d. ManagedClusterSetBinding + Placement that MCOA's CMA references.
oc create ns open-cluster-management-global-set || true
cat <<'EOF' | oc apply -f -
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
EOF
```

## 5. Deploy MCOA standalone

```bash
git clone https://github.com/stolostron/multicluster-observability-addon
cd multicluster-observability-addon
make download-crds
rm -f deploy/crds/core.observatorium.io_observatoria.yaml   # upstream 404 placeholder
oc apply --server-side --force-conflicts -f deploy/crds/
kustomize build deploy/ | oc apply --server-side --force-conflicts -f -

# Trim AddOnDeploymentConfig to traces only (avoids spurious metrics/logs reconcile churn).
cat <<'EOF' | oc apply --server-side --force-conflicts -f -
apiVersion: addon.open-cluster-management.io/v1alpha1
kind: AddOnDeploymentConfig
metadata:
  name: multicluster-observability-addon
  namespace: open-cluster-management-observability
spec:
  customizedVariables:
    - name: userWorkloadTracesCollection
      value: opentelemetrycollectors.v1beta1.opentelemetry.io
EOF
```

Wait for the addon-manager:

```bash
oc -n open-cluster-management-observability get deploy multicluster-observability-addon-manager -w
```

## 6. CRC-only: keep the OCM TLS path alive

CRC's apiserver uses a self-signed cert. The OCM bootstrap kubeconfig does NOT
include the CA, so both `klusterlet-registration-agent` and
`klusterlet-work-agent` fail with:

```
tls: failed to verify certificate: x509: certificate signed by unknown authority
```

The fix is to inject `insecure-skip-tls-verify: true` into the
`hub-kubeconfig-secret`. The registration agent rotates this kubeconfig
periodically and strips the flag, so the patch needs to be reapplied.

**One-shot patch** (use before running clusteradm accept, and any time the
agents start crash-looping):

```bash
oc get secret -n open-cluster-management-agent hub-kubeconfig-secret \
  -o jsonpath='{.data.kubeconfig}' | base64 -d \
  | sed 's|    server: https://api.crc.testing:6443|    insecure-skip-tls-verify: true\n    server: https://api.crc.testing:6443|' \
  | base64 -w0 \
  | xargs -I{} oc patch secret -n open-cluster-management-agent \
      hub-kubeconfig-secret --type=merge -p '{"data":{"kubeconfig":"{}"}}'

oc delete pod -n open-cluster-management-agent -l app=klusterlet-work-agent
oc delete pod -n open-cluster-management-agent -l app=klusterlet-registration-agent
```

**Watchdog loop** (run in another terminal — patches on every rotation):

```bash
while true; do
  KC=$(oc get secret -n open-cluster-management-agent hub-kubeconfig-secret \
       -o jsonpath='{.data.kubeconfig}' 2>/dev/null | base64 -d 2>/dev/null || true)
  if [[ -n "$KC" ]] && ! echo "$KC" | grep -q insecure-skip-tls-verify; then
    PATCHED=$(echo "$KC" | sed 's|    server: https://api.crc.testing:6443|    insecure-skip-tls-verify: true\n    server: https://api.crc.testing:6443|')
    B64=$(echo "$PATCHED" | base64 -w0)
    oc patch secret -n open-cluster-management-agent hub-kubeconfig-secret \
      --type=merge -p "{\"data\":{\"kubeconfig\":\"$B64\"}}" >/dev/null 2>&1
    oc delete pod -n open-cluster-management-agent -l app=klusterlet-work-agent >/dev/null 2>&1
    echo "$(date +%H:%M:%S) repatched after rotation"
  fi
  sleep 10
done
```

A real OCP cluster (cluster-bot, ROSA, OSD productivo) does not have this
problem because the apiserver cert is signed by a CA that the OCM bootstrap
kubeconfig trusts by default.

## 7. Sanity check before running chainsaw

```bash
oc get managedcluster local-cluster -o jsonpath='{.status.conditions[?(@.type=="ManagedClusterConditionAvailable")].status}'
# True

oc get mca -n local-cluster multicluster-observability-addon
# Should exist (auto-created by placement). Conditions take a few cycles to
# settle to ManifestApplied=True after the stanza in step 02 is applied.

oc get manifestwork -n local-cluster addon-multicluster-observability-addon-deploy-0 \
  -o jsonpath='{.spec.workload.manifests[*].kind}'
# After step 02: should include OpenTelemetryCollector, Subscription,
# OperatorGroup, Namespace × 2, ClusterRole.
```

If all of the above hold, run the test:

```bash
cd <distributed-tracing-qe-root>
chainsaw test --test-dir tests/e2e-mcoa/traces-smoke/ --config .chainsaw.yaml
```
