import {
  ShoppingCartIcon,
  BellAlertIcon,
  CalendarDaysIcon,
  MegaphoneIcon,
} from '@heroicons/react/24/outline';

/**
 * Template registry.
 *
 * A template is a reusable, parameterised recipe that the guided TemplateWizard
 * turns into real resources (target + selectors + extractors + an automation, or
 * an AI workflow) using the existing public APIs — no special backend.
 *
 * Templates supersede the older one-off `AUTOMATION_PRESETS` (presets.ts): the
 * restock preset is now `restock_autobuy` here and is driven by the generic
 * wizard instead of a bespoke component.
 *
 * The same registry powers the creation-wizard template strip and the marketplace story:
 * each entry is data, so new templates (and, later, community-published ones)
 * are just more rows.
 */

export type TemplateKind =
  | 'monitor_buy' // watch → in-stock/condition → checkout workflow → notify (auto-buy)
  | 'monitor_alert' // watch → condition → optional "grab" workflow → notify
  | 'monitor_extract' // watch a page → extract structured fields → notify on change
  // 'ai_action' (autonomous AI goal → AI session) is retained in the type only so
  // the guided-wizard branches compile; no template uses this kind in this build.
  | 'ai_action';

export type TemplateEdition = 'cloud' | 'byo';
export type RiskTier = 'clean' | 'tos' | 'adversarial';
export type TemplateCategory = 'acquire' | 'intel' | 'personal';

/**
 * Payment handling for auto-buy. We NEVER store a raw card number (PAN): hosted
 * checkout iframes can't be filled with one anyway, and holding a PAN would put
 * the whole platform in PCI scope. Instead we orchestrate payment that already
 * exists. See AUTOBUY_AND_TEMPLATES_DESIGN.md.
 */
export type PaymentMode =
  | 'merchant_saved' // select a card already saved on the merchant account, then click buy
  | 'browser_autofill'; // BYO local agent: the real Chrome profile autofills the card

export interface TemplateSelectorHint {
  name: string;
  selector: string;
  /** Attach a numeric price extractor to this selector (lands in extracted.price). */
  isPrice?: boolean;
}

export interface TemplateSpec {
  id: string;
  title: string;
  tagline: string; // one short line for the gallery card
  description: string; // fuller explanation shown in the wizard intro
  icon: any;
  category: TemplateCategory;
  kind: TemplateKind;
  riskTier: RiskTier;
  /** Editions this template is allowed to run in (gates adversarial recipes to BYO). */
  editions: TemplateEdition[];
  recommendedEdition: TemplateEdition;

  // ── monitor_* shared ──
  urlPlaceholder?: string;
  checkPeriodMs?: number;
  requiresPlaywright?: boolean;
  selectorHints?: TemplateSelectorHint[];
  pricePattern?: string;

  // ── monitor_buy / monitor_alert ──
  /** Default substring that means "go" (e.g. "In Stock", "available"). */
  conditionText?: string;
  conditionLabel?: string;
  /** Whether the user picks/records an action workflow (false = alert only). */
  actionWorkflow?: boolean;
  notifyTitle?: string;
  notifyTemplate?: string;

  // ── monitor_buy only ──
  payment?: PaymentMode[];
  /** New auto-buy automations start in dry-run (stop before placing the order). */
  defaultDryRun?: boolean;

  // ── monitor_extract only ──
  extractHint?: string;

  // ── ai_action only ──
  aiGoal?: string;
  aiUserContext?: string;
  aiMode?: 'standard' | 'intelligent';
  /** Most personal-ops act behind a login → suggest attaching a persona. */
  suggestPersona?: boolean;
  /**
   * Insert a dedicated card-input step: link an existing Vault card, save a new
   * card to the Vault, or let the local agent's browser autofill it. The card is
   * always encrypted in the Vault (or never collected) — never stored on the
   * workflow. The AI receives card fields as secure {{vault:…}} references.
   */
  collectCard?: boolean;
}

export const TEMPLATE_SPECS: TemplateSpec[] = [
  // ─────────────────────────── ACQUIRE ───────────────────────────
  {
    id: 'restock_autobuy',
    title: 'Restock checkout',
    tagline: "Run your checkout the moment it's back in stock",
    description: "Watch a product page; when it's back in stock under your price ceiling, run your saved checkout workflow — paying with a card already on file. Fires once by default and starts in dry-run so you can review every step before any order is placed.",
    icon: ShoppingCartIcon,
    category: 'acquire',
    kind: 'monitor_buy',
    riskTier: 'adversarial',
    editions: ['byo'],
    recommendedEdition: 'byo',
    urlPlaceholder: 'https://store.com/product/123',
    checkPeriodMs: 120000,
    requiresPlaywright: true,
    selectorHints: [
      {
        name: 'Buy box',
        selector: '.buy-box, [itemprop="offers"], #add-to-cart, .product-info, .product-form, main',
        isPrice: true,
      },
    ],
    pricePattern: '([0-9][0-9.,]*)',
    conditionText: 'In Stock',
    conditionLabel: 'Run checkout when availability text contains',
    actionWorkflow: true,
    notifyTitle: 'Restock purchase',
    notifyTemplate: '✅ Checkout workflow ran for {{target_name}} — back in stock at {{now_time}}.',
    payment: ['merchant_saved', 'browser_autofill'],
    defaultDryRun: true,
  },
  {
    id: 'restock_alert',
    title: 'Restock alert',
    tagline: 'Get pinged the second it restocks',
    description: 'Watch an out-of-stock item and get notified the instant it becomes available. No purchase, no payment — just a fast alert.',
    icon: BellAlertIcon,
    category: 'acquire',
    kind: 'monitor_alert',
    riskTier: 'clean',
    editions: ['cloud', 'byo'],
    recommendedEdition: 'cloud',
    urlPlaceholder: 'https://store.com/product/123',
    checkPeriodMs: 120000,
    requiresPlaywright: true,
    selectorHints: [
      { name: 'Availability', selector: '.availability, .stock-status, [data-availability], .buy-box, main' },
    ],
    conditionText: 'In Stock',
    conditionLabel: 'Alert when availability text contains',
    actionWorkflow: false,
    notifyTitle: 'Back in stock',
    notifyTemplate: '🔔 {{target_name}} is back in stock — {{now_time}}.',
  },
  {
    id: 'reservation_sniper',
    title: 'Reservation watch',
    tagline: 'Know the instant a slot opens',
    description: 'Watch a reservation or appointment page (Resy, OpenTable, Recreation.gov, a DMV/visa portal, a tee-time site) and the moment a slot opens, get alerted — or trigger your booking workflow. Great for hard-to-get tables, campsites and appointments.',
    icon: CalendarDaysIcon,
    category: 'acquire',
    kind: 'monitor_alert',
    riskTier: 'tos',
    editions: ['cloud', 'byo'],
    recommendedEdition: 'byo',
    urlPlaceholder: 'https://resy.com/cities/... or https://www.recreation.gov/...',
    checkPeriodMs: 60000,
    requiresPlaywright: true,
    selectorHints: [
      { name: 'Availability', selector: '.availability, .ReservationButton, [data-available], .rec-availability, main' },
    ],
    conditionText: 'available',
    conditionLabel: 'Alert when availability text contains',
    actionWorkflow: true,
    notifyTitle: 'Slot available',
    notifyTemplate: '📅 A slot opened on {{target_name}} — {{now_time}}.',
  },

  // ─────────────────────────── INTEL ───────────────────────────
  {
    id: 'ad_tracer',
    title: 'Ad tracer',
    tagline: "Track a competitor's live ads",
    description: "Watch a competitor's ad-library page (Meta Ad Library, TikTok Creative Center, Google Ads Transparency) and get notified when new creatives appear, capturing the ad copy so you can track their messaging and offers over time.",
    icon: MegaphoneIcon,
    category: 'intel',
    kind: 'monitor_extract',
    riskTier: 'tos',
    editions: ['cloud', 'byo'],
    recommendedEdition: 'cloud',
    urlPlaceholder: 'https://www.facebook.com/ads/library/?...  (a competitor search URL)',
    checkPeriodMs: 21600000, // 6h — ad libraries don't change minute-to-minute
    requiresPlaywright: true,
    selectorHints: [
      { name: 'Ad creatives', selector: '[role="article"], .ad-card, ._7jvw, .creative-card, main' },
    ],
    extractHint: 'The container that holds each ad card. We snapshot its text so new ads surface as a change.',
    notifyTitle: 'New competitor ad',
    notifyTemplate: '📣 New ad activity on {{target_name}} — {{now_time}}.',
  },

];

export function getTemplate(id: string): TemplateSpec | undefined {
  return TEMPLATE_SPECS.find((t) => t.id === id);
}

export const TEMPLATE_CATEGORY_LABELS: Record<TemplateCategory, string> = {
  acquire: 'Buy & acquire',
  intel: 'Market intelligence',
  personal: 'Personal automations',
};

export const RISK_LABELS: Record<RiskTier, { label: string; hint: string }> = {
  clean: { label: 'Standard', hint: 'Runs anywhere, including the managed cloud.' },
  tos: {
    label: 'Use responsibly',
    hint: "Acts on sites under your own login. Review each site's terms; best run on your own linked agent.",
  },
  adversarial: {
    label: 'BYO only',
    hint: 'High-demand pages are unreliable to watch from the shared cloud. Runs only on your own linked machine and starts in dry-run.',
  },
};

export const PAYMENT_LABELS: Record<PaymentMode, { label: string; hint: string; comingSoon?: boolean }> = {
  merchant_saved: {
    label: 'Card already saved on the store',
    hint: "We never see your card. The checkout step selects the card you've already saved on the merchant and clicks buy.",
  },
  browser_autofill: {
    label: "Your browser's saved card (local agent)",
    hint: 'Runs on your linked machine so your real Chrome profile autofills payment. The card never leaves your computer.',
  },
};
