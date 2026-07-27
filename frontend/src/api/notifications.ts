import client from './client';

export interface NotificationProvider {
  name: string;
  configured: boolean;
  enabled: boolean;
}

export interface AllProvidersResponse {
  providers: {
    pushover: NotificationProvider;
    email: NotificationProvider;
    twilio: NotificationProvider;
    whatsapp: NotificationProvider;
    signal: NotificationProvider;
  };
}

export interface EmailConfig {
  configured: boolean;
  enabled: boolean;
  smtp_host?: string;
  smtp_port?: number;
  smtp_username?: string;
  from_email?: string;
  from_name?: string;
  use_tls?: boolean;
}

// Get all providers status
export const getAllProviders = async (): Promise<AllProvidersResponse> => {
  const response = await client.get('/notifications/providers/all');
  return response.data;
};

// Platform notification preferences (owner event × channel matrix).
export interface PreferenceChannelSpec {
  default: boolean;
  locked: boolean;
}

export interface PreferenceEventSpec {
  key: string;
  category: string;
  label: string;
  description: string;
  channels: Record<string, PreferenceChannelSpec>;
}

export interface NotificationPreferences {
  catalog: {
    categories: Record<string, string>;
    channels: string[];
    events: PreferenceEventSpec[];
  };
  // Effective (defaults merged with stored overrides): event → channel → bool
  effective: Record<string, Record<string, boolean>>;
  // Raw stored overrides only (what the owner explicitly changed)
  overrides: Record<string, Record<string, boolean>>;
  channel_availability: Record<string, boolean>;
  phone_number: string | null;
  pushover_user_key_set: boolean;
}

export interface NotificationPreferencesUpdate {
  // Partial: only supplied events are touched; an event's dict REPLACES its
  // stored overrides (send {} to reset that event to catalog defaults).
  preferences?: Record<string, Record<string, boolean>>;
  phone_number?: string; // empty string clears
  pushover_user_key?: string; // empty string clears
}

export const getNotificationPreferences = async (): Promise<NotificationPreferences> => {
  const response = await client.get('/notifications/preferences');
  return response.data;
};

export const updateNotificationPreferences = async (
  update: NotificationPreferencesUpdate,
): Promise<NotificationPreferences> => {
  const response = await client.put('/notifications/preferences', update);
  return response.data;
};

// Pushover endpoints
export interface PushoverConfig {
  configured: boolean;
  enabled: boolean;
  notification_title?: string | null;
  notification_priority?: number | null;
  notification_sound?: string | null;
  html_enabled?: boolean | null;
}

export const getPushoverConfig = async (): Promise<PushoverConfig> => {
  const response = await client.get('/notifications/pushover/config');
  return response.data;
};

export const updatePushoverConfig = async (config: {
  app_token: string;
  user_key: string;
  html_enabled?: boolean;
}) => {
  const response = await client.post('/notifications/pushover/config', config);
  return response.data;
};

// Global notification behavior (quiet hours, rate limiting, batching, health thresholds).
export interface NotificationSettings {
  quiet_hours_enabled: boolean;
  quiet_hours_start: string | null;
  quiet_hours_end: string | null;
  rate_limiting_enabled: boolean;
  rate_limit_max: number;
  rate_limit_period: string;
  batch_enabled: boolean;
  batch_window_minutes: number;
  agent_errors_enabled: boolean;
  agent_error_threshold: number;
  agent_error_delay_minutes: number;
  target_health_enabled: boolean;
  target_health_threshold: number;
  target_health_check_interval: number;
  system_health_enabled: boolean;
  system_health_agent_disconnect_delay: number;
}

export const getNotificationSettings = async (): Promise<NotificationSettings> => {
  const response = await client.get('/notifications/settings');
  return response.data;
};

export const updateNotificationSettings = async (
  settings: NotificationSettings,
): Promise<NotificationSettings> => {
  const response = await client.put('/notifications/settings', settings);
  return response.data;
};

// Email endpoints
export const getEmailConfig = async (): Promise<EmailConfig> => {
  const response = await client.get('/notifications/email/config');
  return response.data;
};

export const updateEmailConfig = async (config: {
  smtp_host: string;
  smtp_port: number;
  smtp_username: string;
  smtp_password: string;
  from_email: string;
  from_name: string;
  use_tls: boolean;
  enabled: boolean;
}) => {
  const response = await client.post('/notifications/email/config', config);
  return response.data;
};

export const sendTestEmail = async (to_email: string) => {
  const response = await client.post('/notifications/email/test', { to_email });
  return response.data;
};

// Twilio SMS endpoints
export interface TwilioConfig {
  configured: boolean;
  enabled: boolean;
  account_sid?: string;
  from_phone?: string;
}

export interface TwilioRecipient {
  id: number;
  name: string;
  phone_number: string;
  enabled: boolean;
  created_at: string;
  last_notified_at: string | null;
}

export const getTwilioConfig = async (): Promise<TwilioConfig> => {
  const response = await client.get('/notifications/twilio/config');
  return response.data;
};

export const updateTwilioConfig = async (config: {
  account_sid: string;
  auth_token: string;
  from_phone: string;
  enabled: boolean;
}) => {
  const response = await client.post('/notifications/twilio/config', config);
  return response.data;
};

export const sendTestTwilioSMS = async (to_phone: string) => {
  const response = await client.post('/notifications/twilio/test', { to_phone });
  return response.data;
};

export const getTwilioRecipients = async (): Promise<TwilioRecipient[]> => {
  const response = await client.get('/notifications/twilio/recipients');
  return response.data;
};

export const addTwilioRecipient = async (recipient: { name: string; phone_number: string }) => {
  const response = await client.post('/notifications/twilio/recipients', recipient);
  return response.data;
};

export const deleteTwilioRecipient = async (recipientId: number) => {
  const response = await client.delete(`/notifications/twilio/recipients/${recipientId}`);
  return response.data;
};

export const toggleTwilioRecipient = async (recipientId: number) => {
  const response = await client.patch(`/notifications/twilio/recipients/${recipientId}/toggle`);
  return response.data;
};

// WhatsApp endpoints
export interface WhatsAppConfig {
  configured: boolean;
  enabled: boolean;
  account_sid?: string;
  from_number?: string;
}

export interface WhatsAppRecipient {
  id: number;
  name: string;
  phone_number: string;
  enabled: boolean;
  created_at: string;
  last_notified_at: string | null;
}

export const getWhatsAppConfig = async (): Promise<WhatsAppConfig> => {
  const response = await client.get('/notifications/whatsapp/config');
  return response.data;
};

export const updateWhatsAppConfig = async (config: {
  account_sid: string;
  auth_token: string;
  from_number: string;
  enabled: boolean;
}) => {
  const response = await client.post('/notifications/whatsapp/config', config);
  return response.data;
};

export const sendTestWhatsAppMessage = async (to_number: string) => {
  const response = await client.post('/notifications/whatsapp/test', { to_number });
  return response.data;
};

export const getWhatsAppRecipients = async (): Promise<WhatsAppRecipient[]> => {
  const response = await client.get('/notifications/whatsapp/recipients');
  return response.data;
};

export const addWhatsAppRecipient = async (recipient: { name: string; phone_number: string }) => {
  const response = await client.post('/notifications/whatsapp/recipients', recipient);
  return response.data;
};

export const deleteWhatsAppRecipient = async (recipientId: number) => {
  const response = await client.delete(`/notifications/whatsapp/recipients/${recipientId}`);
  return response.data;
};

export const toggleWhatsAppRecipient = async (recipientId: number) => {
  const response = await client.patch(`/notifications/whatsapp/recipients/${recipientId}/toggle`);
  return response.data;
};

// Signal endpoints
export interface SignalConfig {
  configured: boolean;
  enabled: boolean;
  api_url?: string;
  sender_number?: string;
}

export interface SignalRecipient {
  id: number;
  name: string;
  phone_number: string;
  enabled: boolean;
  created_at: string;
  last_notified_at: string | null;
}

export const getSignalConfig = async (): Promise<SignalConfig> => {
  const response = await client.get('/notifications/signal/config');
  return response.data;
};

export const updateSignalConfig = async (config: {
  api_url: string;
  sender_number: string;
  enabled: boolean;
}) => {
  const response = await client.post('/notifications/signal/config', config);
  return response.data;
};

export const sendTestSignalMessage = async (to_number: string) => {
  const response = await client.post('/notifications/signal/test', { to_number });
  return response.data;
};

export const getSignalRecipients = async (): Promise<SignalRecipient[]> => {
  const response = await client.get('/notifications/signal/recipients');
  return response.data;
};

export const addSignalRecipient = async (recipient: { name: string; phone_number: string }) => {
  const response = await client.post('/notifications/signal/recipients', recipient);
  return response.data;
};

export const deleteSignalRecipient = async (recipientId: number) => {
  const response = await client.delete(`/notifications/signal/recipients/${recipientId}`);
  return response.data;
};

export const toggleSignalRecipient = async (recipientId: number) => {
  const response = await client.patch(`/notifications/signal/recipients/${recipientId}/toggle`);
  return response.data;
};
