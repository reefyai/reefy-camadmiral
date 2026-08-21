# CamAdmiral

CamAdmiral discovers ONVIF and RTSP cameras, validates their streams, and exposes stable
downstream streams for consumers such as Frigate.

![CamAdmiral solution architecture](docs/images/camadmiral-solution.png)

## Why CamAdmiral?

- **Stable camera URLs.** Cameras on a local network can receive new IP addresses.
  CamAdmiral tracks adopted cameras by MAC and ONVIF identity and keeps their downstream
  URLs stable, so consumers do not need reconfiguration after an IP change.
- **Shared camera connections.** CamAdmiral shares one upstream connection per camera
  stream across all consumers, reducing load on slow links such as Wi-Fi and on cameras
  with limited client capacity.
- **Health monitoring and alerts.** CamAdmiral monitors video availability, retains an
  availability history, and sends Telegram notifications when a camera goes offline or
  recovers.

![CamAdmiral camera dashboard](docs/images/camadmiral-dashboard.png)

## Discover and adopt cameras

CamAdmiral scans the local network for ONVIF and RTSP cameras. It includes an embedded,
independently maintained compatibility database of RTSP URL paths sourced from public
first-party vendor documentation. This lets CamAdmiral automatically try a bounded set of
likely stream paths after the operator supplies the camera username and password.

![CamAdmiral ONVIF and RTSP network scan details](docs/images/camadmiral-network-scan.png)

Adoption validates the credentials and discovered streams before storing anything. ONVIF
cameras expose their media profiles directly; RTSP-only cameras use the compatibility
database to find working paths without asking the operator to construct URLs manually.

![CamAdmiral camera adoption dialog](docs/images/camadmiral-adopt-camera.png)

## Live view

Open a camera's live view directly from the dashboard to confirm its framing without leaving
CamAdmiral. The browser consumes CamAdmiral's managed downstream instead of opening another
connection to the camera.

![CamAdmiral live camera view](docs/images/camadmiral-live-view.png)

## Stable downstream streams

CamAdmiral validates each source, reports its codec, resolution, frame rate, and health, then
exposes stable RTSP URLs for record and detection consumers. Camera credentials and source
paths remain hidden unless an operator explicitly reveals them. Downstream credentials in
the screenshot are intentionally masked.

![CamAdmiral validated downstream streams](docs/images/camadmiral-downstream-streams.png)

## Availability timeline

Each camera includes a seven-day availability timeline. Red blocks show periods when the
camera was offline.

![CamAdmiral camera availability timeline showing offline periods](docs/images/camadmiral-availability-timeline.png)

## Run with Docker

Build the image and create one named volume for all persistent CamAdmiral state:

```console
docker build -t camadmiral:latest .
docker volume create camadmiral-data
mkdir -p secrets
openssl rand -hex 32 > secrets/api-token
openssl rand -base64 24 > secrets/admin-password
chmod 600 secrets/api-token secrets/admin-password
docker run -d \
  --name camadmiral \
  --network host \
  --restart unless-stopped \
  --volume camadmiral-data:/var/lib/camadmiral \
  --mount type=bind,source="$(pwd)/secrets/api-token",target=/run/secrets/camadmiral_api_token,readonly \
  --mount type=bind,source="$(pwd)/secrets/admin-password",target=/run/secrets/camadmiral_admin_password,readonly \
  camadmiral:latest
```

Open `http://<device-address>:18080`. No configuration file is required. On first boot,
CamAdmiral generates its master key inside the data volume. Recreating the container with
the same named volume preserves adopted cameras and credentials. Sign in as `admin` with
the password in `secrets/admin-password`.

For Docker Compose, create the API token once and start the checked-in hardened
configuration:

```console
mkdir -p secrets
openssl rand -hex 32 > secrets/api-token
openssl rand -base64 24 > secrets/admin-password
chmod 600 secrets/api-token
chmod 600 secrets/admin-password
docker compose up -d
```

The browser prompts for HTTP Basic credentials. Use username `admin` and the
value stored in `secrets/admin-password`. Put CamAdmiral behind an HTTPS reverse
proxy before accessing it across an untrusted network because HTTP Basic
credentials are not encrypted by the application protocol.

The complete persistent state boundary is `/var/lib/camadmiral`. Back up and restore that
volume as a unit. Stop CamAdmiral before making a raw volume copy so the SQLite database and
its generated key are captured consistently.

CamAdmiral checks streams already in use from go2rtc's runtime counters and periodically asks
go2rtc for one small JPEG frame per camera. For an idle camera, that bounded request briefly
opens the source, validates decodable video, refreshes the in-memory table thumbnail, and
disconnects. An active camera reuses its existing upstream connection. Loading the web UI
reads only this cache and never opens a camera stream.

## Telegram notifications

Open **Notifications** in the web UI to connect a dedicated Telegram bot. Create the bot
with `@BotFather`, paste its token, then use the generated **Open Telegram** link and press
**Start**. CamAdmiral discovers the destination chat from that one-time pairing message, so
you do not need to find or enter a numeric chat ID. Alerts are enabled automatically when
the bot is configured and paired.

<p align="center">
  <img src="docs/images/camadmiral-telegram-alerts.png" alt="CamAdmiral Telegram offline and recovery alerts" width="420">
</p>

Use a dedicated bot without an existing webhook. CamAdmiral rejects bots already connected
to another application and never changes their webhook configuration. The bot token and
temporary pairing secret are encrypted with CamAdmiral's master key and are never returned
by the settings API. Alert messages contain only the camera name, incident state, and
observation time. They do not contain camera credentials, media URLs, IP addresses, or MAC
addresses.

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
