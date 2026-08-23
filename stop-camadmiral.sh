#!/bin/sh

set -eu

CONTAINER="camadmiral"

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is required." >&2
    exit 1
fi

if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "CamAdmiral is not installed. Nothing to stop."
    exit 0
fi

running=$(docker container inspect --format '{{.State.Running}}' "$CONTAINER")
if [ "$running" = "true" ]; then
    docker stop "$CONTAINER" >/dev/null
    echo "CamAdmiral is stopped."
else
    echo "CamAdmiral is already stopped."
fi

echo "The container, credentials, configuration, and camadmiral-data volume are preserved."
echo "Run ./start-camadmiral.sh to start CamAdmiral again."
