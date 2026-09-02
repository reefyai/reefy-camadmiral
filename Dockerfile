FROM --platform=$BUILDPLATFORM golang:1.25-bookworm AS go2rtc

ARG TARGETOS
ARG TARGETARCH
ARG GO2RTC_REVISION=b5948cfb25404cc5cb37b166ecaa2dca20b11d4b
ARG GO2RTC_SOURCE_SHA256=78aa79bcedec8f155e4060a379613979b0b3ee48ff62ee5164bafc0ac6532386

WORKDIR /src

RUN apt-get update \
    && apt-get install --yes --no-install-recommends patch \
    && rm -rf /var/lib/apt/lists/* \
    && curl --fail --show-error --silent --location \
        "https://github.com/AlexxIT/go2rtc/archive/${GO2RTC_REVISION}.tar.gz" \
        --output /tmp/go2rtc.tar.gz \
    && echo "${GO2RTC_SOURCE_SHA256}  /tmp/go2rtc.tar.gz" | sha256sum --check - \
    && tar --extract --gzip --file /tmp/go2rtc.tar.gz --strip-components=1 \
    && rm /tmp/go2rtc.tar.gz

COPY third_party/go2rtc/patches/0001-live-source-handover.patch /tmp/go2rtc.patch

RUN patch --batch --strip=1 --input=/tmp/go2rtc.patch \
    && gofmt -w \
         internal/streams/add_consumer.go \
         internal/streams/play.go \
         internal/streams/producer.go \
         internal/streams/producer_replace_test.go \
         internal/streams/stream.go \
         internal/streams/stream_test.go \
         pkg/core/node.go \
         pkg/core/track.go \
         pkg/core/track_replace_test.go \
         pkg/rtsp/consumer.go \
         pkg/rtsp/consumer_continuity_test.go \
    && go test -race ./internal/streams ./pkg/core \
    && go test -race ./pkg/rtsp -run '^Test(RTPContinuity|PacketWriter)' \
    && CGO_ENABLED=0 GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" \
         go build -buildvcs=false -trimpath -ldflags="-s -w" -o /go2rtc .

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
COPY --from=go2rtc /src/LICENSE /usr/share/licenses/go2rtc/LICENSE
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
