"""
Email (SMTP) notification service.
"""
import logging
from html import escape as _esc
from typing import Optional, Dict, Any, List
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from config import settings

logger = logging.getLogger(__name__)


class EmailNotifier:
    """
    Email notification service client using SMTP.

    Sends email notifications for page changes.
    """

    def __init__(
        self,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_username: Optional[str] = None,
        smtp_password: Optional[str] = None,
        from_email: Optional[str] = None,
        from_name: Optional[str] = None,
        use_tls: Optional[bool] = None,
    ):
        """
        Initialize Email notifier.

        Args:
            smtp_host: SMTP server hostname
            smtp_port: SMTP server port (25, 465, 587)
            smtp_username: SMTP username
            smtp_password: SMTP password
            from_email: Sender email address
            from_name: Sender display name
            use_tls: Whether to use TLS/STARTTLS
        """
        self.smtp_host = smtp_host or getattr(settings, 'smtp_host', None)
        self.smtp_port = smtp_port or getattr(settings, 'smtp_port', 587)
        self.smtp_username = smtp_username or getattr(settings, 'smtp_username', None)
        self.smtp_password = smtp_password or getattr(settings, 'smtp_password', None)
        self.from_email = from_email or getattr(settings, 'smtp_from_email', None)
        self.from_name = from_name or getattr(settings, 'smtp_from_name', 'Writ')
        self.use_tls = use_tls if use_tls is not None else getattr(settings, 'smtp_use_tls', True)

        self.enabled = bool(
            self.smtp_host
            and self.smtp_port
            and self.smtp_username
            and self.smtp_password
            and self.from_email
        )

        if not self.enabled:
            logger.warning("Email SMTP credentials not fully configured")

    def send_email(
        self,
        to_email: str | List[str],
        subject: str,
        body: str,
        html_body: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send an email via SMTP.

        Args:
            to_email: Recipient email address(es)
            subject: Email subject
            body: Plain text email body
            html_body: Optional HTML email body

        Returns:
            Response dict with status

        Raises:
            Exception: If email fails to send
        """
        if not self.enabled:
            logger.error("Email SMTP is not configured")
            raise ValueError("Email SMTP credentials not configured")

        # Convert single email to list
        recipients = [to_email] if isinstance(to_email, str) else to_email

        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = formataddr((self.from_name, self.from_email))
        msg['To'] = ', '.join(recipients)

        # Add plain text part
        part1 = MIMEText(body, 'plain', 'utf-8')
        msg.attach(part1)

        # Add HTML part if provided
        if html_body:
            part2 = MIMEText(html_body, 'html', 'utf-8')
            msg.attach(part2)

        try:
            # Connect to SMTP server
            if self.use_tls:
                # Use STARTTLS (typically port 587)
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10)
                server.ehlo()
                server.starttls()
                server.ehlo()
            else:
                # Direct SSL connection (typically port 465)
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=10)

            # Login
            if self.smtp_username and self.smtp_password:
                server.login(self.smtp_username, self.smtp_password)

            # Send email
            server.sendmail(self.from_email, recipients, msg.as_string())
            server.quit()

            logger.info(f"Email sent successfully to {recipients}")

            return {
                "success": True,
                "recipients": recipients,
                "provider": "email",
            }

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication failed: {e}")
            raise Exception(f"Email authentication failed: {e}")
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error: {e}")
            raise Exception(f"Email error: {e}")
        except Exception as e:
            logger.error(f"Email unexpected error: {e}")
            raise

    def send_change_alert(
        self,
        to_email: str | List[str],
        target_url: str,
        content_before: Optional[str],
        content_after: Optional[str],
        num_agents: int,
        quorum: int,
    ) -> Dict[str, Any]:
        """
        Send a page change alert via email.

        Args:
            to_email: Recipient email address(es)
            target_url: URL that changed
            content_before: Content before change
            content_after: Content after change
            num_agents: Number of agents confirming the change
            quorum: Required quorum

        Returns:
            Response dict from send_email
        """
        subject = f"Writ Alert: Change detected on {target_url}"

        # Plain text body
        body = f"""Writ has detected a change on one of your monitored pages.

URL: {target_url}
Confirmed by: {num_agents}/{quorum} agents

"""
        if content_before and content_after:
            body += f"""BEFORE:
{content_before}

AFTER:
{content_after}
"""
        else:
            body += "View the dashboard for details."

        body += "\n---\nThis is an automated notification from Writ"

        # HTML body
        html_body = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .header {{ background-color: #4F46E5; color: white; padding: 20px; text-align: center; }}
                .content {{ padding: 20px; }}
                .alert-box {{ background-color: #FEF3C7; border-left: 4px solid #F59E0B; padding: 15px; margin: 20px 0; }}
                .comparison {{ display: table; width: 100%; margin: 20px 0; }}
                .before, .after {{ display: table-cell; width: 50%; padding: 10px; vertical-align: top; }}
                .before {{ background-color: #FEE2E2; border: 1px solid #EF4444; }}
                .after {{ background-color: #D1FAE5; border: 1px solid: #10B981; }}
                .label {{ font-weight: bold; margin-bottom: 5px; }}
                pre {{ white-space: pre-wrap; word-wrap: break-word; font-size: 12px; }}
                .footer {{ background-color: #F3F4F6; padding: 15px; text-align: center; font-size: 12px; color: #6B7280; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>🔔 Writ Alert</h1>
            </div>
            <div class="content">
                <div class="alert-box">
                    <strong>Change Detected!</strong><br>
                    A change was detected on one of your monitored pages.
                </div>
                <p><strong>URL:</strong> <a href="{_esc(str(target_url))}">{_esc(str(target_url))}</a></p>
                <p><strong>Confirmed by:</strong> {num_agents}/{quorum} agents</p>
        """

        if content_before and content_after:
            # Escaped for the same reason as the Pushover path in
            # notifications/dispatcher.py: this is text scraped from the monitored
            # page, so it is attacker-influenced, and it is being interpolated
            # into an HTML email body. <pre> does not neutralise markup inside it.
            html_body += f"""
                <div class="comparison">
                    <div class="before">
                        <div class="label" style="color: #DC2626;">Before</div>
                        <pre>{_esc(str(content_before))}</pre>
                    </div>
                    <div class="after">
                        <div class="label" style="color: #059669;">After</div>
                        <pre>{_esc(str(content_after))}</pre>
                    </div>
                </div>
            """

        html_body += """
            </div>
            <div class="footer">
                This is an automated notification from Writ
            </div>
        </body>
        </html>
        """

        return self.send_email(to_email, subject, body, html_body)

    def send_test_email(self, to_email: str) -> Dict[str, Any]:
        """
        Send a test email to verify configuration.

        Args:
            to_email: Test recipient email address

        Returns:
            Response dict from send_email
        """
        subject = "Writ Test Email"
        body = "This is a test email from Writ to verify your email notification configuration."
        html_body = """
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2>✅ Test Email Successful!</h2>
            <p>This is a test email from Writ.</p>
            <p>Your email notification configuration is working correctly.</p>
        </body>
        </html>
        """
        return self.send_email(to_email, subject, body, html_body)
