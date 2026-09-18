# RHOAI Models-as-a-Service (MaaS) Workshop

This repository contains workshop materials, manifests, and interactive guides demonstrating **Models-as-a-Service (MaaS)** capabilities on **Red Hat OpenShift AI (RHOAI)**.

It illustrates how platform operators and consumers manage the full lifecycle of AI models: deploying large language models (LLMs), defining access control policies, managing tiered subscriptions with token rate limits, connecting external model providers, and integrating usage metering.

---

## Key Components

- **Model Deployments & Subscriptions (`models/`)**  
  Kustomize manifests for deploying LLMs (such as `gpt-oss-20b` and `qwen3-06b`) with associated OpenShift AI MaaS custom resources:
  - `MaaSModelRef`: Model registration and reference to inference services.
  - `MaaSAuthPolicy`: Group- and user-based access control.
  - `MaaSSubscription`: Tiered subscription definitions (e.g., Free vs. Premium tiers) configuring token rate limits and quotas.

- **External Model Providers (`external-models/`)**  
  Configurations to proxy and route inference traffic to external providers (such as OpenAI or remote inference clusters) using `ExternalModel` resources, credential secrets, and TLS destination rules.

- **MaaS API Specification (`maas-api/`)**  
  OpenAPI 3.0 specification (`openapi3.yaml`) for the MaaS management and billing API, detailing endpoints for health checks, model discovery, subscription inspection, and API key management.

- **External Metering Simulator (`external-metering/`)**  
  A lightweight, OpenMeter-compatible mock HTTP service and payload processing plugin manifests to simulate customer entitlements, quota enforcement, and per-inference balance deduction.

- **Interactive Notebooks (`notebooks/`)**  
  A step-by-step Jupyter notebook (`openshift-user-maas-interaction.ipynb`) and presentation utilities (`maas_notebook_utils.py`) walking through:
  1. Authenticating via OpenShift CLI (`oc`) tokens.
  2. Listing accessible subscriptions and models via the MaaS API.
  3. Generating temporary MaaS API keys.
  4. Running model inference while observing real-time metering and entitlement checks.
  5. Revoking API keys.

- **Helper Scripts (`scripts/`)**  
  `render.sh`: Utility script to render Kustomize manifests with scoped variable substitution via `envsubst` and optionally apply them via `oc apply`.

---

## Repository Structure

```text
├── external-metering/       # Metering plugins and OpenMeter simulator service
├── external-models/         # Manifests for integrating external model providers
├── maas-api/                # OpenAPI 3.0 specification for the MaaS API
├── models/                  # LLM deployment manifests and tiered subscription CRs
├── notebooks/               # Hands-on Jupyter notebook and helper utilities
├── pyproject.toml           # Python project definition and dependencies
├── scripts/                 # Utility scripts (manifest rendering, deployment)
└── uv.lock                  # Lockfile for reproducible Python dependencies
```

---

## Getting Started

### Prerequisites

- Python 3.14+ (managed via [`uv`](https://docs.astral.sh/uv/) or `python3 -m venv`)
- OpenShift CLI (`oc`) logged in to a target cluster
- Optional CLI tools for manifest rendering: `kustomize`, `envsubst`

### Setup

1. **Install dependencies:**
   ```bash
   uv sync
   # or with pip:
   pip install -e .
   ```

2. **Configure environment variables:**
   Copy the example environment configuration and adjust values for your cluster:
   ```bash
   cp .env.example .env
   ```
   Key variables include:
   - `KUBECONFIG`: Path to your OpenShift kubeconfig (default: `~/.kube/config`).
   - `MAAS_GATEWAY_URL`: Base gateway ingress URL for the OpenShift cluster.
   - `MAAS_SUBSCRIPTION`: Target subscription tier (e.g., `free-tier`).
   - `MAAS_MODEL`: Model name to query (e.g., `gpt-oss-20b`).
   - `MAAS_RHDM_API_KEY` / `MAAS_INFERENCE_CLUSTER_A_TOKEN`: Credentials for external providers if deploying external models.

3. **Launch the workshop notebook:**
   Start Jupyter Lab or Notebook:
   ```bash
   uv run jupyter lab notebooks/openshift-user-maas-interaction.ipynb
   ```

---

## Running Tests

Unit tests for the metering simulator and notebook utilities can be executed with Python's test runner:

```bash
PYTHONPATH=notebooks:external-metering/simulator uv run python -m unittest external-metering/simulator/test_simulator.py external-metering/simulator/test_maas_notebook_utils.py
```
