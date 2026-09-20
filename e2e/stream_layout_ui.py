"""Browser regression for stream layout using synthetic data, without Docker."""
from pathlib import Path

from playwright.sync_api import sync_playwright, expect


def main():
    html = (Path(__file__).resolve().parents[1] / 'camadmiral/index.html').read_text()
    with sync_playwright() as playwright:
        browser = playwright.webkit.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 1000})
        page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
        page.add_init_script('window.fetch = () => new Promise(() => {}); window.setInterval = () => 0;')
        page.goto('http://synthetic.invalid')
        page.evaluate('''() => {
          mediaAccessFailed = true;
          window.sourceRequests = 0;
          window.fetch = async (url, options) => {
            if (url.endsWith('/source-access')) {
              window.sourceRequests++;
              if (options.headers['X-CamAdmiral-Action'] !== 'reveal-camera-source') throw Error('Missing action');
              return {ok: true, json: async () => ({uri: 'rtsp://viewer:synthetic-secret@192.0.2.10/main'})};
            }
            return new Promise(() => {});
          };
          window.syntheticCamera = {candidate_uuid: 'synthetic', display_name: 'Z adopted',
            ip: '192.0.2.10', online: true, rtsp: [], adoption: {
              camera_uuid: 'synthetic-camera', enabled: true,
              roles: {record: 'main', detect: 'sub'}, streams: ['main', 'sub'].map(id => ({
                stream_uuid: id, stream_key: id, profile_token: id, name: id,
                enabled: true, uri: 'rtsp://192.0.2.10/' + id,
                health_status: 'healthy', width: 640, height: 360, encoding: 'H264', fps: 20
              }))}};
          devices = [syntheticCamera, {candidate_uuid: 'other', display_name: 'A unadopted',
            ip: '192.0.2.11', online: true, rtsp: []}];
          renderRows();
        }''')
        for direction in (1, -1):
            page.evaluate('(direction) => {sortDirection = direction; renderRows();}', direction)
            expect(page.locator('#camera-rows tr.camera-row').first).to_contain_text('Z adopted')
        page.get_by_role('button', name='Streams', exact=True).click()
        expect(page.get_by_role('combobox', name='Record', exact=True)).to_have_value('main')
        expect(page.get_by_role('combobox', name='Record', exact=True).locator('option[value="main"]')).to_have_text('main · 640 × 360 · H264 · 20 fps')
        page.get_by_role('combobox', name='Record', exact=True).select_option('sub')
        expect(page.get_by_role('combobox', name='Detect', exact=True)).to_have_value('sub')
        page.get_by_label('main settings', exact=True).get_by_label('Enabled', exact=True).uncheck()
        expect(page.get_by_role('combobox', name='Record', exact=True).locator('option')).to_have_count(2)
        expect(page.get_by_role('combobox', name='Record', exact=True)).to_have_value('sub')
        assert page.evaluate('streamSettingsDrafts.get("synthetic-camera").roles') == {'record': 'sub', 'detect': 'sub'}
        assert page.locator('.camera-source-url').count() == 2
        assert page.locator('.camera-source-url[open]').count() == 0
        assert page.evaluate('sourceRequests') == 0
        expect(page.get_by_role('button', name='Copy camera source URL', exact=True).first).not_to_be_visible()
        page.locator('.camera-source-url summary').first.click()
        expect(page.locator('.camera-source-url .downstream-url').first).to_have_text('rtsp://viewer:********@192.0.2.10/main')
        assert 'synthetic-secret' not in page.locator('.stream-details').inner_text()
        assert '********' in page.locator('.camera-source-url').first.inner_text()
        assert page.evaluate('maskedSourceUrl("rtsp://viewer:secret@192.0.2.10/live?password=secret")').count('secret') == 0
        page.evaluate('window.copied = null; copyText = value => {window.copied = value;}')
        page.get_by_role('button', name='Copy camera source URL', exact=True).first.click()
        assert page.evaluate('window.copied') == 'rtsp://viewer:synthetic-secret@192.0.2.10/main'
        for width in (1280, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('''() => {
              const roles = [...document.querySelectorAll('.stream-role-choice')].map(el => el.getBoundingClientRect());
              const identity = document.querySelector('.stream-identity');
              return roles[1].top >= roles[0].bottom &&
                identity.querySelector('.stream-enabled-control').getBoundingClientRect().top >=
                identity.querySelector('.profile-name').getBoundingClientRect().bottom + 12;
            }''')
            assert page.evaluate('''() => [...document.querySelectorAll('.stream-access')].every(el => {
              const source = el.querySelector('.camera-source-url').getBoundingClientRect();
              const downstream = el.querySelector('.downstream-row').getBoundingClientRect();
              return source.top >= downstream.bottom && source.right <= innerWidth;
            })''')
        page.get_by_label('sub settings', exact=True).get_by_label('Enabled', exact=True).uncheck()
        expect(page.locator('.camera-source-url').first).to_have_attribute('open', '')
        expect(page.get_by_role('button', name='Copy camera source URL', exact=True).first).to_be_enabled()
        assert page.evaluate('sourceRequests') == 1
        page.locator('.camera-source-url summary').first.click()
        page.evaluate('renderActiveCameraModal()')
        expect(page.locator('.camera-source-url').first).not_to_have_attribute('open', '')
        assert page.evaluate('streamSettingsDrafts.get("synthetic-camera").roles') == {}
        page.get_by_role('button', name='Save stream settings', exact=True).click()
        expect(page.get_by_text('Choose an enabled stream for both Record and Detect.', exact=True)).to_be_visible()
        browser.close()
    print('Stream layout, masking, role dropdowns, and adopted-first browser checks passed.')


if __name__ == '__main__':
    main()
