"""Refuse to overwrite a published version; verify exact source on retries."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

if __package__:
    from .ci_image_retry import transient_download_failure
else:
    from ci_image_retry import transient_download_failure


def get_json(url, headers=None, expected=None):
    for attempt in range(1, 7):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=30) as response:
                payload = response.read()
            if expected and 'sha256:' + hashlib.sha256(payload).hexdigest() != expected:
                raise ValueError('Registry content digest does not match its manifest')
            return json.loads(payload)
        except (urllib.error.URLError, TimeoutError) as error:
            transient = isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError)
            if (isinstance(error, urllib.error.HTTPError) or attempt == 6
                    or not (transient or transient_download_failure(str(error)))):
                raise
            print(f'Registry metadata transfer retry {attempt}/6', flush=True)
            time.sleep(5 * attempt)


def needs_publish(repository, version, source, fetch=get_json):
    if not re.fullmatch(r'[a-z0-9_.-]+/[a-z0-9_.-]+', repository):
        raise ValueError('Invalid image repository')
    if not re.fullmatch(r'v\d{4}\.\d{2}\.\d{2}-\d{2}', version) or not re.fullmatch(r'[0-9a-f]{40}', source):
        raise ValueError('Exact first-party version and source are required')
    token = fetch(f'https://ghcr.io/token?service=ghcr.io&scope=repository:{repository}:pull')['token']
    headers = {'Authorization': 'Bearer ' + token, 'Accept': ','.join([
        'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json',
        'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'])}
    base = f'https://ghcr.io/v2/{repository}/'
    try:
        manifest = fetch(base + 'manifests/' + version, headers)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return True
        raise
    if 'manifests' in manifest:
        platform = next((m for m in manifest['manifests'] if m.get('platform', {}).get('os') == 'linux'
                         and m['platform'].get('architecture') == 'amd64'), None)
        if not platform:
            raise ValueError('Published version does not contain the required Linux amd64 platform')
        manifest = fetch(base + 'manifests/' + platform['digest'], headers, platform['digest'])
    config_digest = manifest['config']['digest']
    config = fetch(base + 'blobs/' + config_digest, headers, config_digest)
    labels = config.get('config', {}).get('Labels') or {}
    if (labels.get('org.opencontainers.image.version') != version
            or labels.get('org.opencontainers.image.revision') != source):
        raise ValueError('Published version has different or missing provenance; refusing to overwrite it')
    return False


def main():
    publish = needs_publish(os.environ['GITHUB_REPOSITORY'], os.environ['RELEASE_VERSION'], os.environ['RELEASE_SOURCE'])
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write('publish=' + str(publish).lower() + '\n')
    print('New immutable version may be published' if publish else 'Retaining existing exact-source image version')


if __name__ == '__main__':
    main()
