FROM --platform=$BUILDPLATFORM golang:1.25-alpine@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59 AS go2rtc

ARG TARGETOS=linux
ARG TARGETARCH
ARG GO2RTC_VERSION=1.9.14
ARG GO2RTC_SOURCE=b5948cfb25404cc5cb37b166ecaa2dca20b11d4b
ARG GO2RTC_SOURCE_SHA256=78aa79bcedec8f155e4060a379613979b0b3ee48ff62ee5164bafc0ac6532386

RUN apk add --no-cache ca-certificates curl patch gcc musl-dev
WORKDIR /src
RUN curl --retry 5 --retry-all-errors -fsSL \
      "https://codeload.github.com/AlexxIT/go2rtc/tar.gz/${GO2RTC_SOURCE}" -o /source.tar.gz \
    && echo "${GO2RTC_SOURCE_SHA256}  /source.tar.gz" | sha256sum -c - \
    && tar -xzf /source.tar.gz --strip-components=1 -C /src \
    && rm /source.tar.gz
COPY third_party/go2rtc/bounded-setup.patch /tmp/bounded-setup.patch
COPY third_party/go2rtc/retention_test.go internal/streams/retention_test.go
RUN patch --batch --fuzz=0 -p1 < /tmp/bounded-setup.patch \
    && gofmt -w internal/streams/producer.go internal/streams/stream.go \
      internal/streams/add_consumer.go internal/streams/retention_test.go
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go test -race ./internal/streams -run 'TestRetainedSetup|TestFailedDial'
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
      go build -trimpath -ldflags "-s -w" -o /go2rtc .

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
