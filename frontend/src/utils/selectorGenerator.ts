/**
 * Advanced CSS Selector Generator
 * Generates highly precise, stable CSS selectors for complex DOM elements
 */

interface SelectorOptions {
  maxCombinedLength?: number;
  maxDepth?: number;
}

const defaultOptions: SelectorOptions = {
  maxCombinedLength: 300,
  maxDepth: 15,
};

/**
 * Check if a class name looks stable (not generated/random)
 */
function isStableClass(className: string): boolean {
  const unstablePatterns = [
    /^[a-f0-9]{6,}$/i,        // Hash-like: abc123def
    /random|temp|generated/i,  // Contains "random", "temp", etc.
    /^_[a-zA-Z0-9_]+$/,        // Starts with underscore (often generated)
    /^[A-Z][a-z]+[A-Z0-9]/,    // CamelCase with numbers (styled-components)
    /^css-/i,                  // CSS-in-JS generated classes
  ];

  return !unstablePatterns.some(pattern => pattern.test(className));
}

/**
 * Check if an ID looks stable
 */
function isStableId(id: string): boolean {
  const unstablePatterns = [
    /random|temp|generated|uuid|guid/i,
    /^[a-f0-9-]{20,}$/i,  // UUID-like
    /^react-/i,            // React-generated IDs
    /^[0-9]+$/,            // Pure numbers
  ];

  return Boolean(id && !unstablePatterns.some(pattern => pattern.test(id)));
}

/**
 * Get stable classes from an element
 */
function getStableClasses(element: Element): string[] {
  const classes = Array.from(element.classList);
  return classes.filter(cls => {
    // Filter out our internal highlight classes
    if (cls.startsWith('ps-')) return false;
    // Filter out unstable patterns
    return isStableClass(cls);
  });
}

/**
 * Get useful data attributes from an element
 */
function getDataAttributes(element: Element): Record<string, string> {
  const attrs: Record<string, string> = {};

  // Prioritize certain data attributes
  const priorityAttrs = [
    'data-testid',
    'data-test',
    'data-id',
    'data-name',
    'data-component',
    'data-type',
  ];

  for (const attr of priorityAttrs) {
    const value = element.getAttribute(attr);
    if (value) {
      attrs[attr] = value;
    }
  }

  // Get other data attributes
  for (const attr of element.getAttributeNames()) {
    if (attr.startsWith('data-') && !attrs[attr]) {
      const value = element.getAttribute(attr);
      if (value && !value.match(/^[a-f0-9-]{20,}$/i)) {
        attrs[attr] = value;
      }
    }
  }

  return attrs;
}

/**
 * Get semantic attributes from an element
 */
function getSemanticAttributes(element: Element): Record<string, string> {
  const attrs: Record<string, string> = {};

  const semanticAttrs = [
    'role',
    'aria-label',
    'aria-labelledby',
    'name',
    'type',
    'placeholder',
    'title',
    'alt',
    'href',
    'rel',
  ];

  for (const attr of semanticAttrs) {
    const value = element.getAttribute(attr);
    if (value) {
      attrs[attr] = value;
    }
  }

  return attrs;
}

/**
 * Test if a selector uniquely identifies the element
 */
function isUniqueSelector(element: Element, selector: string): boolean {
  try {
    const doc = element.ownerDocument;
    const matches = doc.querySelectorAll(selector);
    return matches.length === 1 && matches[0] === element;
  } catch {
    return false;
  }
}

/**
 * Generate selector using ID
 */
function generateIdSelector(element: Element): string | null {
  const id = element.id;
  if (id && isStableId(id)) {
    return `#${CSS.escape(id)}`;
  }
  return null;
}

/**
 * Generate selector using data attributes
 */
function generateDataAttributeSelector(element: Element): string[] {
  const selectors: string[] = [];
  const dataAttrs = getDataAttributes(element);
  const tag = element.tagName.toLowerCase();

  for (const [attr, value] of Object.entries(dataAttrs)) {
    const escaped = CSS.escape(value);
    selectors.push(`${tag}[${attr}="${escaped}"]`);
    selectors.push(`[${attr}="${escaped}"]`);
  }

  return selectors;
}

/**
 * Generate selector using semantic attributes
 */
function generateSemanticSelector(element: Element): string[] {
  const selectors: string[] = [];
  const semanticAttrs = getSemanticAttributes(element);
  const tag = element.tagName.toLowerCase();

  for (const [attr, value] of Object.entries(semanticAttrs)) {
    // Skip very long values (likely not stable)
    if (value.length > 50) continue;

    const escaped = CSS.escape(value);
    selectors.push(`${tag}[${attr}="${escaped}"]`);
  }

  return selectors;
}

/**
 * Generate selector using classes
 */
function generateClassSelector(element: Element): string[] {
  const selectors: string[] = [];
  const classes = getStableClasses(element);
  const tag = element.tagName.toLowerCase();

  if (classes.length === 0) return selectors;

  // Try all classes
  const allClasses = classes.map(c => `.${CSS.escape(c)}`).join('');
  selectors.push(`${tag}${allClasses}`);

  // Try combinations of classes
  if (classes.length >= 2) {
    for (let i = 0; i < Math.min(classes.length, 3); i++) {
      for (let j = i + 1; j < Math.min(classes.length, 4); j++) {
        const combo = `.${CSS.escape(classes[i])}.${CSS.escape(classes[j])}`;
        selectors.push(`${tag}${combo}`);
      }
    }
  }

  // Try single most specific class
  if (classes.length > 0) {
    const mostSpecific = classes[0];
    selectors.push(`${tag}.${CSS.escape(mostSpecific)}`);
  }

  return selectors;
}

/**
 * Generate nth-of-type selector
 */
function generateNthOfTypeSelector(element: Element): string | null {
  const parent = element.parentElement;
  if (!parent) return null;

  const tag = element.tagName.toLowerCase();
  const siblings = Array.from(parent.children).filter(
    child => child.tagName.toLowerCase() === tag
  );

  const index = siblings.indexOf(element);
  if (index === -1) return null;

  return `${tag}:nth-of-type(${index + 1})`;
}

/**
 * Generate nth-child selector
 */
function generateNthChildSelector(element: Element): string | null {
  const parent = element.parentElement;
  if (!parent) return null;

  const siblings = Array.from(parent.children);
  const index = siblings.indexOf(element);
  if (index === -1) return null;

  const tag = element.tagName.toLowerCase();
  return `${tag}:nth-child(${index + 1})`;
}

/**
 * Generate selector with text content (for unique text)
 * NOTE: Currently unused internally but exported for potential external use
 */
export function generateTextContentSelector(element: Element): string[] {
  const selectors: string[] = [];
  const text = element.textContent?.trim();

  if (!text || text.length > 50 || text.length < 3) return selectors;

  const tag = element.tagName.toLowerCase();

  // TODO: Implement xpath/document uniqueness check
  // const doc = element.ownerDocument;
  // const xpath = `//${tag}[contains(text(), "${text.substring(0, 20)}")]`;

  try {
    // Try using :contains pseudo-selector (CSS4, may not work everywhere)
    const containsSelector = `${tag}:has-text("${text.substring(0, 30)}")`;
    selectors.push(containsSelector);
  } catch {
    // Ignore if not supported
  }

  return selectors;
}

/**
 * Build a path selector by traversing up the DOM tree
 */
function buildPathSelector(element: Element, maxDepth: number = 10): string[] {
  const paths: string[] = [];
  let current: Element | null = element;
  const parts: string[] = [];
  let depth = 0;

  while (current && depth < maxDepth) {
    // Try ID at this level - if found, prepend and stop
    if (current.id && isStableId(current.id)) {
      parts.unshift(`#${CSS.escape(current.id)}`);
      paths.push(parts.join(' > '));
      break;
    }

    // Try data attributes
    const dataAttrs = getDataAttributes(current);
    if (Object.keys(dataAttrs).length > 0) {
      const [attr, value] = Object.entries(dataAttrs)[0];
      const tag = current.tagName.toLowerCase();
      parts.unshift(`${tag}[${attr}="${CSS.escape(value)}"]`);
      paths.push(parts.join(' > '));
    }

    // Try classes
    const classes = getStableClasses(current);
    if (classes.length > 0) {
      const tag = current.tagName.toLowerCase();
      const classStr = classes.slice(0, 3).map(c => `.${CSS.escape(c)}`).join('');
      parts.unshift(`${tag}${classStr}`);

      // Test if current path is unique
      const testPath = parts.join(' > ');
      if (isUniqueSelector(element, testPath)) {
        paths.push(testPath);
      }
    } else {
      // Use nth-of-type as fallback
      const nthOfType = generateNthOfTypeSelector(current);
      if (nthOfType) {
        parts.unshift(nthOfType);
      }
    }

    current = current.parentElement;
    depth++;
  }

  return paths;
}

/**
 * Generate optimal CSS selector for an element
 * Returns the most precise selector possible
 */
export function generateSelector(element: Element, options: SelectorOptions = {}): string {
  const opts = { ...defaultOptions, ...options };
  const candidates: string[] = [];

  // 1. Try ID (most precise)
  const idSelector = generateIdSelector(element);
  if (idSelector && isUniqueSelector(element, idSelector)) {
    return idSelector;
  }

  // 2. Try data attributes (very stable)
  const dataSelectors = generateDataAttributeSelector(element);
  for (const selector of dataSelectors) {
    if (isUniqueSelector(element, selector)) {
      candidates.push(selector);
    }
  }

  // 3. Try semantic attributes
  const semanticSelectors = generateSemanticSelector(element);
  for (const selector of semanticSelectors) {
    if (isUniqueSelector(element, selector)) {
      candidates.push(selector);
    }
  }

  // 4. Try class-based selectors
  const classSelectors = generateClassSelector(element);
  for (const selector of classSelectors) {
    if (isUniqueSelector(element, selector)) {
      candidates.push(selector);
    }
  }

  // If we found simple unique selectors, prefer more specific ones (with parent context)
  // Sort by specificity (longer paths are more specific) but cap at reasonable length
  if (candidates.length > 0) {
    const sortedCandidates = candidates.sort((a, b) => {
      const aDepth = (a.match(/>/g) || []).length + (a.match(/\s/g) || []).length;
      const bDepth = (b.match(/>/g) || []).length + (b.match(/\s/g) || []).length;

      // Prefer selectors with parent context (depth > 0)
      if (aDepth > 0 && bDepth === 0) return -1;
      if (aDepth === 0 && bDepth > 0) return 1;

      // If both have context or both don't, prefer data attributes and IDs
      if (a.includes('[data-') && !b.includes('[data-')) return -1;
      if (!a.includes('[data-') && b.includes('[data-')) return 1;
      if (a.startsWith('#') && !b.startsWith('#')) return -1;
      if (!a.startsWith('#') && b.startsWith('#')) return 1;

      // Otherwise prefer shorter
      return a.length - b.length;
    });
    return sortedCandidates[0];
  }

  // 5. Build path selectors (combining parent context)
  const pathSelectors = buildPathSelector(element, opts.maxDepth);
  for (const selector of pathSelectors) {
    if (isUniqueSelector(element, selector)) {
      candidates.push(selector);
    }
  }

  if (candidates.length > 0) {
    // Prefer more specific path selectors with parent context
    const sortedCandidates = candidates.sort((a, b) => {
      const aDepth = (a.match(/>/g) || []).length;
      const bDepth = (b.match(/>/g) || []).length;

      // Prefer deeper paths (more specific)
      if (aDepth !== bDepth) return bDepth - aDepth;

      // If same depth, prefer shorter
      return a.length - b.length;
    });
    return sortedCandidates[0];
  }

  // 6. Last resort: build very specific path with nth-child (more precise than nth-of-type)
  const tag = element.tagName.toLowerCase();
  const nthChild = generateNthChildSelector(element);
  if (nthChild) {
    let current: Element | null = element.parentElement;
    const parts: string[] = [nthChild];
    let depth = 0;

    while (current && depth < opts.maxDepth!) {
      if (current.id && isStableId(current.id)) {
        parts.unshift(`#${CSS.escape(current.id)}`);
        break;
      }

      const classes = getStableClasses(current);
      if (classes.length > 0) {
        const parentTag = current.tagName.toLowerCase();
        const classStr = classes.map(c => `.${CSS.escape(c)}`).join('');
        parts.unshift(`${parentTag}${classStr}`);
      } else {
        // Use nth-child for maximum specificity
        const parentNth = generateNthChildSelector(current);
        if (parentNth) {
          parts.unshift(parentNth);
        }
      }

      current = current.parentElement;
      depth++;
    }

    const finalSelector = parts.join(' > ');
    if (isUniqueSelector(element, finalSelector)) {
      return finalSelector;
    }
  }

  // Absolute fallback - use nth-of-type
  const nthOfType = generateNthOfTypeSelector(element);
  if (nthOfType) {
    return nthOfType;
  }

  return tag;
}

/**
 * Get common ignore regex patterns based on detected element types
 */
export function suggestIgnoreRegex(element: Element): string {
  const patterns: string[] = [];

  // Check for ad-related classes/ids
  const html = element.outerHTML.toLowerCase();
  if (html.includes('ad') || html.includes('advertisement')) {
    patterns.push('google-ad', 'doubleclick', 'ad-slot', 'advertisement');
  }

  // Check for analytics
  if (html.includes('analytics') || html.includes('gtag') || html.includes('ga-')) {
    patterns.push('analytics', 'gtag', '_ga', 'utm_');
  }

  // Check for social media
  if (html.includes('facebook') || html.includes('twitter') || html.includes('social')) {
    patterns.push('facebook', 'twitter', 'social-share');
  }

  // Check for timestamps/dates
  if (html.includes('time') || html.includes('date')) {
    patterns.push('\\d{4}-\\d{2}-\\d{2}', '\\d{2}:\\d{2}', 'timestamp');
  }

  // React-specific patterns
  if (html.includes('data-react') || html.includes('data-v-')) {
    patterns.push('data-reactid="[^"]*"', 'data-v-[a-z0-9]+');
  }

  // Dynamic IDs/hashes
  patterns.push('[a-f0-9]{32,}');  // MD5/SHA hashes

  return patterns.length > 0 ? patterns.join('|') : '';
}

/**
 * Extract text preview from selected element
 */
export function getElementPreview(element: Element, maxLength: number = 200): string {
  const text = element.textContent?.trim() || '';
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + '...';
}
