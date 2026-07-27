/**
 * Client-side helpers for importing EXISTING authenticator secrets.
 *
 * `parseOtpauthUri` reads a single `otpauth://totp/...` URI entirely in the
 * browser — the secret lives in the query string, so a single-persona "scan/paste
 * your authenticator setup link" flow needs no backend round-trip. The bulk
 * import (Google Authenticator export / many accounts) is parsed server-side; here
 * we only decode the QR image to the underlying URI before sending it on.
 */

export interface ParsedOtpauth {
  type: 'totp';
  secret: string;          // base32, normalized (upper-case, no spaces/padding)
  issuer?: string;
  label?: string;
  algorithm: string;       // SHA1 | SHA256 | SHA512
  digits: number;          // 6 | 8
  period: number;          // seconds
}

export function isBase32(s: string): boolean {
  return /^[A-Z2-7]+$/.test((s || '').replace(/\s/g, '').toUpperCase().replace(/=+$/, ''));
}

/** Parse a single otpauth://totp/... URI. Returns null for HOTP or anything malformed. */
export function parseOtpauthUri(uri: string): ParsedOtpauth | null {
  const raw = (uri || '').trim();
  if (!/^otpauth:\/\//i.test(raw)) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if ((url.host || '').toLowerCase() !== 'totp') return null; // hotp can't be time-minted
  const params = url.searchParams;
  const secret = (params.get('secret') || '').replace(/[\s-]/g, '').toUpperCase().replace(/=+$/, '');
  if (!secret || !isBase32(secret)) return null;

  let label: string | undefined = decodeURIComponent(url.pathname.replace(/^\//, '')) || undefined;
  let issuer: string | undefined = params.get('issuer') || undefined;
  if (label && label.includes(':')) {
    const [prefix, ...rest] = label.split(':');
    if (!issuer && prefix.trim()) issuer = prefix.trim();
    label = rest.join(':').trim() || label;
  }

  let algorithm = (params.get('algorithm') || 'SHA1').toUpperCase();
  if (!['SHA1', 'SHA256', 'SHA512'].includes(algorithm)) algorithm = 'SHA1';
  let digits = parseInt(params.get('digits') || '6', 10);
  if (![6, 8].includes(digits)) digits = 6;
  let period = parseInt(params.get('period') || '30', 10);
  if (!period || period < 1) period = 30;

  return { type: 'totp', secret, issuer, label, algorithm, period, digits };
}

export function isMigrationUri(s: string): boolean {
  return /^otpauth-migration:\/\//i.test((s || '').trim());
}

// ── Google Authenticator export (otpauth-migration://) ──────────────────────
// A single QR encodes a base64 protobuf holding EVERY enrolled account at once.
// We decode it entirely in the browser (mirrors backend authenticator_import.py)
// so the create/edit form can offer the accounts and fill the chosen seed — no
// round-trip and no bulk-persona creation.

const B32_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
const MIG_ALGO_BY_ENUM: Record<number, string> = { 0: 'SHA1', 1: 'SHA1', 2: 'SHA256', 3: 'SHA512' };
const MIG_DIGITS_BY_ENUM: Record<number, number> = { 0: 6, 1: 6, 2: 8 };

/** RFC 4648 base32 encode (unpadded) — Google stores the secret as raw bytes. */
function base32Encode(data: Uint8Array): string {
  let bits = 0, value = 0, out = '';
  for (let i = 0; i < data.length; i++) {
    value = (value << 8) | data[i];
    bits += 8;
    while (bits >= 5) {
      out += B32_ALPHABET[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
    value &= (1 << bits) - 1; // keep only the leftover low bits (stays < 2^8)
  }
  if (bits > 0) out += B32_ALPHABET[(value << (5 - bits)) & 31];
  return out;
}

/** Decode standard or url-safe base64, tolerating missing padding. */
function b64decodeLoose(input: string): Uint8Array | null {
  let s = (input || '').trim().replace(/\s/g, '').replace(/-/g, '+').replace(/_/g, '/');
  s += '='.repeat((4 - (s.length % 4)) % 4);
  try {
    const bin = atob(s);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  } catch {
    return null;
  }
}

function readVarint(buf: Uint8Array, start: number): [number, number] {
  let result = 0, shift = 0, i = start;
  for (;;) {
    if (i >= buf.length) throw new Error('truncated varint');
    const b = buf[i++];
    // Multiply (not <<) so varints wider than 31 bits don't overflow.
    result += (b & 0x7f) * Math.pow(2, shift);
    if (!(b & 0x80)) return [result, i];
    shift += 7;
    if (shift > 63) throw new Error('varint too long');
  }
}

type PbField = [number, number, number | Uint8Array];

/** Minimal protobuf wire reader — varint + length-delimited, skips the rest. */
function iterProtobufFields(buf: Uint8Array): PbField[] {
  const out: PbField[] = [];
  let i = 0;
  while (i < buf.length) {
    let tag: number;
    [tag, i] = readVarint(buf, i);
    const field = Math.floor(tag / 8);
    const wire = tag & 0x07;
    if (wire === 0) {
      let val: number;
      [val, i] = readVarint(buf, i);
      out.push([field, 0, val]);
    } else if (wire === 2) {
      let len: number;
      [len, i] = readVarint(buf, i);
      if (i + len > buf.length) throw new Error('truncated field');
      out.push([field, 2, buf.subarray(i, i + len)]);
      i += len;
    } else if (wire === 5) { out.push([field, 5, 0]); i += 4; }
    else if (wire === 1) { out.push([field, 1, 0]); i += 8; }
    else throw new Error('unsupported wire type');
  }
  return out;
}

function parseOtpParameters(buf: Uint8Array): ParsedOtpauth | null {
  let secretBytes: Uint8Array | null = null;
  let name: string | undefined;
  let issuer: string | undefined;
  let algorithm = 'SHA1';
  let digits = 6;
  let otpType = 2; // default TOTP
  try {
    const dec = new TextDecoder();
    for (const [field, wire, value] of iterProtobufFields(buf)) {
      if (field === 1 && wire === 2 && value instanceof Uint8Array) secretBytes = value;
      else if (field === 2 && wire === 2 && value instanceof Uint8Array) name = dec.decode(value) || undefined;
      else if (field === 3 && wire === 2 && value instanceof Uint8Array) issuer = dec.decode(value) || undefined;
      else if (field === 4 && wire === 0) algorithm = MIG_ALGO_BY_ENUM[value as number] || 'SHA1';
      else if (field === 5 && wire === 0) digits = MIG_DIGITS_BY_ENUM[value as number] || 6;
      else if (field === 6 && wire === 0) otpType = value as number;
    }
  } catch {
    return null; // one bad sub-message shouldn't kill the whole import
  }
  if (otpType !== 2) return null;          // 1=HOTP (counter-based) can't be time-minted
  if (!secretBytes || !secretBytes.length) return null;
  const secret = base32Encode(secretBytes);
  if (!secret) return null;

  let label = name;
  if (name && name.includes(':')) {
    const [prefix, ...rest] = name.split(':');
    if (!issuer && prefix.trim()) issuer = prefix.trim();
    label = rest.join(':').trim() || name;
  }
  // Google's migration export is always 30s period.
  return { type: 'totp', secret, issuer, label, algorithm, digits, period: 30 };
}

/**
 * Parse a Google Authenticator export. Accepts the full
 * `otpauth-migration://offline?data=<urlencoded-base64>` URI or just the raw
 * data blob. Returns every TOTP account it contains (HOTP entries skipped).
 */
export function parseMigrationUri(input: string): ParsedOtpauth[] {
  let dataB64 = (input || '').trim();
  // Pull the data= param without URLSearchParams, which would turn '+' into a
  // space and corrupt the base64.
  if (/data=/i.test(dataB64) || isMigrationUri(dataB64)) {
    const m = dataB64.match(/[?&]data=([^&#]*)/i);
    dataB64 = m ? m[1] : '';
  }
  if (!dataB64) return [];
  try {
    dataB64 = decodeURIComponent(dataB64);
  } catch {
    /* not percent-encoded — use as-is */
  }
  const raw = b64decodeLoose(dataB64);
  if (!raw) return [];

  let fields: PbField[];
  try {
    fields = iterProtobufFields(raw);
  } catch {
    return [];
  }
  const out: ParsedOtpauth[] = [];
  for (const [field, wire, value] of fields) {
    if (field === 1 && wire === 2 && value instanceof Uint8Array) {
      const entry = parseOtpParameters(value);
      if (entry) out.push(entry);
    }
  }
  return out;
}

/** True when the browser can decode a QR image (Chromium/Chrome ships BarcodeDetector). */
export function canDecodeQr(): boolean {
  return typeof (window as any).BarcodeDetector !== 'undefined';
}

/**
 * Decode the first QR code in an image file to its raw text (an otpauth:// or
 * otpauth-migration:// URI). Returns null if no QR is found or the browser has no
 * BarcodeDetector — callers should fall back to manual paste.
 */
export async function decodeQrFromImageFile(file: File): Promise<string | null> {
  const BD = (window as any).BarcodeDetector;
  if (!BD) return null;
  let bitmap: ImageBitmap | null = null;
  try {
    const detector = new BD({ formats: ['qr_code'] });
    bitmap = await createImageBitmap(file);
    const codes = await detector.detect(bitmap);
    if (!codes || !codes.length) return null;
    // Prefer a recognizable otpauth result when a screenshot holds more than one code.
    const otp = codes.find((c: any) => /^otpauth/i.test(c.rawValue || ''));
    return (otp || codes[0]).rawValue || null;
  } catch {
    return null;
  } finally {
    bitmap?.close?.();
  }
}
