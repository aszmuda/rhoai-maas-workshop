# AGENTS.md — Repository Guide for AI Coding Agents

This document provides essential architectural context, project conventions, environment details, and execution guidelines for AI coding agents (such as JetBrains Junie, OpenAI Codex, Claude Code, and Cursor) interacting with the `rhoai-maas-workshop` repository.

---

## 1. Project Overview & Scope

The **`rhoai-maas-workshop`** repository contains workshop materials, Kubernetes/OpenShift Kustomize manifests, API specifications, simulated infrastructure services, and interactive Jupyter notebooks demonstrating **Models-as-a-Service (MaaS)** capabilities on **Red Hat OpenShift AI (RHOAI)**.

### Core Capabilities Demonstrated
- **Model Deployment & Lifecycle**: Deploying LLMs (e.g., `gpt-oss-20b`, `qwen3-06b`) served via vLLM with GPU acceleration using KServe / OpenDataHub custom resources.
- **MaaS Governance CRDs**: Managing access control, model registration, and tiered quotas via:
  - `MaaSModelRef`: Registers model endpoints and links to underlying inference services.
  - `MaaSAuthPolicy`: Defines group- and user-level access controls using OpenShift user identities.
  - `MaaSSubscription`: Tiered subscription definitions (e.g., `gpt-oss-20b-free`, `gpt-oss-20b-premium`) enforcing token rate limits (e.g. 100 tokens/min vs 5000 tokens/min) and priorities.
- **External Model Providers**: Integrating external LLM endpoints (e.g., OpenAI, remote inference clusters) via `ExternalModel` resources with weighted routing, credential secrets, and Istio TLS DestinationRules.
- **API Management & Ingress Gateways**: Defining the MaaS billing and management API (`maas-api/openapi3.yaml`), supporting OpenShift user tokens and minted API keys (`sk-oai-...`).
- **External Metering & Rate-Limiting**: Simulating usage-based quota enforcement and balance deduction via an OpenMeter-compatible simulator HTTP service and Envoy `PayloadProcessorConfig` plugins.
- **Interactive User Experience**: Providing a guided Jupyter notebook demonstrating the complete end-to-end user workflow: cluster authentication, model discovery, subscription inspection, key minting, metered inference, and key revocation.

---

## 2. Repository Structure

```text
rhoai-maas-workshop/
├── .env.example                               # Template for environment variables and secrets
├── .gitignore                                 # Git ignore patterns
├── README.md                                  # Human-facing project overview and quickstart
├── pyproject.toml                             # Python package definition & dependencies
├── uv.lock                                    # Lockfile for reproducible Python dependencies
├── scripts/
│   └── render.sh                              # Manifest rendering utility (kustomize + envsubst)
├── maas-api/
│   └── openapi3.yaml                          # OpenAPI 3.0 specification for MaaS management & billing
├── models/
│   ├── gpt-oss-20b/                           # gpt-oss-20b model manifests
│   │   ├── kustomization.yaml                 # Umbrella kustomization
│   │   ├── namespace.yaml                     # Namespace manifest (llm)
│   │   ├── llm/                               # vLLM inference service deployment
│   │   └── maas/                              # MaaSModelRef, MaaSAuthPolicy, MaaSSubscriptions
│   └── qwen3-06b/                             # qwen3-06b model manifests (analogous structure)
├── external-models/
│   ├── inference-cluster-certs/               # CA secrets & DestinationRules for external clusters
│   └── openai/                                # ExternalModel, Secret, & provider kustomize manifests
├── external-metering/
│   ├── payload-processing-plugins.yaml        # ConfigMap with Envoy PayloadProcessorConfig & NetworkPolicy
│   ├── payload-processing-plugins-orig.yaml   # Reference backup of original plugin configuration
│   └── simulator/
│       ├── Dockerfile                         # Container build for the metering simulator
│       ├── simulator.py                       # Lightweight OpenMeter-compatible mock HTTP service
│       ├── simulator-openshift.yaml           # OpenShift deployment and service manifests
│       ├── test_simulator.py                  # Unit tests for the metering simulator
│       └── test_maas_notebook_utils.py        # Unit tests for notebook helper utilities
└── notebooks/
    ├── maas_notebook_utils.py                 # Notebook presentation, auth, and log parser utilities
    └── openshift-user-maas-interaction.ipynb  # Interactive step-by-step workshop notebook
```

---

## 3. Technology Stack & Prerequisites

- **Python**: `>=3.14` managed via [`uv`](https://docs.astral.sh/uv/) or standard virtual environments.
  - Core dependencies (in `pyproject.toml`): `requests >= 2.34.2`, `ipykernel >= 7.3.0`, `urllib3`.
- **OpenShift / Kubernetes**:
  - OpenShift CLI (`oc`) logged into a cluster running RHOAI / OpenDataHub.
  - Custom Resource Definitions:
    - `maas.opendatahub.io/v1alpha1` (`MaaSModelRef`, `MaaSAuthPolicy`, `MaaSSubscription`)
    - `inference.opendatahub.io/v1alpha1` (`ExternalModel`)
    - `llm-d.ai/v1alpha1` (`PayloadProcessorConfig`)
    - `networking.istio.io/v1` (`DestinationRule`)
- **Manifest Tooling**: `kustomize` (or `oc kustomize`), `envsubst` (gettext).

---

## 4. Key Architectural Patterns & Data Flows

### A. Authentication & Model Access Flow
1. **User Login**: User authenticates to OpenShift using `oc login`. The notebook extracts the session token via `oc whoami -t`.
2. **Access Evaluation**: The MaaS API validates the OpenShift user token via Kubernetes TokenReview against groups defined in `MaaSAuthPolicy` and `MaaSSubscription`.
3. **Key Minting**: User calls `POST /maas-api/v1/api-keys` with their OpenShift token, binding the generated API key (`sk-oai-...`) to an authorized subscription (e.g., `gpt-oss-20b-free` or `gpt-oss-20b-premium`).
4. **Inference Execution**: Requests sent to `POST /v1/chat/completions` pass the minted API key in the `Authorization: Bearer <key>` header.
5. **Gateway Enrichment**: The gateway injects `X-MaaS-Subscription` based on the key binding, routing to the appropriate model backend while enforcing rate limits.
6. **Key Revocation**: User calls `DELETE /maas-api/v1/api-keys/{id}` with their OpenShift token to revoke the API key.

### B. Metering & Entitlement Simulation
- **Entitlement Check (`GET /api/v1/customers/{customer}/entitlements/{feature}/value`)**:
  - Intercepted before inference by `external-metering` payload processor plugin.
  - Returns `{"hasAccess": true, "balance": <balance>, ...}`.
  - Initial simulated balance: `$1.00`. Cost per inference: `$0.50`.
  - When balance reaches `$0.00`, access is denied (HTTP 429). Simulator auto-resets the balance to `$1.00` upon denial.
- **Usage Reporting (`POST /api/v1/events`)**:
  - CloudEvent emitted upon successful inference completion (`type: inference.tokens`).
  - Simulator deducts the usage charge and updates customer balance.
- **Structured Log Parsing**:
  - `simulator.py` outputs JSON records delimited by `### <ISO-8601-TIMESTAMP> ###`.
  - `maas_notebook_utils.py` filters and correlates these log records to provide real-time balance tracking in the notebook.

### C. Scoped Manifest Templating (`scripts/render.sh`)
- Uses `envsubst` strictly with explicit target variables (`MAAS_RHDM_API_KEY`, `MAAS_INFERENCE_CLUSTER_A_TOKEN`).
- Prevents accidental overwriting of Kubernetes environment variables (e.g., `$(POD_NAME)`) or unrelated placeholders.

---

## 5. Development Workflows & Agent Instructions

### Test Execution
Always execute the test suite after modifying Python code in `notebooks/` or `external-metering/simulator/`:
```bash
# Run all unit tests
PYTHONPATH=notebooks:external-metering/simulator uv run python -m unittest \
    external-metering/simulator/test_simulator.py \
    external-metering/simulator/test_maas_notebook_utils.py

# Run a specific test class
PYTHONPATH=notebooks:external-metering/simulator uv run python -m unittest \
    external-metering.simulator.test_simulator.UsageBalancesTests
```

### Manifest Rendering & Validation
When editing Kustomize manifests under `models/` or `external-models/`:
```bash
# Validate kustomize build for models
oc kustomize models/gpt-oss-20b/maas
oc kustomize models/qwen3-06b/maas

# Render external models with scoped environment variables
./scripts/render.sh external-models/openai

# Dry-run applying to cluster (if connected)
./scripts/render.sh external-models/openai --apply
```

### Running the Simulator Locally
```bash
# Start standalone simulator on port 8080 (or PORT env var)
PORT=8080 uv run python external-metering/simulator/simulator.py
```

### OpenAPI Contract & Immutability Rule
- **`maas-api/openapi3.yaml` is strictly read-only**:
  - **NEVER edit, modify, or format `maas-api/openapi3.yaml`**.
  - Agents and developers may read this file to understand API context, endpoints, schemas, parameters, and auth contracts, but must never modify it.
  - All external consumers, notebooks, manifests, scripts, and documentation must adapt to conform to `maas-api/openapi3.yaml`, never vice versa.

### Environment Configuration
- The codebase relies on `.env` (derived from `.env.example`).
- **Never commit `.env` or hardcode credentials, tokens, or cluster-specific endpoints.**
- Supported environment variables:
  - `KUBECONFIG`: Path to active kubeconfig (defaults to `~/.kube/config`).
  - `MAAS_GATEWAY_URL`: Ingress URL for the OpenShift cluster gateway.
  - `MAAS_API`: Optional override for MaaS API endpoint.
  - `MAAS_SUBSCRIPTION`: Target subscription name (e.g. `gpt-oss-20b-free`).
  - `MAAS_MODEL`: Target model identifier (e.g. `gpt-oss-20b`).
  - `MAAS_INFERENCE_CLUSTER_A_HOST`: Route domain of remote inference cluster A.
  - `MAAS_INFERENCE_CLUSTER_A_TOKEN`: Bearer token for remote inference cluster A.
  - `MAAS_RHDP_HOST`: Endpoint domain for external RHDP provider.
  - `MAAS_RHDM_API_KEY`: API key for external OpenAI-compatible provider (RHDP).

---

## 6. Coding Conventions & Best Practices

1. **Python Code**:
   - Enable `from __future__ import annotations`.
   - Use Python standard library modules where feasible (`http.server`, `threading`, `unittest`, `dataclasses`, `pathlib`).
   - Keep threading and concurrency safe (e.g., `UsageBalances` and `LOG_LOCK` in `simulator.py`).
   - Match existing log format conventions: `### <timestamp> ###\n<json-payload>\n`.
2. **Kubernetes Manifests**:
   - Ensure manifests remain compatible with standard `kustomize` and `oc kustomize`.
   - Adhere strictly to the API versions (`maas.opendatahub.io/v1alpha1`, `inference.opendatahub.io/v1alpha1`, `llm-d.ai/v1alpha1`).
   - Maintain namespace scoping: `llm` for inference services, `models-as-a-service` for subscriptions and policies, `external-models` for proxy providers, `external-metering` for simulator.
3. **OpenAPI Specification & Immutability**:
   - `maas-api/openapi3.yaml` is strictly read-only and must NEVER be modified. AI coding agents and developers may read it to understand API context, endpoints, and schemas, but must never write to it.
   - The specification serves as the authoritative contract adhering to OpenAPI 3.0.3 standards.
   - Maintain consistency between response schemas (`ErrorResponse`, `HealthResponse`, `SubscriptionListResponse`, `ApiKeyResponse`) across notebook and client interactions without altering the specification file.
4. **Jupyter Notebooks**:
   - Keep notebooks clean of hardcoded secrets or temporary output.
   - Separate reusable presentation logic into `notebooks/maas_notebook_utils.py` and cover it with unit tests in `external-metering/simulator/test_maas_notebook_utils.py`.
