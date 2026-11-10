FROM mirror.gcr.io/library/ubuntu@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852

ARG TOOLS_REVISION
ARG PYTHON_VERSION=3.13.18
COPY scripts/ci_bootstrap.sh /opt/camadmiral-ci/scripts/ci_bootstrap.sh
COPY scripts/ci_packages.py /opt/camadmiral-ci/scripts/ci_packages.py
RUN --mount=type=bind,source=apt-cache,target=/tmp/cached-archives \
    cp /tmp/cached-archives/*.deb /var/cache/apt/archives/ \
    && mv /etc/apt/apt.conf.d/docker-clean /opt/camadmiral-ci/docker-clean \
    && bash /opt/camadmiral-ci/scripts/ci_bootstrap.sh \
    && printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod 0755 /usr/sbin/policy-rc.d \
    && python3 -c "from pathlib import Path; p=Path('/etc/apt/sources.list.d/ubuntu.sources'); p.write_text(p.read_text().replace('http://archive.ubuntu.com/', 'https://archive.ubuntu.com/').replace('http://security.ubuntu.com/', 'https://security.ubuntu.com/'))" \
    && python3 /opt/camadmiral-ci/scripts/ci_packages.py update \
    && CAMADMIRAL_CI_PACKAGE_COMMAND_SECONDS=300 CAMADMIRAL_CI_PACKAGE_BUDGET_SECONDS=2100 \
       python3 /opt/camadmiral-ci/scripts/ci_packages.py install -y --no-install-recommends docker.io docker-compose-v2 docker-buildx \
    && mv /opt/camadmiral-ci/docker-clean /etc/apt/apt.conf.d/docker-clean \
    && rm -f /var/cache/apt/archives/*.deb

COPY python-cache /opt/camadmiral-toolcache/Python
COPY requirements.txt /opt/camadmiral-ci/requirements.txt
COPY e2e/requirements.txt /opt/camadmiral-ci/e2e/requirements.txt
COPY scripts/ci_tools.py /opt/camadmiral-ci/scripts/ci_tools.py
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/camadmiral-browsers
RUN mkdir -p /opt/camadmiral-ci/bin \
    && printf '#!/bin/sh\nexec /usr/bin/python3 /opt/camadmiral-ci/scripts/ci_packages.py "$@"\n' > /opt/camadmiral-ci/bin/apt-get \
    && chmod 0755 /opt/camadmiral-ci/bin/apt-get \
    && LD_LIBRARY_PATH=/opt/camadmiral-toolcache/Python/${PYTHON_VERSION}/x64/lib \
       /opt/camadmiral-toolcache/Python/${PYTHON_VERSION}/x64/bin/python -m pip install --disable-pip-version-check -r /opt/camadmiral-ci/requirements.txt -r /opt/camadmiral-ci/e2e/requirements.txt \
    && PATH=/opt/camadmiral-ci/bin:$PATH LD_LIBRARY_PATH=/opt/camadmiral-toolcache/Python/${PYTHON_VERSION}/x64/lib \
       /opt/camadmiral-toolcache/Python/${PYTHON_VERSION}/x64/bin/python -m playwright install --with-deps webkit \
    && CAMADMIRAL_CI_TOOLS_REVISION=${TOOLS_REVISION} LD_LIBRARY_PATH=/opt/camadmiral-toolcache/Python/${PYTHON_VERSION}/x64/lib \
       /opt/camadmiral-toolcache/Python/${PYTHON_VERSION}/x64/bin/python /opt/camadmiral-ci/scripts/ci_tools.py write \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip /opt/camadmiral-ci/bin
LABEL org.opencontainers.image.source="https://github.com/reefyai/reefy-camadmiral" \
      org.opencontainers.image.revision="${TOOLS_REVISION}" \
      org.opencontainers.image.version="ci-${TOOLS_REVISION}"
