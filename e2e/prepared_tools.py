"""Exercise the prepared CI image without accessing external applications."""
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.ci_tools import MARKER,validate_prepared
from playwright.sync_api import sync_playwright


def main():
    started=time.monotonic()
    assert validate_prepared(), 'Prepared tooling manifest is required'
    metadata=json.loads(MARKER.read_text())
    assert platform.python_version()==metadata['python_version']
    tools={}
    for command in [['dockerd','--version'],['docker','--version'],['docker','compose','version'],['docker','buildx','version']]:
        tools[' '.join(command[:-1])]=subprocess.check_output(command,text=True,timeout=10).strip()
    with sync_playwright() as browser:
        process=browser.webkit.launch()
        page=process.new_page()
        page.goto('about:blank')
        assert page.title()==''
        process.close()
    print(json.dumps({'status':'passed','source':metadata['source'],'python':platform.python_version(),
        'browser':metadata['playwright_version'],'tools':tools,'seconds':round(time.monotonic()-started,3)}))


if __name__=='__main__':main()
