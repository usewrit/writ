"""
Database models for the Writ self-hosted coordinator.

The coordinator is single-user, so only the owner-scoped models are defined here.
"""
from .agent import Agent, AgentStatus, Platform
from .target import Target
from .detected_change import DetectedChange
from .config import Config
from .api_key import APIKey, Role
from .pushover_recipient import PushoverRecipient
from .pushover_config import PushoverConfig
from .target_notification import TargetNotification
from .target_assignment import TargetAssignment
from .uptime_check import UptimeCheck
from .automation_workflow import AutomationWorkflow
from .automation_task import AutomationTask
from .target_selector import TargetSelector
from .selector_extractor import SelectorExtractor
from .trigger_rule import TriggerRule, TriggerExecution
from .webhook_config import WebhookConfig
from .webhook_recipient import WebhookRecipient
from .webhook_trigger import WebhookTrigger
from .signal_group import SignalGroup
from .scrape_job import ScrapeJob
from .scrape_result import ScrapeResult
from .user import User
from .email_config import EmailConfig
from .email_recipient import EmailRecipient
from .twilio_config import TwilioConfig
from .twilio_recipient import TwilioRecipient
from .whatsapp_config import WhatsAppConfig
from .whatsapp_recipient import WhatsAppRecipient
from .signal_config import SignalConfig
from .signal_recipient import SignalRecipient
from .notification_settings import NotificationSettings
from .notification_log import NotificationLog
from .notification import Notification
from .user_notification_preference import UserNotificationPreference
from .oauth import OAuthApplication, OAuthAuthorizationCode, OAuthAccessToken
from .ai_provider_config import AIProviderConfig
from .vault_secret import VaultSecret
from .streaming_session import StreamingSession
from .mcp_endpoint import McpEndpoint
from .persona import Persona
from .mail_connection import MailConnection
from .stored_file import StoredFile
from .local_workflow import LocalWorkflow
from .ai_session import AiSession
from .crawl_job import CrawlJob
from .crawl_definition import CrawlDefinition


__all__ = [
    "Agent",
    "AgentStatus",
    "Platform",
    "Target",
    "DetectedChange",
    "Config",
    "APIKey",
    "Role",
    "PushoverRecipient",
    "PushoverConfig",
    "TargetNotification",
    "TargetAssignment",
    "UptimeCheck",
    "AutomationWorkflow",
    "AutomationTask",
    "TargetSelector",
    "SelectorExtractor",
    "TriggerRule",
    "TriggerExecution",
    "WebhookConfig",
    "WebhookRecipient",
    "WebhookTrigger",
    "SignalGroup",
    "ScrapeJob",
    "ScrapeResult",
    "User",
    "EmailConfig",
    "EmailRecipient",
    "TwilioConfig",
    "TwilioRecipient",
    "WhatsAppConfig",
    "WhatsAppRecipient",
    "SignalConfig",
    "SignalRecipient",
    "NotificationSettings",
    "NotificationLog",
    "Notification",
    "UserNotificationPreference",
    "OAuthApplication",
    "OAuthAuthorizationCode",
    "OAuthAccessToken",
    "AIProviderConfig",
    "VaultSecret",
    "StreamingSession",
    "McpEndpoint",
    "Persona",
    "MailConnection",
    "StoredFile",
    "LocalWorkflow",
    "AiSession",
    "CrawlJob",
    "CrawlDefinition",
]
