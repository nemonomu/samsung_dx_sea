import contextlib
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from walmart.tv.test_wmart_tv_render import Clock, Page, html
from walmart.tv.wmart_tv_next_data import WalmartNextDataClient, read_zenrows_api_key
from walmart.tv.wmart_tv_render import WalmartTVBrowserRenderer

URL = 'https://www.walmart.com/ip/example/123?classType=REGULAR&token=secret-value'


class TransportLogTests(unittest.TestCase):
    def test_fallback_records_failures_selection_and_redacts_query_and_exception(self):
        client = WalmartNextDataClient(renderer=Mock())
        client.fetch_direct_html = Mock(side_effect=RuntimeError('https://provider/?apikey=secret-value'))
        client.renderer.fetch_html.return_value = ({'content_valid': False, 'blocked': True}, '')
        client.fetch_zenrows_html = Mock(return_value=({'status': 200, 'blocked': False}, html()))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = client.fetch_next_data(URL)
        lines = output.getvalue()
        self.assertEqual(result['source'], 'zenrows_static')
        self.assertIn('route=direct', lines)
        self.assertIn('error_class=RuntimeError', lines)
        self.assertIn('next=browser_render', lines)
        self.assertIn('route=browser_render', lines)
        self.assertIn('reason=blocked next=zenrows_static', lines)
        self.assertIn('route=zenrows_static', lines)
        self.assertIn('selected=True', lines)
        self.assertIn('timestamp=', lines)
        self.assertNotIn('secret-value', lines)
        self.assertNotIn('apikey', lines)

    def test_readiness_reports_success_and_timeout_elapsed(self):
        clock = Clock(); page = Page([html()]); page.urls.append(URL)
        renderer = WalmartTVBrowserRenderer(); renderer._page = page
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('walmart.tv.wmart_tv_render.time.monotonic', clock.monotonic), patch('walmart.tv.wmart_tv_render.time.sleep', clock.sleep):
            self.assertIsNotNone(renderer._wait_ready(URL, 3)[1])
            page.contents = ['Robot or human']
            self.assertIsNone(renderer._wait_ready(URL, 3)[1])
        self.assertIn('event=readiness_start', output.getvalue())
        self.assertIn('budget_s=3', output.getvalue())
        self.assertIn('elapsed_ms=0 status=None reason=ready', output.getvalue())
        self.assertIn('elapsed_ms=3000 status=None reason=blocked', output.getvalue())

    def test_direct_200_with_block_page_does_not_retry(self):
        client = WalmartNextDataClient()
        client.session.get = Mock(return_value=Mock(
            status_code=200, text='Robot or human', url=URL,
        ))
        meta, _ = client.fetch_direct_html(URL)
        self.assertTrue(meta['blocked'])
        self.assertEqual(meta['status'], 200)
        client.session.get.assert_called_once()

    def test_provider_key_prefers_environment_and_preserves_file_fallback(self):
        with TemporaryDirectory() as folder:
            config = Path(folder) / 'config.py'
            config.write_text('ZENROWS_API_KEY = "fixture-file"', encoding='utf-8')
            with patch.dict(os.environ, {'ZENROWS_API_KEY': ' fixture-env '}):
                self.assertEqual(read_zenrows_api_key(config), 'fixture-env')
            with patch.dict(os.environ, {'ZENROWS_API_KEY': ''}):
                self.assertEqual(read_zenrows_api_key(config), 'fixture-file')

    def test_provider_calls_and_selection_are_distinct_without_renderer(self):
        client = WalmartNextDataClient(direct_enabled=False)
        client.zenrows_api_key = 'secret-value'
        response = Mock(status_code=200, text=html(), url='https://provider/?apikey=secret-value')
        output = io.StringIO()
        with patch('walmart.tv.wmart_tv_next_data.requests.get', return_value=response), contextlib.redirect_stdout(output):
            result = client.fetch_next_data(URL, js_render_fallback=False)
        self.assertEqual(result['source'], 'zenrows_static')
        self.assertIn('event=request route=zenrows_static', output.getvalue())
        self.assertIn('selected=True', output.getvalue())
        self.assertNotIn('secret-value', output.getvalue())


if __name__ == '__main__':
    unittest.main()
