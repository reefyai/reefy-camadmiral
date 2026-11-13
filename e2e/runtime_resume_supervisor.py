"""Test-only control module; all shipped supervisor/health behavior stays active."""
from camadmiral import supervisor

original_start = supervisor.Child.start

def start(child):
    if child.name == 'control':
        child.command = [arg.replace('camadmiral.app:app', 'runtime_resume_control:app') for arg in child.command]
    return original_start(child)

supervisor.Child.start = start
raise SystemExit(supervisor.main())
