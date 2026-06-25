"""
HHP Crawling Monitoring and Alert Module
- Crawling result analysis
- Email alert when issues detected
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import pytz

from config import EMAIL_CONFIG


def format_elapsed_time(seconds, short=False):
    """초 단위를 시간 형식으로 변환

    Args:
        seconds: 초 단위 시간
        short: True면 '1h 10m 11s' 형식, False면 '1234.5 seconds (1h 10m 11s)' 형식
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hours > 0:
        time_str = f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        time_str = f"{minutes}m {secs}s"
    else:
        time_str = f"{secs}s"

    if short:
        return time_str
    else:
        return f"{seconds:.1f} seconds ({time_str})"


def send_crawl_alert(retailer, results, failed_stages, elapsed_time, error_message=None, resume_from=None, test_mode=False, start_time_kst=None, start_time_server=None):
    """
    Send crawling completion/failure email alert for integrated crawlers.

    Args:
        retailer: Retailer name (e.g., 'Amazon HHP', 'Walmart HHP', 'BestBuy HHP')
        results: Dictionary of stage results {stage_name: success_bool or 'skipped'}
        failed_stages: List of failed stage names
        elapsed_time: Total elapsed time in seconds
        error_message: Additional error message (optional)
        resume_from: Resume stage name if resumed (optional)
        test_mode: If True, adds [TEST] prefix to email subject (default: False)
        start_time_kst: Crawler start time in KST timezone (optional)
        start_time_server: Crawler start time in server local timezone (optional)

    Returns:
        bool: Email send success status

    Usage example:
        from common.alert_monitor import send_crawl_alert

        # After crawling complete
        send_crawl_alert(
            retailer='Amazon HHP',
            results={'main': True, 'bsr': False, 'detail': True},
            failed_stages=['bsr'],
            elapsed_time=3600.5
        )

        # On fatal error
        send_crawl_alert(
            retailer='Amazon HHP',
            results={},
            failed_stages=['Fatal error'],
            elapsed_time=0,
            error_message='ChromeDriver initialization failed'
        )

        # Resume from detail
        send_crawl_alert(
            retailer='Amazon HHP',
            results=crawl_results,
            failed_stages=[],
            elapsed_time=1234.5,
            resume_from='detail'
        )
    """
    try:
        korea_tz = pytz.timezone('Asia/Seoul')
        now_kst = datetime.now(korea_tz)
        now_server = datetime.now()

        # Start times from crawler
        start_kst = start_time_kst
        start_server = start_time_server

        # Determine alert level
        is_critical = len(failed_stages) > 0 or error_message is not None

        # Generate email subject
        test_tag = "[TEST] " if test_mode else ""
        resume_tag = f" (Resume: {resume_from})" if resume_from else ""
        if is_critical:
            subject = f"{test_tag}[CRITICAL] {retailer} Crawler Alert{resume_tag} - {now_kst.strftime('%Y-%m-%d %H:%M')}"
        else:
            subject = f"{test_tag}[OK] {retailer} Crawler Report{resume_tag} - {now_kst.strftime('%Y-%m-%d %H:%M')}"

        # Build results table rows
        results_rows = ""
        for stage_name, result in results.items():
            # Handle 'skipped', dict {'success': bool, 'duration': float}, bool, or None
            if result == 'skipped':
                status = '<span style="color: #6c757d;">SKIPPED</span>'
                duration_str = '-'
            elif result is None:
                status = '<span style="color: #6c757d;">SKIPPED</span>'
                duration_str = '-'
            elif isinstance(result, dict):
                # {'success': bool, 'duration': float} 형태
                success = result.get('success')
                duration = result.get('duration')
                if success:
                    status = '<span style="color: #28a745;">SUCCESS</span>'
                else:
                    status = '<span style="color: #dc3545;">FAILED</span>'
                duration_str = format_elapsed_time(duration, short=True) if duration is not None else '-'
            elif result:
                status = '<span style="color: #28a745;">SUCCESS</span>'
                duration_str = '-'
            else:
                status = '<span style="color: #dc3545;">FAILED</span>'
                duration_str = '-'

            results_rows += f"""
                <tr>
                    <td>{stage_name}</td>
                    <td>{status}</td>
                    <td>{duration_str}</td>
                </tr>
            """

        # Generate email body (HTML)
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ font-family: 'Malgun Gothic', Arial, sans-serif; }}
                .critical {{ color: #dc3545; font-weight: bold; }}
                .success {{ color: #28a745; font-weight: bold; }}
                table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                th {{ background-color: #4CAF50; color: white; }}
                tr:nth-child(even) {{ background-color: #f2f2f2; }}
                .header {{ background-color: #333; color: white; padding: 15px; }}
                .section {{ margin: 20px 0; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h2>{retailer} Crawler Report</h2>
            </div>

            <div class="section">
                <h3>Execution Time</h3>
                <table>
                    <tr>
                        <th>Item</th>
                        <th>KST (Korea)</th>
                        <th>Server Time</th>
                    </tr>
                    <tr>
                        <td>Start Time</td>
                        <td>{start_kst.strftime('%Y-%m-%d %H:%M:%S') if start_kst else '-'}</td>
                        <td>{start_server.strftime('%Y-%m-%d %H:%M:%S') if start_server else '-'}</td>
                    </tr>
                    <tr>
                        <td>End Time</td>
                        <td>{now_kst.strftime('%Y-%m-%d %H:%M:%S')}</td>
                        <td>{now_server.strftime('%Y-%m-%d %H:%M:%S')}</td>
                    </tr>
                </table>
            </div>

            <div class="section">
                <h3>Execution Summary</h3>
                <table>
                    <tr>
                        <th>Item</th>
                        <th>Value</th>
                    </tr>
                    <tr>
                        <td>Total Elapsed Time</td>
                        <td>{format_elapsed_time(elapsed_time)}</td>
                    </tr>
                    <tr>
                        <td>Overall Status</td>
                        <td>{'<span class="critical">FAILED</span>' if is_critical else '<span class="success">SUCCESS</span>'}</td>
                    </tr>
                </table>
            </div>

            <div class="section">
                <h3>Stage Results</h3>
                <table>
                    <tr>
                        <th>Stage</th>
                        <th>Status</th>
                        <th>Duration</th>
                    </tr>
                    {results_rows}
                </table>
            </div>
        """

        # Add error message section if present
        if error_message:
            html_content += f"""
            <div class="section">
                <h3>Error Details</h3>
                <div style="background-color: #f8d7da; border: 1px solid #f5c6cb; border-radius: 4px; padding: 10px; color: #721c24;">
                    {error_message}
                </div>
            </div>
            """

        # Add failed stages section if present
        if failed_stages:
            failed_list = "".join([f"<li>{stage}</li>" for stage in failed_stages])
            html_content += f"""
            <div class="section">
                <h3>Failed Stages</h3>
                <ul class="critical">
                    {failed_list}
                </ul>
            </div>
            """

        html_content += """
            <div class="section">
                <p style="color: #666; font-size: 12px;">
                    This email was sent automatically. If issues persist, please check the crawler logs.
                </p>
            </div>
        </body>
        </html>
        """

        # Create email
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = EMAIL_CONFIG['sender_email']
        msg['To'] = EMAIL_CONFIG['receiver_email']

        msg.attach(MIMEText(html_content, 'html'))

        # Send email
        with smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port']) as server:
            server.starttls()
            server.login(EMAIL_CONFIG['sender_email'], EMAIL_CONFIG['sender_password'])
            server.sendmail(
                EMAIL_CONFIG['sender_email'],
                EMAIL_CONFIG['receiver_email'],
                msg.as_string()
            )

        print(f"[OK] Alert email sent: {subject}")
        return True

    except Exception as e:
        print(f"[ERROR] Failed to send crawl alert email: {e}")
        return False
