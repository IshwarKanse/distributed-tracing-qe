#!/bin/bash
set -e

# IIB index images per OCP version from the release payload
# Format: "ocp_version:iib_tag"
IIB_ENTRIES=(
    "v4.12:1107456"
    "v4.14:1107462"
    "v4.16:1107458"
    "v4.17:1107453"
    "v4.18:1107454"
    "v4.19:1107460"
    "v4.20:1107455"
    "v4.21:1107459"
    "v4.22:1107457"
)

IIB_REGISTRY="brew.registry.redhat.io/rh-osbs/iib"
OPERATOR_NAMESPACE="openshift-opentelemetry-operator"
MARKETPLACE_NAMESPACE="openshift-marketplace"
CATALOGSOURCE_NAME="otel-registry"
SUBSCRIPTION_NAME="opentelemetry-product"
OPERATOR_PACKAGE="opentelemetry-product"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

function log_info() {
    echo -e "\n[INFO] $*"
}

function log_error() {
    >&2 echo -e "\n[ERROR] $*"
}

function exit_error() {
    log_error "$*"
    exit 1
}

function get_iib_tag() {
    local target_version="$1"
    for entry in "${IIB_ENTRIES[@]}"; do
        local version="${entry%%:*}"
        local tag="${entry##*:}"
        if [[ "$version" == "$target_version" ]]; then
            echo "$tag"
            return 0
        fi
    done
    return 1
}

function delete_operator_namespace() {
    log_info "Deleting namespace $OPERATOR_NAMESPACE if it exists..."
    if oc get namespace "$OPERATOR_NAMESPACE" &>/dev/null; then
        # Delete the subscription first to avoid finalizer issues
        oc delete subscription --all -n "$OPERATOR_NAMESPACE" --ignore-not-found 2>/dev/null || true
        # Delete the CSV
        oc delete csv --all -n "$OPERATOR_NAMESPACE" --ignore-not-found 2>/dev/null || true
        # Delete the operator group
        oc delete operatorgroup --all -n "$OPERATOR_NAMESPACE" --ignore-not-found 2>/dev/null || true
        # Delete the namespace
        oc delete namespace "$OPERATOR_NAMESPACE" --wait=true --timeout=120s
        log_info "Namespace $OPERATOR_NAMESPACE deleted."
    else
        log_info "Namespace $OPERATOR_NAMESPACE does not exist, skipping deletion."
    fi
}

function delete_catalogsource() {
    log_info "Deleting CatalogSource $CATALOGSOURCE_NAME if it exists..."
    oc delete catalogsource "$CATALOGSOURCE_NAME" -n "$MARKETPLACE_NAMESPACE" --ignore-not-found
}

function create_catalogsource() {
    local iib_tag=$1
    local iib_image="${IIB_REGISTRY}:${iib_tag}"

    log_info "Creating CatalogSource $CATALOGSOURCE_NAME with image $iib_image..."
    cat <<EOF | oc apply -f -
apiVersion: operators.coreos.com/v1alpha1
kind: CatalogSource
metadata:
  name: ${CATALOGSOURCE_NAME}
  namespace: ${MARKETPLACE_NAMESPACE}
spec:
  sourceType: grpc
  image: ${iib_image}
  displayName: OpenTelemetry Stage Registry
  publisher: Red Hat
  updateStrategy:
    registryPoll:
      interval: 15m
EOF
}

function wait_for_catalogsource_ready() {
    log_info "Waiting for CatalogSource $CATALOGSOURCE_NAME pod to be running..."
    local retries=60
    local wait_seconds=5

    for ((i=1; i<=retries; i++)); do
        local pod_name
        pod_name=$(oc get pods -n "$MARKETPLACE_NAMESPACE" -l "olm.catalogSource=${CATALOGSOURCE_NAME}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

        if [[ -n "$pod_name" ]]; then
            local pod_status
            pod_status=$(oc get pod "$pod_name" -n "$MARKETPLACE_NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)
            if [[ "$pod_status" == "Running" ]]; then
                log_info "CatalogSource pod $pod_name is running."
                return 0
            fi
            echo "  Attempt $i/$retries: Pod $pod_name status is '$pod_status', waiting..."
        else
            echo "  Attempt $i/$retries: CatalogSource pod not found yet, waiting..."
        fi
        sleep "$wait_seconds"
    done

    exit_error "CatalogSource pod did not reach Running state within $((retries * wait_seconds)) seconds."
}

function install_operator() {
    log_info "Creating namespace $OPERATOR_NAMESPACE..."
    oc create namespace "$OPERATOR_NAMESPACE" || true

    log_info "Creating OperatorGroup in $OPERATOR_NAMESPACE..."
    cat <<EOF | oc apply -f -
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: opentelemetry-operator-group
  namespace: ${OPERATOR_NAMESPACE}
spec: {}
EOF

    log_info "Creating Subscription for $OPERATOR_PACKAGE..."
    cat <<EOF | oc apply -f -
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: ${SUBSCRIPTION_NAME}
  namespace: ${OPERATOR_NAMESPACE}
spec:
  channel: stable
  installPlanApproval: Automatic
  name: ${OPERATOR_PACKAGE}
  source: ${CATALOGSOURCE_NAME}
  sourceNamespace: ${MARKETPLACE_NAMESPACE}
EOF

    log_info "Waiting for operator deployment to be available..."
    local retries=60
    local wait_seconds=10

    for ((i=1; i<=retries; i++)); do
        local available
        available=$(oc get deployment opentelemetry-operator-controller-manager -n "$OPERATOR_NAMESPACE" -o jsonpath='{.status.availableReplicas}' 2>/dev/null || true)
        if [[ "$available" == "1" ]]; then
            log_info "Operator deployment is available."
            return 0
        fi
        echo "  Attempt $i/$retries: Operator not ready yet, waiting..."
        sleep "$wait_seconds"
    done

    exit_error "Operator deployment did not become available within $((retries * wait_seconds)) seconds."
}

function run_verification_script() {
    log_info "Running otel-images-and-version-check.sh..."
    bash "${SCRIPT_DIR}/otel-images-and-version-check.sh"
}

function cleanup_test_pods() {
    log_info "Cleaning up test pods in default namespace..."
    # Clean up pods created by the verification script (random-name-*)
    for pod in $(oc get pods -n default -o name 2>/dev/null | grep "random-name-" || true); do
        oc delete "$pod" -n default --ignore-not-found 2>/dev/null || true
    done
}

# Determine which OCP version to use based on the cluster
function get_cluster_ocp_version() {
    local version
    version=$(oc version -o json 2>/dev/null | python3 -c "import sys,json; v=json.load(sys.stdin)['openshiftVersion']; print('.'.join(v.split('.')[:2]))" 2>/dev/null || true)
    echo "$version"
}

# Main
echo "=============================================="
echo " OpenTelemetry IIB Stage Verification"
echo "=============================================="

cluster_version=$(get_cluster_ocp_version)
if [[ -n "$cluster_version" ]]; then
    log_info "Detected cluster OCP version: $cluster_version"
fi

# Build list of versions to test
if [[ $# -ge 1 ]]; then
    versions=("$@")
else
    versions=()
    for entry in "${IIB_ENTRIES[@]}"; do
        versions+=("${entry%%:*}")
    done
fi

passed=0
failed=0
failed_versions=""

for ocp_version in "${versions[@]}"; do
    iib_tag=$(get_iib_tag "$ocp_version" || true)
    if [[ -z "$iib_tag" ]]; then
        log_error "No IIB tag found for OCP version $ocp_version, skipping."
        continue
    fi

    echo
    echo "=============================================="
    echo " Testing OCP $ocp_version (IIB tag: $iib_tag)"
    echo "=============================================="

    # Step 1: Delete operator namespace
    delete_operator_namespace

    # Step 2: Delete existing catalogsource and create new one
    delete_catalogsource
    create_catalogsource "$iib_tag"

    # Step 3: Wait for catalog pod to be running
    wait_for_catalogsource_ready

    # Step 4: Install the operator
    install_operator

    # Step 5: Run verification script
    if run_verification_script; then
        log_info "OCP $ocp_version (IIB: $iib_tag): PASSED"
        passed=$((passed + 1))
    else
        log_error "OCP $ocp_version (IIB: $iib_tag): FAILED"
        failed=$((failed + 1))
        failed_versions="$failed_versions $ocp_version"
    fi

    # Cleanup test pods
    cleanup_test_pods
done

# Final cleanup
delete_operator_namespace
delete_catalogsource

echo
echo "=============================================="
echo " Verification Summary"
echo "=============================================="
echo "  Passed: $passed"
echo "  Failed: $failed"
if [[ -n "$failed_versions" ]]; then
    echo "  Failed versions:$failed_versions"
fi
echo "=============================================="

if [[ $failed -gt 0 ]]; then
    exit 1
fi
