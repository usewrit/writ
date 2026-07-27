import { ShoppingCartIcon } from '@heroicons/react/24/outline';
import { FlowBlock } from './types';

/**
 * Prebuilt automation presets.
 *
 * A preset scaffolds a fully-wired block chain into the FlowBuilder. The user
 * only fills in the parts that are specific to them (which product page, which
 * checkout workflow). Guardrails (max_fires / cooldown_minutes) live on the
 * root event block's config and are persisted into the trigger's `conditions`
 * on save (see FlowBuilder.handleSave).
 */
export interface AutomationPreset {
  id: string;
  title: string;
  description: string;
  icon: any;
  build: () => { name: string; description: string; blocks: FlowBlock[] };
}

let _seq = 0;
function bid(tag: string): string {
  _seq += 1;
  return `block_${tag}_${Date.now()}_${_seq}`;
}

function buildEcommerceRestock(): { name: string; description: string; blocks: FlowBlock[] } {
  const evt = bid('event');
  const condStock = bid('cond');
  const condPrice = bid('cond');
  const buy = bid('action');
  const bought = bid('event');
  const notify = bid('action');

  const blocks: FlowBlock[] = [
    {
      // Watch the product page. The user supplies the URL + selectors in the
      // Content Monitor panel; selectors below are sensible starting points.
      id: evt,
      type: 'event',
      blockType: 'change_detected',
      config: {
        url: '',
        check_period_ms: 120000,
        // One watched buy-box selector so stock text AND the numeric price arrive
        // in the same change event (extracted data is per-selector, not merged).
        wizardSelectors: [
          {
            name: 'Buy box',
            selector: '.buy-box, [itemprop="offers"], #add-to-cart, .product-info, .product-form, main',
            // Pull the first number out of the text -> lands in extracted.price,
            // which the price-ceiling condition compares against.
            extractors: [
              {
                name: 'Price',
                outputName: 'price',
                extractType: 'regex',
                config: { pattern: '([0-9][0-9.,]*)', group: 1 },
              },
            ],
          },
        ],
        // Guardrails — persisted to trigger.conditions on save.
        // Buy once, then stop; ignore repeat fires within 24h as a safety net.
        max_fires: 1,
        cooldown_minutes: 1440,
      },
    },
    {
      // Only proceed when the page comes back IN STOCK.
      id: condStock,
      type: 'condition',
      blockType: 'condition',
      parentId: evt,
      config: { field: 'content', operator: 'contains', value: 'In Stock' },
    },
    {
      // Price ceiling. Defaults to "no real ceiling" — lower it to your max.
      // Requires a numeric `price` extractor on the Availability/Price selector.
      id: condPrice,
      type: 'condition',
      blockType: 'condition',
      parentId: condStock,
      config: { field: 'extracted.price', operator: 'lte', value: '99999', _isCustom: true },
    },
    {
      // Run YOUR checkout workflow (add-to-cart → checkout). Pick or record it.
      id: buy,
      type: 'action',
      blockType: 'workflow',
      parentId: condPrice,
      config: { workflow_id: null, input_mapping: {} },
    },
    {
      // Wait for the checkout workflow to finish successfully.
      id: bought,
      type: 'event',
      blockType: 'workflow_completed',
      parentId: buy,
      config: { status_condition: 'success', linked_to_block: buy },
    },
    {
      // Tell the user it bought.
      id: notify,
      type: 'action',
      blockType: 'notification',
      parentId: bought,
      config: {
        channels: ['email'],
        title: 'Restock purchase',
        template: '✅ Checkout workflow ran for {{target_name}} — back in stock at {{now_time}}.',
      },
    },
  ];

  return {
    name: 'Ecommerce restock checkout',
    description:
      "Watch a product page; when it's back in stock under your price ceiling, run your checkout workflow and notify you. Fires once by default.",
    blocks,
  };
}

export const AUTOMATION_PRESETS: AutomationPreset[] = [
  {
    id: 'ecommerce_restock',
    title: 'Restock checkout',
    description: "Run your checkout the moment it's back in stock",
    icon: ShoppingCartIcon,
    build: buildEcommerceRestock,
  },
];

export function getPreset(id: string): AutomationPreset | undefined {
  return AUTOMATION_PRESETS.find((p) => p.id === id);
}
