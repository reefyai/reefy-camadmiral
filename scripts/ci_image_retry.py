"""Retry dependency acquisition inside an already isolated CI Docker daemon."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

# Preserve six transport attempts and the existing worst-case total budget.
# A cold multi-GB image may need more than 90 seconds to write/extract layers;
# canceling that work repeatedly increases disk contention in parallel labs.
ATTEMPTS = 6
TIMEOUT = 300
TOTAL_TIMEOUT = 6 * 90 + sum(5 * attempt for attempt in range(1, 6))


def transient_download_failure(detail):
    detail = detail.lower()
    permanent = ('unauthorized', 'forbidden', '401 ', '403 ', '404 ', 'manifest unknown',
                 'no space left', 'invalid signature', 'certificate verify failed',
                 'certificate_verify_failed', 'certificate verification failed',
                 'unknown authority', 'not found', 'denied:')
    transient = ('bad record mac', 'decryption_failed_or_bad_record_mac', 'decryption failed',
                 'unexpected eof', 'connection reset', 'connection timed out', 'i/o timeout',
                 'read timed out', 'tls handshake timeout', 'tls connection',
                 'temporary failure resolving', 'temporary failure in name resolution',
                 'remote end closed connection', 'unexpected media type text/html',
                 'connection closed by remote host')
    return not any(value in detail for value in permanent) and any(value in detail for value in transient)


def pull_image(docker, image):
    command = [*docker, 'pull', image]
    deadline = time.monotonic() + TOTAL_TIMEOUT
    failure = None
    for attempt in range(1, ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise failure or subprocess.TimeoutExpired(command, TOTAL_TIMEOUT)
        started = time.monotonic()
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=min(TIMEOUT, remaining))
        except subprocess.TimeoutExpired as exc:
            status, failure = 'timeout', exc
        else:
            print(result.stdout, end='', flush=True)
            print(result.stderr, end='', file=sys.stderr, flush=True)
            if result.returncode == 0:
                return
            status = 'failed'
            failure = subprocess.CalledProcessError(result.returncode, result.args)
            if not transient_download_failure(result.stdout + '\n' + result.stderr):
                raise failure
        backoff = 5 * attempt
        if attempt == ATTEMPTS or deadline - time.monotonic() <= backoff:
            raise failure
        print('CI image transfer retry: ' + json.dumps({'image': image, 'attempt': attempt,
              'status': status, 'elapsed_seconds': round(time.monotonic() - started, 1),
              'next_attempt': attempt + 1, 'backoff_seconds': backoff}), flush=True)
        time.sleep(backoff)


def literal_build_images(build):
    """Prefetch literal FROM sources; BuildKit retains authority over the build."""
    if not build:
        return set()
    build = {'context': build} if isinstance(build, str) else build
    text = build.get('dockerfile_inline')
    if text is None:
        context = Path(build.get('context') or '.')
        path = context / (build.get('dockerfile') or 'Dockerfile')
        if not path.is_file():
            return set()
        text = path.read_text()
    stages, images = set(), set()
    for line in text.replace('\\\n', '').splitlines():
        if not line.lstrip().upper().startswith('FROM '):
            continue
        words = shlex.split(line, comments=True)
        index = 2 if len(words) > 1 and words[1].startswith('--platform=') else 1
        if len(words) <= index:
            continue
        image = words[index]
        if image.lower() != 'scratch' and image.lower() not in stages and '$' not in image:
            images.add(image)
        if len(words) > index + 2 and words[index + 1].upper() == 'AS':
            stages.add(words[index + 2].lower())
    return images


def prefetch_compose_images(command):
    if 'compose' not in command:
        return
    compose = command.index('compose')
    up = compose + 1
    value_options = {'-p', '--project-name', '-f', '--file', '--profile',
                     '--env-file', '--project-directory', '--ansi', '--parallel', '--progress'}
    while up < len(command) and command[up].startswith('-'):
        up += 2 if command[up] in value_options else 1
    if up >= len(command) or command[up] not in ('up', 'build'):
        return
    prefix = command[:up]
    result = subprocess.run([*prefix, 'config', '--format', 'json'],
                            capture_output=True, text=True, timeout=30, check=True)
    services = json.loads(result.stdout)['services']
    selected = {value for value in command[up+1:] if value in services}
    if not selected:
        selected = {name for name, value in services.items() if not value.get('profiles')}
    pending = list(selected)
    while pending:
        name = pending.pop()
        for dependency in services[name].get('depends_on') or {}:
            if dependency not in selected:
                selected.add(dependency); pending.append(dependency)
    docker = command[:compose]
    images = {services[name]['image'] for name in selected
              if command[up] == 'up' and services[name].get('image')}
    for name in selected:
        images.update(literal_build_images(services[name].get('build')))
    for image in sorted(images):
        # Retain already verified local images and locally built application
        # images. Docker still verifies registry layers and TLS during pulls.
        exists = subprocess.run([*docker, 'image', 'inspect', image],
                                capture_output=True, timeout=15).returncode == 0
        if not exists:
            pull_image(docker, image)


def main():
    command = sys.argv[1:]
    if not command:
        raise SystemExit('A routed Docker command is required')
    prefetch_compose_images(command)
    # A lab command executes exactly once. Never retry startup mutations,
    # readiness checks, recording checks or test assertions.
    os.execv(command[0], command)


if __name__ == '__main__':
    main()
