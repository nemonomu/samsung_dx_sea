"""Offline checks for the Walmart browser location picker."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from common.walmart_base import WalmartBaseCrawler


class BrowserLocationTests(unittest.TestCase):
    @patch('common.walmart_base.time.sleep')
    @patch('common.walmart_base.ChromiumOptions')
    @patch('common.walmart_base.ChromiumPage')
    @patch('subprocess.run')
    def test_setup_retry_does_not_quit_unowned_browser(self, run, page_class, _options, _sleep):
        crawler = WalmartBaseCrawler.__new__(WalmartBaseCrawler)
        first, second = Mock(), Mock()
        first.run_cdp.side_effect = RuntimeError('startup failed')
        page_class.side_effect = [first, second]
        self.assertTrue(crawler.setup_browser())
        first.quit.assert_not_called()
        second.quit.assert_not_called()
        run.assert_not_called()

    @patch('common.walmart_base.time.sleep')
    @patch('subprocess.run')
    def test_restart_does_not_kill_unrelated_chrome(self, run, _sleep):
        crawler = WalmartBaseCrawler.__new__(WalmartBaseCrawler)
        page = crawler.page = Mock()
        crawler.setup_browser = Mock(return_value=True)
        crawler.initialize_session = Mock()
        self.assertTrue(crawler.restart_browser())
        page.quit.assert_called_once()
        run.assert_not_called()

    @patch('common.walmart_base.time.sleep')
    def test_zip_elsewhere_in_html_does_not_skip_store_selection(self, _sleep):
        crawler = WalmartBaseCrawler.__new__(WalmartBaseCrawler)
        crawler.page = Mock()
        crawler.page.html = '<div>11581 mentioned in unrelated content</div>'
        banner = Mock(text='Sacramento 95829')
        picker, zip_input, store, save = Mock(), Mock(), Mock(), Mock()
        save.click.side_effect = lambda: setattr(banner, 'text', 'Valley Stream 11581')

        def element(selector, timeout):
            if selector == 'css:button[data-automation-id="fulfillment-banner"]':
                return banner
            if selector.startswith('xpath:(//*[@role="dialog"]//button'):
                return picker
            if selector == 'css:input[name="zipCodeOrCityState"]':
                return zip_input
            if selector.startswith('xpath:(//label[@data-automation-id="pickup-store"'):
                return store
            if selector == 'css:button[data-automation-id="save-label"]':
                return save
            return None

        crawler.page.ele.side_effect = element
        crawler.handle_captcha = Mock()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertTrue(crawler.set_walmart_zip_code('11581'))
        self.assertIn('banner displays ZIP 11581', output.getvalue())
        self.assertIn('product data not checked', output.getvalue())
        zip_input.input.assert_called_with('11581', clear=True)
        store.click.assert_called_once()
        save.click.assert_called_once()

    @patch('common.walmart_base.time.sleep')
    def test_correct_banner_skips_store_selection(self, _sleep):
        crawler = WalmartBaseCrawler.__new__(WalmartBaseCrawler)
        crawler.page = Mock()
        crawler.page.html = '<script>product location 95829</script>'
        banner = Mock(text='Valley Stream 11581')
        crawler.page.ele.return_value = banner
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertTrue(crawler.set_walmart_zip_code('11581'))
        self.assertIn('banner displays ZIP 11581', output.getvalue())
        self.assertIn('product data not checked', output.getvalue())
        banner.click.assert_not_called()


if __name__ == '__main__':
    unittest.main()
