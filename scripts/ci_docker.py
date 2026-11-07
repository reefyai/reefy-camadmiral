"""A private Docker daemon inside an Actions job container, never the host daemon."""
from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
DATA_VOLUME = Path('/var/lib/camadmiral-ci-docker')


def owner(environment):
    return ':'.join(environment[key] for key in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB'))


def private_environment(root, environment):
    result = dict(environment)
    for key in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_TLS_CERTDIR', 'DOCKER_API_VERSION'):
        result.pop(key, None)
    result['DOCKER_HOST'] = 'unix://' + str(root / 'docker.sock')
    result['DOCKER_CONFIG'] = str(root / 'config')
    return result


def prepare(environment):
    root = Path(tempfile.mkdtemp(prefix='camadmiral-docker-', dir=environment['RUNNER_TEMP']))
    (root / 'config').mkdir(mode=0o700)
    config = root / 'config/config.json'
    config.write_text('{"auths":{}}\n')
    config.chmod(0o600)
    (root / 'owner.json').write_text(json.dumps({'owner': owner(environment)}))
    (root / 'daemon.json').write_text('{}\n')
    return root


def owned_root(value, environment):
    root = Path(value)
    if (root.is_symlink() or not root.name.startswith('camadmiral-docker-')
            or root.parent.resolve() != Path(environment['RUNNER_TEMP']).resolve()):
        raise ValueError('Refusing Docker cleanup outside this job temporary directory')
    if json.loads((root / 'owner.json').read_text())['owner'] != owner(environment):
        raise ValueError('Refusing Docker cleanup for a different job')
    return root


def prepare_data(root, directory, environment):
    if any(directory.iterdir()):
        raise ValueError('Docker job storage must start empty')
    (directory / 'owner.json').write_text(json.dumps({'owner': owner(environment), 'root': str(root)}))


def owned_data(root, directory, environment):
    marker = json.loads((directory / 'owner.json').read_text())
    if marker != {'owner': owner(environment), 'root': str(root)}:
        raise ValueError('Refusing cleanup of storage not owned by this Docker job')
    return directory / 'data'


def command(root, *arguments, check=True, timeout=60):
    result = subprocess.run(['docker', *arguments], env=private_environment(root, os.environ),
                            text=True, capture_output=True, check=False, timeout=timeout)
    if check and result.returncode:
        print(result.stderr[-8000:], file=sys.stderr, flush=True)
        result.check_returncode()
    return result


def enable_cgroup_nesting():
    # This is the job container's private cgroup namespace, not the host root.
    group = Path('/sys/fs/cgroup')
    controllers = (group / 'cgroup.controllers').read_text().split()
    if not {'memory', 'pids'} <= set(controllers):
        raise RuntimeError('Nested Docker requires memory and PID controllers')
    initial = group / 'init'
    initial.mkdir(exist_ok=True)
    deadline = time.monotonic() + 15
    while True:
        for pid in (group / 'cgroup.procs').read_text().split():
            try:
                (initial / 'cgroup.procs').write_text(pid)
            except ProcessLookupError:
                pass
        try:
            (group / 'cgroup.subtree_control').write_text(' '.join('+' + item for item in controllers))
            return
        except OSError as error:
            if error.errno != errno.EBUSY or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def verified_limits(output):
    if output.split() != ['33554432', '32']:
        raise RuntimeError('Private Docker did not enforce the requested memory and PID limits')
    return {'memory_max': 33554432, 'pids_max': 32}


def fingerprint(info):
    return hashlib.sha256(info['ID'].encode()).hexdigest()


def client_wrapper(root, binary='/usr/bin/camadmiral-docker-cli'):
    # GITHUB_ENV also affects the outer runner's Docker exec commands. Route
    # only the client inside this ephemeral job container to its own daemon.
    return ('#!/bin/sh\n'
            'unset DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_TLS_CERTDIR DOCKER_API_VERSION\n'
            'exec ' + shlex.quote(str(binary)) + ' --config ' + shlex.quote(str(root / 'config'))
            + ' --host ' + shlex.quote('unix://' + str(root / 'docker.sock')) + ' "$@"\n')


def process_running(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


def private_mountpoints(root, data, mountinfo):
    # mountinfo escapes spaces and backslashes with octal sequences. Match
    # complete owned path components, excluding the parent storage volume.
    paths = set()
    for line in mountinfo.splitlines():
        value = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), line.split()[4])
        path = Path(value)
        if path.is_relative_to(root) or path.is_relative_to(data):
            paths.add(path)
    return sorted(paths, key=lambda path: len(path.parts), reverse=True)


def start():
    if (os.environ.get('CAMADMIRAL_CI_JOB_CONTAINER') != '1'
            or not Path('/.dockerenv').exists() or os.geteuid() != 0):
        raise RuntimeError('Private Docker requires the declared privileged Actions job container')
    artifacts = ROOT / 'e2e-artifacts/docker-lab'
    artifacts.mkdir(parents=True, exist_ok=True)
    root = prepare(os.environ)
    with open(os.environ['GITHUB_STATE'], 'a') as state:
        state.write('docker_root=' + str(root) + '\n')
    if not DATA_VOLUME.is_mount():
        raise RuntimeError('Private Docker requires its declared anonymous job storage volume')
    prepare_data(root, DATA_VOLUME, os.environ)
    # Service managers must not start a second daemon at the default socket.
    policy = Path('/usr/sbin/policy-rc.d')
    policy.write_text('#!/bin/sh\nexit 101\n')
    policy.chmod(0o755)
    sources = Path('/etc/apt/sources.list.d/ubuntu.sources')
    if sources.exists():
        sources.write_text(sources.read_text().replace('http://archive.ubuntu.com/', 'https://archive.ubuntu.com/')
                           .replace('http://security.ubuntu.com/', 'https://security.ubuntu.com/'))
    environment = dict(os.environ, DEBIAN_FRONTEND='noninteractive')
    subprocess.run(['apt-get', '-o', 'Acquire::Retries=3', 'update'], env=environment, check=True)
    subprocess.run(['apt-get', '-o', 'Acquire::Retries=3', 'install', '-y', '--no-install-recommends',
                    'docker.io', 'docker-compose-v2', 'docker-buildx'], env=environment, check=True)
    enable_cgroup_nesting()
    log = (artifacts / 'dockerd.log').open('w')
    daemon = subprocess.Popen(['dockerd', '--config-file', str(root / 'daemon.json'),
        '--host', 'unix://' + str(root / 'docker.sock'), '--data-root', str(DATA_VOLUME / 'data'),
        '--exec-root', str(root / 'exec'), '--pidfile', str(root / 'dockerd.pid'),
        '--exec-opt', 'native.cgroupdriver=cgroupfs'],
        env=private_environment(root, os.environ), stdout=log, stderr=subprocess.STDOUT)
    metadata = {'owner': owner(os.environ), 'pid': daemon.pid}
    (root / 'owner.json').write_text(json.dumps(metadata))
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and daemon.poll() is None:
        result = command(root, 'info', '--format', '{{json .}}', check=False, timeout=5)
        if result.returncode == 0:
            info = json.loads(result.stdout)
            metadata['daemon'] = fingerprint(info)
            (root / 'owner.json').write_text(json.dumps(metadata))
            if str(info['CgroupVersion']) != '2' or not info['MemoryLimit'] or not info['PidsLimit']:
                raise RuntimeError('Docker lab must enforce real cgroup v2 memory and PID limits')
            probe = command(root, 'run', '--rm', '--memory=32m', '--memory-swap=32m', '--pids-limit=32',
                            'alpine:3.22', 'sh', '-c',
                            'cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max', timeout=120)
            limits = verified_limits(probe.stdout)
            client = Path('/usr/bin/docker')
            client.rename('/usr/bin/camadmiral-docker-cli')
            client.write_text(client_wrapper(root))
            client.chmod(0o755)
            revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
            (artifacts / 'readiness.json').write_text(json.dumps({
                'status': 'ready', 'revision': revision, 'daemon_fingerprint': metadata['daemon'],
                'cgroup_version': info['CgroupVersion'], 'memory_limit': info['MemoryLimit'],
                'pid_limit': info['PidsLimit'], 'storage_driver': info['Driver'],
                'server_version': info['ServerVersion'], 'private_credentials': True,
                'enforced_limits': limits,
            }, indent=2) + '\n')
            print('Private Docker ready with cgroup v2 memory and PID enforcement', flush=True)
            return
        time.sleep(1)
    raise RuntimeError('Private Docker did not become ready; see the retained daemon log')


def stop():
    value = os.environ.get('STATE_docker_root')
    if not value:
        return
    root = owned_root(value, os.environ)
    metadata = json.loads((root / 'owner.json').read_text())
    data = owned_data(root, DATA_VOLUME, os.environ)
    if metadata.get('daemon'):
        result = command(root, 'info', '--format', '{{json .}}', check=False, timeout=10)
        if result.returncode == 0:
            if fingerprint(json.loads(result.stdout)) != metadata['daemon']:
                raise RuntimeError('Refusing cleanup of a daemon not created by this job')
            containers = command(root, 'ps', '-aq').stdout.split()
            if containers:
                command(root, 'rm', '-f', *containers)
            command(root, 'system', 'prune', '--all', '--force', '--volumes')
    pid = metadata.get('pid')
    if pid and process_running(pid):
        args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        if str(data).encode() not in args:
            raise RuntimeError('Refusing to stop a process outside this Docker lab')
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process_running(pid):
            time.sleep(0.2)
        if process_running(pid):
            os.kill(pid, signal.SIGKILL)
    # Docker can leave data-root and network-namespace bind mounts after exit.
    # Detach only mounts beneath validated owned paths, deepest first.
    for mount in private_mountpoints(root, data, Path('/proc/self/mountinfo').read_text()):
        subprocess.run(['umount', '--lazy', str(mount)], check=False, capture_output=True)
    if data.exists():
        shutil.rmtree(data)
    (DATA_VOLUME / 'owner.json').unlink()
    shutil.rmtree(root)
    print('Removed only this job private Docker resources', flush=True)


if __name__ == '__main__':
    {'start': start, 'stop': stop}[sys.argv[1]]()
