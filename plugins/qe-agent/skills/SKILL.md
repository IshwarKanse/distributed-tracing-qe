---
name: qe-agent
description: Use this skill to analyze failing CI tests for Red Hat OpenShift Distributed Tracing (OpenTelemetry Operator, Tempo Operator, Tracing UI console plugin), rerun the specific failing tests, diagnose whether the failure is a product bug or a test that needs fixing, apply fixes to test source files when needed, and export results to the artifact directory. Trigger whenever failing JUnit XML results are present in $SHARED_DIR/qe-agent/ or when an engineer asks to debug, rerun, or fix failing distributed tracing QE tests.
---

# RHOSDT QE Agent — Test Failure Triage and Fix

This skill drives an agentic loop that takes failing CI test results, reruns the failing tests, determines root cause (product bug vs broken test), and either fixes the test or writes a structured bug report.

## Test Infrastructure Overview

Three test suites are supported. The JUnit report name prefix tells you which suite failed:

| JUnit prefix | Suite | Framework | Repo |
|---|---|---|---|
| `junit_otel_*` | OpenTelemetry Operator | chainsaw | `https://github.com/IshwarKanse/opentelemetry-operator` |
| `junit_tempo_*` | Tempo Operator | chainsaw | `https://github.com/grafana/tempo-operator` |
| `junit_distributed-tracing-console-plugin*` | Tracing UI (Cypress) | Cypress/npm | `https://github.com/openshift/distributed-tracing-console-plugin` |

---

## Step 0 — Read Setup Context and Fetch the Step Script

Read `${SHARED_DIR}/qe-agent/setup-context.json`. The test step writes it at exit time with two fields:

```json
{
  "step_script_ref": "distributed-tracing/tests/opentelemetry/downstream/distributed-tracing-tests-opentelemetry-downstream-commands.sh",
  "env": {
    "MULTISTAGE_PARAM_OVERRIDE_OTEL_TESTS_BRANCH": "rhosdt-3.9"
  }
}
```

- `step_script_ref` — path relative to `ci-operator/step-registry/` in the openshift/release repo
- `env` — runtime env var values that were injected at job time and are needed to reproduce setup (e.g. branch names, image refs); most steps have an empty `env`

Construct the raw GitHub URL and fetch the script:

```text
https://raw.githubusercontent.com/openshift/release/main/ci-operator/step-registry/<step_script_ref>
```

Read the script carefully. It is divided into two logical sections:
1. **Setup** — everything before the `chainsaw test` or `npx cypress run` commands: cloning repos, `oc apply`, `kubectl create`, CSV patches, `make build`, env variable setup
2. **Test execution** — the `chainsaw test` / `npx cypress run` invocations themselves

## Step 0b — Re-establish the Test Environment

Export any env vars from the `env` field, then **run the setup section of the fetched script** — the commands up to (but not including) the first `chainsaw test` or `npx cypress run` invocation.

Key adaptations when running setup commands from the script:
- `cp -R /tmp/opentelemetry-operator /tmp/opentelemetry-tests` — the source image mount `/tmp/opentelemetry-operator` does not exist in the qe-agent pod. Replace with a `git clone` of the upstream repo at the same commit. Use `git log` from the running cluster's operator deployment to identify the version if the commit is unknown.
- `kubectl create -f <url>` for CRDs — change to `kubectl apply -f <url>` since `create` fails if the CRD already exists from the original test run; `apply` is idempotent.
- CSV patches (`oc patch csv ...`) — the operator is already installed and already patched from the original test step. Skip these unless the test you are rerunnig specifically requires a freshly patched CSV. Check `oc get csv -n <namespace>` to verify env vars are already set.
- `unset NAMESPACE` — always run this before chainsaw to avoid conflicts.

After setup, `cd` into the repo directory and proceed with Steps 1–6.

If `setup-context.json` does not exist, infer the suite from the JUnit file name prefix (`junit_otel_*` → OpenTelemetry, `junit_tempo_*` → Tempo, `junit_distributed-tracing-console-plugin*` → Tracing UI) and skip the rerun — proceed directly to diagnosis from the JUnit content and cluster state.

## Step 1 — Parse JUnit XMLs and Identify Failures

Read all `*.xml` files from `${SHARED_DIR}/qe-agent/` (recurse into sub-directories — test steps may nest results).

For each XML file, extract:
- **Suite name** (`name` attribute on `<testsuite>`)
- **Failed test cases**: `<testcase>` elements that contain a `<failure>` or `<error>` child
- **Failure message**: the `message` attribute and text body of `<failure>`/`<error>`
- **Stack trace / details**: the full text content of the failure element

Group failures by suite so you process each operator's failures together.

If `${SHARED_DIR}/qe-agent/` does not exist or contains no XML files, exit with a clear message — the test steps did not run or produced no results.

---

## Step 2 — Locate Test Source Files

For **chainsaw** suites (OpenTelemetry, Tempo):

The JUnit test case name usually matches the folder name under the test directory. For example, a failing test named `e2e/targetallocator` corresponds to `tests/e2e/targetallocator/`. Inside that folder look for:
- `chainsaw-test.yaml` — the test definition (steps, assertions)
- `*.yaml` resource manifests applied during the test
- `assert.yaml` / `error.yaml` — explicit assertion files

To find the right folder when the name mapping is unclear, use `find <repo-root>/tests -type d -name "<test-name>"`.

Once located, record this as `TEST_DIR` (e.g. `tests/e2e/targetallocator`). The rerun commands in Step 3 reference `${TEST_DIR}` directly.

For **Cypress** suites (Tracing UI):

The failing test name maps to a `describe` + `it` block inside `.cy.js` or `.cy.ts` files under `tests/cypress/e2e/`. Use `grep -r "<test-name>"` to locate the spec file.

The repo location and how it was set up is determined by the fetched step script (Step 0b). By the time you reach Step 2, the setup commands from that script have already been run:
- **Upstream tests**: the step script does `cp -R /tmp/<operator> /tmp/<tests>` — since the source is an image mount that does not exist in the qe-agent pod, Step 0b substitutes this with a `git clone`. The repo is at the destination path shown in the script (e.g., `/tmp/opentelemetry-tests`, `/tmp/tempo-tests`).
- **Downstream / stage tests**: the step script does `git clone <url> /tmp/<tests>` directly. Step 0b runs this clone. The repo is at the path shown in the script.

Use the destination path from the step script as your repo root — do not guess or check `/tmp/` broadly.

---

## Step 3 — Rerun the Failing Tests

Rerun only the specific failing tests, not the entire suite, to save time and keep the rerun focused.

### Cleaning up test resources before each rerun

Chainsaw reruns use `--skip-delete` so resources remain on the cluster after the test finishes — this lets you inspect them and understand why a test failed. However, because resources persist, **you must clean up before running the same test again**, otherwise the next run will collide with leftover state.

`kubectl delete -f <test-folder>/` is **not sufficient** — chainsaw tests create resources in multiple ways beyond static YAML files:
- Script steps that run `kubectl apply` / `oc apply` dynamically
- Resources created by the operator itself in response to CRs (e.g., a `TempoStack` CR triggers the operator to create Deployments, Services, ConfigMaps)
- Cluster-scoped resources (ClusterRoles, ClusterRoleBindings, CRDs) created by test setup scripts
- Chainsaw's own test namespaces — chainsaw automatically creates a namespace per test with a `chainsaw-` prefix (e.g., `chainsaw-targetallocator`, `chainsaw-tls-profile`)

**Reliable cleanup approach:**

```bash
# 1. Find and delete the chainsaw test namespace(s) for this test
#    Chainsaw prefixes namespaces with "chainsaw-" followed by the test name
kubectl get namespace | grep "chainsaw-<test-name>"
kubectl delete namespace chainsaw-<test-name> --ignore-not-found=true
kubectl wait --for=delete namespace/chainsaw-<test-name> --timeout=5m 2>/dev/null || true
```

Deleting the namespace cascades and removes all namespaced resources the test created — CRs, operator-managed Deployments, Services, ConfigMaps — regardless of how they were created (YAML, script, or operator reconciliation).

```bash
# 2. Read chainsaw-test.yaml to identify cluster-scoped resources created by the test
#    (ClusterRoles, ClusterRoleBindings, CRDs, etc.) and delete them explicitly
kubectl delete clusterrole,clusterrolebinding -l app.kubernetes.io/managed-by=chainsaw --ignore-not-found=true

# 3. If the test's script steps created additional resources (visible in chainsaw-test.yaml
#    script blocks), identify and delete those resources manually
```

```bash
# 4. Verify the namespace is gone before rerunning
kubectl get namespace | grep "chainsaw-<test-name>" && echo "WARNING: namespace still exists" || echo "Clean"
```

Read `chainsaw-test.yaml` for the failing test before cleanup — it tells you what namespaces, CRs, and cluster-scoped resources the test creates, which guides what to delete.

### OpenTelemetry Operator
```bash
# Use TEST_DIR resolved in Step 2 (e.g. tests/e2e/targetallocator)
# Read the fetched step script (from Step 0) to check whether --selector is used in the chainsaw invocation
# If the script passes --selector <value>, include the same flag in the rerun
CHAINSAW_CMD="chainsaw test --skip-delete --quiet --report-name junit_rerun_otel --report-path ${ARTIFACT_DIR} --report-format XML"
# Add selector if the original script used one (OTEL only — check the step script)
# CHAINSAW_CMD+=" --selector <selector-from-script>"
CHAINSAW_CMD+=" --test-dir ${TEST_DIR}"
eval "$CHAINSAW_CMD"
```

### Tempo Operator
```bash
# Use TEST_DIR resolved in Step 2 (e.g. tests/e2e-openshift/tls-profile)
# Tempo always uses --config .chainsaw-openshift.yaml (visible in the fetched step script)
chainsaw test \
  --skip-delete \
  --config .chainsaw-openshift.yaml \
  --quiet \
  --report-name "junit_rerun_tempo" \
  --report-path "${ARTIFACT_DIR}" \
  --report-format XML \
  --test-dir "${TEST_DIR}"
```

### Tracing UI (Cypress)
```bash
export NO_COLOR=1
export CYPRESS_CACHE_FOLDER=/tmp/Cypress
npx cypress run \
  --browser chrome \
  --headless \
  --spec "tests/cypress/e2e/<spec-file>"
```

After the rerun, read the fresh JUnit XML (saved to `$ARTIFACT_DIR`) to check whether the test is:
- **Consistently failing** — same failure, same message → proceed to Step 4 (diagnose)
- **Passed on first rerun** — possible flakiness → do not stop here; run the test 3 more times (4 total reruns) to confirm and locate where the flakiness occurs (see below)
- **Fixed by environment reset** — only relevant if the test setup was stale

### Flakiness confirmation loop

If the test passes on the first rerun, run it 3 more times sequentially. Clean up test resources before each run (see above). Use a unique `--report-name` per run so the XMLs don't overwrite each other:

```bash
for i in 2 3 4; do
  # Delete the chainsaw test namespace to cascade all resources (including operator-managed ones)
  kubectl delete namespace chainsaw-<test-name> --ignore-not-found=true
  kubectl wait --for=delete namespace/chainsaw-<test-name> --timeout=5m 2>/dev/null || true

  # Delete any cluster-scoped resources the test created
  kubectl delete clusterrole,clusterrolebinding -l app.kubernetes.io/managed-by=chainsaw --ignore-not-found=true

  chainsaw test \
    --skip-delete \
    --quiet \
    --report-name "junit_rerun_otel_run${i}" \
    --report-path "${ARTIFACT_DIR}" \
    --report-format XML \
    --test-dir "${TEST_DIR}"
done
```

For Cypress:
```bash
for i in 2 3 4; do
  npx cypress run \
    --browser chrome \
    --headless \
    --spec "tests/cypress/e2e/<spec-file>" \
    --reporter junit \
    --reporter-options "mochaFile=${ARTIFACT_DIR}/junit_rerun_cypress_run${i}.xml"
done
```

After all 4 runs, count how many passed vs failed. Record the pass/fail pattern (e.g., `PFPP`, `PPFP`). Then inspect the test source:
- Look for missing `wait` blocks between an action and an assertion
- Look for very short `timeout` values in chainsaw steps (e.g., `timeout: 30s` where the operator may take longer)
- Look for assertions that depend on ordering of concurrent resources
- For Cypress: look for missing `cy.wait()` or `cy.intercept()` before asserting UI state

If the failure is reproducible even 1 out of 4 runs, classify as `FLAKY` and proceed to Step 5c to fix it.

---

## Step 4 — Diagnose: Product Bug vs Test Issue

Read the failure message, rerun output, and test source files together. Gather additional cluster evidence using `oc`:

```bash
# Pod logs from the operator namespace
oc logs -n openshift-opentelemetry-operator deploy/opentelemetry-operator-controller-manager --tail=100
oc logs -n openshift-tempo-operator deploy/tempo-operator-controller-manager --tail=100

# Recent events in the test namespace
oc get events -n <test-namespace> --sort-by='.lastTimestamp' | tail -30

# Check if expected CRDs/resources exist
oc get crd | grep -E 'opentelemetry|tempo|jaeger'
```

### Product Bug indicators
Classify as `PRODUCT_BUG` when the evidence shows the operator or operand itself misbehaved:
- Operator pod in `CrashLoopBackOff` or `OOMKilled`
- Operand resource (e.g., `TempoStack`, `OpenTelemetryCollector`) stuck in an error state not caused by the test YAML
- API object that the operator should have created is missing
- Image pull failure for an operand image referenced in the CSV
- CRD validation error rejecting a valid CR that worked in a prior release
- Timeout waiting for operator reconciliation when the operator logs show no activity

### Test Issue indicators
Classify as `TEST_ISSUE` when the test itself is wrong or stale:
- Hardcoded version string or image tag in the test YAML that doesn't match the currently installed operator version
- Wrong namespace name in an assertion (namespace changed between releases)
- Race condition: the test asserts a resource state before the operator has had time to act — look for very short `timeout` values in chainsaw steps or missing `wait` blocks
- Missing prerequisite in the test setup (e.g., a CRD that must be installed before the test runs but isn't part of the test's `setup` steps)
- Assertion checks a field or value that changed in the operator API (e.g., a renamed status condition)
- Cypress test references a UI element selector that changed in the console plugin

When genuinely ambiguous, gather more cluster evidence before deciding. Explain your reasoning explicitly in the output.

---

## Step 5a — If TEST_ISSUE: Fix and Export

Apply the **minimal** change to make the test correct. Avoid refactoring or improving unrelated parts of the test — a focused, small diff is easier to review and merge.

**For chainsaw tests:**
- Edit `chainsaw-test.yaml`, `assert.yaml`, resource manifests, or other YAML files in the test folder
- Common fixes: update image/version references, fix namespace, add a `wait` step before an assertion, correct a changed field name in assertions

**For Cypress tests:**
- Edit the `.cy.js` / `.cy.ts` spec file
- Common fixes: update CSS selector, fix a changed route or API endpoint, add a `cy.wait()` for async operations

After editing, copy only the changed files to `${ARTIFACT_DIR}/test-fixes/` **preserving the directory path relative to the repo root**:

```bash
# Example: tests/e2e/targetallocator/chainsaw-test.yaml was fixed
dest="${ARTIFACT_DIR}/test-fixes/tests/e2e/targetallocator"
mkdir -p "${dest}"
cp tests/e2e/targetallocator/chainsaw-test.yaml "${dest}/"
```

Write a `${ARTIFACT_DIR}/test-fixes/CHANGES.md` using this structure:

```markdown
# Test Fix Summary

## Failing test
<suite name> / <test case name>

## Root cause
<one paragraph explaining what was wrong in the test and why>

## Fix applied
<what was changed, which files, what specifically>

## Files changed
- `tests/e2e/<folder>/chainsaw-test.yaml`

## Verification
Rerun result after fix: [PASS / FAIL / not re-verified]
```

---

## Step 5c — If FLAKY: Fix and Export

Apply the minimal change that eliminates the race or timing condition. Do not suppress flakiness with blanket retries — find and fix the root cause.

**For chainsaw tests:**
- If a step asserts state immediately after a resource is applied, add an explicit `wait` step using `chainsaw wait` or a `sleep` in a script step before the assertion
- If a `timeout` is too short, increase it to give the operator time to reconcile (common fix: `30s` → `2m`)
- If two resources are created concurrently and one depends on the other, reorder the steps to create the dependency first

Example — adding a wait step in `chainsaw-test.yaml`:
```yaml
- name: Wait for collector to be ready before asserting
  wait:
    apiVersion: opentelemetry.io/v1alpha1
    kind: OpenTelemetryCollector
    name: otel-collector
    timeout: 2m
    for:
      condition:
        name: Ready
        value: "True"
```

**For Cypress tests:**
- Add `cy.intercept()` to wait for the relevant API call before asserting
- Use `cy.findByText(...).should('be.visible')` with a custom timeout rather than asserting immediately
- Replace `cy.wait(<ms>)` (fixed-time sleep) with a condition-based wait when possible

After editing, copy changed files to `${ARTIFACT_DIR}/test-fixes/` with the same structure as Step 5a. Write `CHANGES.md` using the same template, and include the pass/fail pattern from the 4 rerun runs as evidence.

---

## Step 5b — If PRODUCT_BUG: Write Bug Report

Do not attempt to fix the operator code. Instead, write `${ARTIFACT_DIR}/bug-report.md`:

```markdown
# Product Bug Report

## Summary
<one-sentence description of the bug>

## Affected component
- Operator: <OpenTelemetry Operator / Tempo Operator / Tracing UI>
- Namespace: <openshift-opentelemetry-operator | openshift-tempo-operator>
- Failing test: <suite / test case>

## Reproduction
1. <Step-by-step reproduction based on what the test does>

## Observed behavior
<What happened — include the exact failure message from JUnit>

## Expected behavior
<What should have happened>

## Evidence
### Operator logs
```text
<relevant log lines>
```

### Cluster events
```text
<relevant events>
```

### JUnit failure message
```text
<failure text from XML>
```

## Suggested severity
<Critical / Major / Minor — based on whether this blocks a release gate>
```

---

## Step 6 — Write Analysis Summary

Always write `${ARTIFACT_DIR}/qe-agent-analysis.md` as the final step, regardless of outcome:

```markdown
# QE Agent Analysis

## Failed Tests
| Suite | Test Case | JUnit File |
|---|---|---|
| <suite> | <test-case> | <xml-filename> |

## Rerun Result
<still failing / passed on rerun (flaky) / not rerun>

## Diagnosis
**<PRODUCT_BUG | TEST_ISSUE | FLAKY>**

<Two to three sentences explaining the reasoning. Reference specific log lines, error messages, or test YAML fields that led to this conclusion.>

## Rerun Summary
| Run | Result |
|---|---|
| Original CI run | FAIL |
| Rerun 1 | PASS / FAIL |
| Rerun 2 | PASS / FAIL |
| Rerun 3 | PASS / FAIL |
| Rerun 4 | PASS / FAIL |

## Outcome
<If TEST_ISSUE>: Test fix applied. Changed files in `${ARTIFACT_DIR}/test-fixes/`. See `CHANGES.md` for details.
<If PRODUCT_BUG>: Bug report written to `${ARTIFACT_DIR}/bug-report.md`.
<If FLAKY>: Flaky test confirmed (pattern: <e.g. PFPP>). Fix applied to `${ARTIFACT_DIR}/test-fixes/`. See `CHANGES.md` for root cause and fix details.
```

---

## Notes for CI context

- The cluster is already provisioned and the operator is already installed — do not reinstall the operator
- `$KUBECONFIG` is set and points to the test cluster
- `oc` and `kubectl` are available
- `chainsaw` is available in PATH
- The test repo is set up by Step 0b using commands from the fetched step script — the repo path is the destination shown in the script (e.g., `/tmp/opentelemetry-tests`, `/tmp/tempo-tests`). The qe-agent runs in a fresh pod so `/tmp/` is always empty at start; Step 0b populates it
- All output files must go to `$ARTIFACT_DIR` (uploaded to GCS by the sidecar) or `$SHARED_DIR` (accessible to other steps)
- This step runs `best_effort: true` — always exit 0 even if analysis is incomplete
