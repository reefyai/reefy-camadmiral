#!/bin/sh
set -eu

if [ ! -f /var/lib/camadmiral/inventory.json ]; then
    cp /e2e/fixtures/inventory-initial.json /var/lib/camadmiral/inventory.json
fi

exec python -m camadmiral.supervisor
