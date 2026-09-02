FROM alpine:3.22 AS go2rtc

ARG TARGETARCH
ARG GO2RTC_VERSION=1.9.14
ARG GO2RTC_AMD64_SHA256=32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6
ARG GO2RTC_ARM64_SHA256=359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50

RUN apk add --no-cache ca-certificates curl \
    && case "${TARGETARCH}" in \
         amd64) asset=go2rtc_linux_amd64; expected="${GO2RTC_AMD64_SHA256}" ;; \
         arm64) asset=go2rtc_linux_arm64; expected="${GO2RTC_ARM64_SHA256}" ;; \
         *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL \
         "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/${asset}" \
         -o /go2rtc \
    && echo "${expected}  /go2rtc" | sha256sum -c - \
    && chmod 0755 /go2rtc

FROM python:3.13-alpine3.22

ARG CHECKPOINT_REVISION=local
ARG GO2RTC_VERSION=1.9.14

RUN apk add --no-cache ca-certificates ffmpeg \
    && addgroup -g 10001 camadmiral \
    && adduser -D -H -u 10001 -G camadmiral camadmiral \
    && mkdir -p /run/secrets /run/camadmiral /var/lib/camadmiral \
    && chown 10001:10001 /run/camadmiral /var/lib/camadmiral

WORKDIR /opt/camadmiral

COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check \
    --root-user-action=ignore -r requirements.txt

COPY --from=go2rtc /go2rtc /usr/local/bin/go2rtc
COPY third_party/go2rtc/LICENSE /usr/share/licenses/go2rtc/LICENSE
COPY go2rtc.yaml /etc/camadmiral/go2rtc.yaml
COPY VERSION ./VERSION
COPY camadmiral ./camadmiral
COPY reefy/icon.png ./reefy/icon.png

ENV CAMADMIRAL_CHECKPOINT=downstream-preview \
    CAMADMIRAL_REVISION=${CHECKPOINT_REVISION} \
    CAMADMIRAL_GO2RTC_URL=http://127.0.0.1:1984 \
    CAMADMIRAL_GO2RTC_RTSP_URL=rtsp://127.0.0.1:18554 \
    CAMADMIRAL_GO2RTC_VERSION=${GO2RTC_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME ["/var/lib/camadmiral"]

USER 10001:10001

EXPOSE 18080 18554

HEALTHCHECK --interval=10s --timeout=2s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18080/healthz', timeout=1)"]

ENTRYPOINT ["python", "-m", "camadmiral.supervisor"]
