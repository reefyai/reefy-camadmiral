"""Small, dependency-free timing recorder for sequential E2E work."""
from contextlib import contextmanager
import json
from pathlib import Path
import time


class Timings:
    def __init__(self, path: Path):
        self.path = path
        self.records = []

    def reset(self):
        self.records.clear()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('', encoding='utf-8')

    @contextmanager
    def measure(self, kind, name):
        started = time.monotonic()
        status = 'passed'
        print(f'E2E START [{kind}] {name}', flush=True)
        try:
            yield
        except BaseException:
            status = 'failed'
            raise
        finally:
            record = {'kind': kind, 'name': name, 'seconds': round(time.monotonic() - started, 3),
                      'status': status}
            self.records.append(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as output:
                output.write(json.dumps(record) + '\n')
            print(f'E2E END [{kind}] {name}: {status} ({record["seconds"]:.1f}s)', flush=True)

    def summary(self):
        # Commands nest inside scenarios. Never sum categories into a total.
        lines = ['## E2E timing', '', 'Categories overlap; durations must not be added together.', '']
        for kind in sorted({item['kind'] for item in self.records}):
            lines.extend([f'### {kind}', '', '| Work | Seconds | Result |', '|---|---:|---|'])
            for item in sorted((r for r in self.records if r['kind'] == kind),
                               key=lambda r: r['seconds'], reverse=True)[:15]:
                name = item['name'].replace('|', '\\|').replace('\n', ' ')
                lines.append(f'| {name} | {item["seconds"]:.1f} | {item["status"]} |')
            lines.append('')
        return '\n'.join(lines)
