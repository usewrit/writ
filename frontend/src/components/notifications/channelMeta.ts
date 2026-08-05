import React from 'react';
import {
  BellIcon,
  DevicePhoneMobileIcon,
  EnvelopeIcon,
  ChatBubbleLeftIcon,
  ChatBubbleLeftRightIcon,
  ChatBubbleOvalLeftEllipsisIcon,
  LockClosedIcon,
  LinkIcon,
  HashtagIcon,
  PaperAirplaneIcon,
} from '@heroicons/react/24/outline';

// Notification-CHANNEL (delivery provider) → monochrome icon + English label.
// Sibling of `notificationMeta.ts`, which maps in-app notification TYPES; this
// one maps the outbound providers (`recipient.provider`, trigger channel keys).
//
// One map for every surface that shows a channel — the triggers modal, the
// channel list, the check detail page — so a provider added here shows up with
// the same glyph everywhere instead of drifting per-screen. The app never uses
// emoji in UI, so these are heroicons, not pictographs.
//
// Labels are English source strings; render them through `t(...)` at the call
// site (they exist as i18n keys already).

export interface ChannelMeta {
  Icon: React.ElementType;
  label: string;
}

const CHANNELS: Record<string, ChannelMeta> = {
  pushover: { Icon: DevicePhoneMobileIcon, label: 'Pushover' },
  email: { Icon: EnvelopeIcon, label: 'Email' },
  twilio: { Icon: ChatBubbleLeftIcon, label: 'SMS' },
  whatsapp: { Icon: ChatBubbleOvalLeftEllipsisIcon, label: 'WhatsApp' },
  signal: { Icon: LockClosedIcon, label: 'Signal' },
  webhook: { Icon: LinkIcon, label: 'Webhook' },
  slack: { Icon: HashtagIcon, label: 'Slack' },
  discord: { Icon: ChatBubbleLeftRightIcon, label: 'Discord' },
  telegram: { Icon: PaperAirplaneIcon, label: 'Telegram' },
};

/** Meta for a channel key. Unknown providers fall back to a bell + the raw key,
 *  so a channel the backend adds before the UI knows about it still renders. */
export function channelMeta(channel: string): ChannelMeta {
  return CHANNELS[channel] ?? { Icon: BellIcon, label: channel };
}

/** Icon component only — for the common `const Icon = iconForChannel(k)` shape. */
export function iconForChannel(channel: string): React.ElementType {
  return channelMeta(channel).Icon;
}

export { CHANNELS as CHANNEL_META };
