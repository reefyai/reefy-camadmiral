#!/bin/sh

set -eu

DEFAULT_IMAGE="ghcr.io/reefyai/reefy-camadmiral:latest"
IMAGE=${CAMADMIRAL_IMAGE:-$DEFAULT_IMAGE}
CONTAINER="camadmiral"
VOLUME="camadmiral-data"
ADMIN_PASSWORD_FILE="/var/lib/camadmiral/standalone-secrets/admin-password"
API_TOKEN_FILE="/var/lib/camadmiral/standalone-secrets/api-token"
CONFIG_FILE="/var/lib/camadmiral/standalone.yaml"
UPDATE=false

usage() {
    echo "Usage: ./start-camadmiral.sh [--update]" >&2
}

case $# in
    0) ;;
    1)
        if [ "$1" = "--update" ]; then
            UPDATE=true
        else
            usage
            exit 2
        fi
        ;;
    *)
        usage
        exit 2
        ;;
esac

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is required." >&2
    exit 1
fi

show_access() {
    password=$(docker exec "$CONTAINER" cat "$ADMIN_PASSWORD_FILE")
    api_token=$(docker exec "$CONTAINER" cat "$API_TOKEN_FILE")
    echo "CamAdmiral is running at http://127.0.0.1:18080"
    echo "Use the device address instead of 127.0.0.1 from another computer."
    echo "Username: admin"
    echo "Password: $password"
    echo "Consumer API token: $api_token"
}

CONTAINER_EXISTS=false
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    CONTAINER_EXISTS=true
fi

if [ "$CONTAINER_EXISTS" = true ] && [ "$UPDATE" = false ]; then
    docker start "$CONTAINER" >/dev/null
    show_access
    exit 0
fi

if [ -z "${CAMADMIRAL_IMAGE:-}" ]; then
    docker pull "$IMAGE"
elif ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "CamAdmiral image not found: $IMAGE" >&2
    exit 1
fi

if [ "$CONTAINER_EXISTS" = true ]; then
    running=$(docker container inspect --format '{{.State.Running}}' "$CONTAINER")
    if [ "$running" = "true" ]; then
        docker stop "$CONTAINER" >/dev/null
    fi
    docker rm "$CONTAINER" >/dev/null
fi

docker volume create "$VOLUME" >/dev/null
docker run --rm \
    --volume "$VOLUME:/var/lib/camadmiral" \
    --entrypoint python \
    "$IMAGE" \
    -c 'from pathlib import Path; import secrets
root = Path("/var/lib/camadmiral/standalone-secrets")
root.mkdir(mode=0o700, exist_ok=True)
root.chmod(0o700)
values = {
    root / "api-token": secrets.token_hex(32),
    root / "admin-password": secrets.token_urlsafe(24),
}
for path, value in values.items():
    if not path.exists():
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)
    elif not path.read_text(encoding="utf-8").strip():
        raise SystemExit(f"existing secret is empty: {path.name}")
config = Path("/var/lib/camadmiral/standalone.yaml")
if not config.exists():
    config.write_text("""version: 1
secrets:
  api_token_file: /var/lib/camadmiral/standalone-secrets/api-token
  admin_password_file: /var/lib/camadmiral/standalone-secrets/admin-password
""", encoding="utf-8")
    config.chmod(0o600)'

docker run -d \
    --name "$CONTAINER" \
    --network host \
    --restart unless-stopped \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --memory 256m \
    --memory-swap 512m \
    --pids-limit 192 \
    --env "CAMADMIRAL_CONFIG_FILE=$CONFIG_FILE" \
    --volume "$VOLUME:/var/lib/camadmiral" \
    --tmpfs /run/camadmiral:rw,noexec,nosuid,size=4m,uid=10001,gid=10001 \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m,uid=10001,gid=10001 \
    "$IMAGE" >/dev/null

if [ "$UPDATE" = true ] && [ "$CONTAINER_EXISTS" = true ]; then
    echo "CamAdmiral was updated. Persistent data and credentials were preserved."
fi
show_access
