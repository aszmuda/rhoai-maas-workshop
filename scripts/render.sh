#!/usr/bin/env bash
set -euo pipefail

# Resolve repository root
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

# 1. Parse arguments
APPLY=false
TARGET_DIR=""

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            echo "Usage: $(basename "$0") [TARGET_DIR] [--apply]"
            echo ""
            echo "Render kustomize manifests with scoped envsubst."
            echo ""
            echo "Arguments:"
            echo "  TARGET_DIR   Path to kustomize directory (default: external-models/openai)"
            echo "  --apply      Directly pipe rendered manifests to 'oc apply -f -'"
            echo "  -h, --help   Show this help message and exit"
            exit 0
            ;;
        --apply)
            APPLY=true
            ;;
        *)
            if [ -z "$TARGET_DIR" ]; then
                TARGET_DIR="$arg"
            fi
            ;;
    esac
done

TARGET_DIR="${TARGET_DIR:-$ROOT_DIR/external-models/openai}"
if [ ! -d "$TARGET_DIR" ] && [ -d "$ROOT_DIR/$TARGET_DIR" ]; then
    TARGET_DIR="$ROOT_DIR/$TARGET_DIR"
fi

if [ ! -d "$TARGET_DIR" ]; then
    echo "Error: Target directory '$TARGET_DIR' not found." >&2
    exit 1
fi

# 2. Load .env strictly inside the script's subshell
if [ -r "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    echo "Warning: '$ENV_FILE' not found. Relying on current environment variables." >&2
fi

# 3. Fail-fast validation of required secret variables
REQUIRED_VARS=(
    "MAAS_RHDM_API_KEY"
    "MAAS_INFERENCE_CLUSTER_A_TOKEN"
)

MISSING_VARS=()
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var:-}" ]; then
        MISSING_VARS+=("$var")
    fi
done

if [ ${#MISSING_VARS[@]} -gt 0 ]; then
    echo "Error: Missing required secret variable(s) for rendering:" >&2
    for var in "${MISSING_VARS[@]}"; do
        echo "  - $var" >&2
    done
    echo "Please set them in '$ENV_FILE' (see .env.example) or export them." >&2
    exit 1
fi

# 4. Determine kustomize command (oc kustomize preferred)
if command -v oc &>/dev/null; then
    KUSTOMIZE_CMD="oc kustomize"
elif command -v kustomize &>/dev/null; then
    KUSTOMIZE_CMD="kustomize build"
else
    echo "Error: Neither 'oc' nor 'kustomize' command found in PATH." >&2
    exit 1
fi

if ! command -v envsubst &>/dev/null; then
    echo "Error: 'envsubst' command not found in PATH." >&2
    exit 1
fi

# 5. Format scoped substitution list
VARS_ARGS=$(printf '${%s} ' "${REQUIRED_VARS[@]}")

# 6. Render and optionally apply
if [ "$APPLY" = true ]; then
    if ! command -v oc &>/dev/null; then
        echo "Error: 'oc' command not found in PATH, required for --apply." >&2
        exit 1
    fi
    $KUSTOMIZE_CMD "$TARGET_DIR" | envsubst "$VARS_ARGS" | oc apply -f -
else
    $KUSTOMIZE_CMD "$TARGET_DIR" | envsubst "$VARS_ARGS"
fi
