# CamAdmiral

CamAdmiral discovers ONVIF and RTSP cameras, validates their streams, and exposes stable
downstream streams for consumers such as Frigate.

## Run with Docker

Build the image and create one named volume for all persistent CamAdmiral state:

```console
docker build -t camadmiral:latest .
docker volume create camadmiral-data
docker run -d \
  --name camadmiral \
  --network host \
  --restart unless-stopped \
  --volume camadmiral-data:/var/lib/camadmiral \
  camadmiral:latest
```

Open `http://<device-address>:18080`. No configuration file is required. On first boot,
CamAdmiral generates its master key inside the data volume. Recreating the container with
the same named volume preserves adopted cameras and credentials.

For Docker Compose, create the API token once and start the checked-in hardened
configuration:

```console
mkdir -p secrets
openssl rand -hex 32 > secrets/api-token
chmod 600 secrets/api-token
docker compose up -d
```

The complete persistent state boundary is `/var/lib/camadmiral`. Back up and restore that
volume as a unit. Stop CamAdmiral before making a raw volume copy so the SQLite database and
its generated key are captured consistently.

## Tests

Run the fast unit and component suite in an environment with the dependencies from
`requirements.txt`:

```console
python -m unittest discover -s tests -v
```

Real synthetic media workflows use the isolated Docker lab under `e2e/`:

```console
python3 e2e/run.py
```

The E2E runner builds the current container and verifies it only through HTTP, RTSP,
process, and persistent-volume boundaries. See [e2e/README.md](e2e/README.md) for its
scenario list and isolation guarantees.

The `CamAdmiral release gate` workflow runs the fast suite and the complete E2E lab for
pull requests, `main`, release tags, manual runs, and as a reusable `workflow_call`.
Release publishing must call this workflow and depend on its successful result. A failed
or cancelled E2E run is therefore not release-eligible.

CamAdmiral versions follow Reefy's `vYYYY.MM.DD-NN` convention. `VERSION` is the
standalone source version. On Reefy, the platform-injected `REEFY_APP_VERSION` is
authoritative and is displayed in the app footer.

## Optional configuration and external secrets

Mount [camadmiral.example.yaml](camadmiral.example.yaml) at
`/etc/camadmiral/config.yaml` to change the server, storage, secret, or Frigate integration
settings. An explicitly configured `secrets.master_key_file` is authoritative and must
exist. This supports Docker Compose secrets and Reefy-managed read-only secret mounts. An
external master key is outside the data-volume backup boundary and must be backed up and
restored separately. The default generated key stays inside the data volume.
