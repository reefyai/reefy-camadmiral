"""Bound verified APT acquisition in disposable CI guests, never retry install."""
from __future__ import annotations
import json
import os
import re
import signal
import subprocess
import sys
import time

ATTEMPTS = 6
COMMAND_TIMEOUT = 180
BUDGET_SECONDS = 900
APT = '/usr/bin/apt-get'
OPTIONS = ['-o', 'Acquire::Retries=0', '-o', 'Acquire::http::Timeout=30',
           '-o', 'Acquire::https::Timeout=30', '-o', 'APT::Update::Error-Mode=any']


def retryable(detail):
    detail = detail.lower()
    permanent = ('certificate verification failed', 'certificate verify failed', 'not signed',
                 'signatures couldn\'t be verified', 'invalid signature', 'no_pubkey',
                 'signatures were invalid', 'certificate is not trusted',
                 'does not have a release file', '401 unauthorized', '403 forbidden',
                 'no space left', 'unable to locate package', 'unmet dependencies',
                 'dpkg was interrupted', 'could not get lock')
    transient = ('bad record mac', 'decryption failed', 'unexpected eof', 'connection reset',
                 'connection timed out', 'connection failed', 'temporarily unavailable',
                 'temporary failure resolving', 'could not resolve', 'failed to fetch',
                 'hash sum mismatch', 'file has unexpected size', '429 too many requests')
    return (not any(x in detail for x in permanent)
            and not re.search(r'\b(?:401\s+unauthorized|403\s+forbidden)\b', detail)
            and any(x in detail for x in transient))


def command(arguments, timeout):
    environment = dict(os.environ, DEBIAN_FRONTEND='noninteractive', LC_ALL='C')
    child = subprocess.Popen([APT, *OPTIONS, *arguments], env=environment,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, start_new_session=True)
    try:
        output, _ = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Only this command's newly owned process group, including transports.
        try: os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: child.communicate(timeout=3)
        except subprocess.TimeoutExpired: pass
        try: os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        output, _ = child.communicate()
        print(output, end='', flush=True)
        raise TimeoutError('CI package acquisition command exceeded its deadline') from None
    print(output, end='', flush=True)
    return child.returncode, output


def acquire(arguments, *, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + min(3600, max(1, int(os.environ.get('CAMADMIRAL_CI_PACKAGE_BUDGET_SECONDS', BUDGET_SECONDS))))
    timeout = min(600, max(1, int(os.environ.get('CAMADMIRAL_CI_PACKAGE_COMMAND_SECONDS', COMMAND_TIMEOUT))))
    for attempt in range(1, ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('CI package acquisition exhausted its total budget')
        started = time.monotonic()
        try:
            status, output = command(arguments, min(timeout, remaining))
        except TimeoutError:
            status, output = 124, ''
        if status == 0:
            return
        if attempt == ATTEMPTS or (status != 124 and not retryable(output)):
            raise subprocess.CalledProcessError(status, [APT, *arguments])
        backoff = min(5 * attempt, max(0, deadline - time.monotonic()))
        print('CI package acquisition retry: ' + json.dumps({
            'operation': arguments[0], 'attempt': attempt, 'status': status,
            'seconds': round(time.monotonic() - started, 2), 'backoff_seconds': backoff}), flush=True)
        time.sleep(backoff)
        # A verified fresh index repairs mirror churn and stale package URLs.
        if arguments[0] != 'update':
            acquire(['update'], deadline=deadline)


def apt_get(arguments):
    if not arguments or arguments[0] not in ('update', 'install'):
        raise ValueError('CI package helper supports only update and install')
    if arguments[0] == 'update':
        acquire(arguments)
        return
    acquire(['install', '--download-only', *arguments[1:]])
    # Package configuration can have side effects. Run it once after every
    # archive is acquired and verified, without further APT downloads or retries.
    status, _ = command(['install', '--no-download', *arguments[1:]], COMMAND_TIMEOUT)
    if status:
        raise subprocess.CalledProcessError(status, [APT, *arguments])


if __name__ == '__main__':
    apt_get(sys.argv[1:])
