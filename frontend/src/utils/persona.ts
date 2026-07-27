// Deterministic (no-AI) client-side detection of whether a workflow authenticates.
// Mirrors the backend _workflow_has_login so the persona UI only appears where a
// login / password / email input was detected (e.g. in the creation wizard,
// before the workflow is saved and a server-side `has_login` is available).

const CRED_FORM_KEYS = ['password', 'email', 'username', 'login', 'user'];

export function workflowHasLogin(steps?: any[], formData?: Record<string, any> | null): boolean {
  const fd = formData || {};
  for (const k of Object.keys(fd)) {
    const lk = String(k).toLowerCase();
    if (lk.startsWith('__secret_') || CRED_FORM_KEYS.includes(lk)) return true;
  }
  if (Array.isArray(steps)) {
    for (const s of steps) {
      if (s && typeof s === 'object' && s.type === 'twofa') return true;
    }
    const blob = JSON.stringify(steps);
    if (/\{\{\s*secret:\s*(password|username|email|login|user)/i.test(blob)) return true;
    if (/type=['"]?password|autocomplete=['"]?(current-password|new-password|username|email)|name=['"]?(password|email|username|login)|\bpassword\b/i.test(blob)) return true;
  }
  return false;
}
