import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { getAbout, type AboutInfo } from '../../api/about';

/**
 * SourceOffer — the human half of the AGPL-3.0 §13 network source offer.
 *
 * §13 entitles anyone who interacts with this coordinator *over a network* to
 * the complete corresponding source of the build they are talking to, so this
 * ships in two shapes: `SourceOfferLine` for the login screen (reachable
 * without an account) and `SourceOffer` for Settings → General (the owner's
 * full block, with version and the configured repository spelled out).
 *
 * The repository link is whatever the operator configured (WRIT_SOURCE_URL) —
 * on a modified build that is their fork, not upstream. Never substitute a
 * hardcoded URL here; that would make the offer false.
 */

/** Only ever link out to a real web URL — an operator typo shouldn't become a
 *  `javascript:` sink. Anything else renders as inert text. */
const webUrl = (url: string | undefined): string | null => {
  if (!url) return null;
  try {
    const u = new URL(url, window.location.origin);
    return u.protocol === 'https:' || u.protocol === 'http:' ? u.href : null;
  } catch {
    return null;
  }
};

const useAbout = (): AboutInfo | null => {
  const [about, setAbout] = useState<AboutInfo | null>(null);
  useEffect(() => {
    let alive = true;
    getAbout()
      .then((a) => alive && setAbout(a))
      .catch(() => {
        /* The offer is also in the shipped README/LICENSE — never block the UI. */
      });
    return () => {
      alive = false;
    };
  }, []);
  return about;
};

const linkClass =
  'underline underline-offset-2 decoration-border hover:decoration-current hover:text-ink transition-colors';

/**
 * One line, for the login screen footer. Renders its own leading separator so
 * the footer never shows a dangling "·" while /api/about is still in flight.
 */
export const SourceOfferLine: React.FC = () => {
  const { t } = useTranslation();
  const about = useAbout();
  if (!about) return null;
  const source = webUrl(about.source_url);
  const license = webUrl(about.license_url);

  return (
    <span>
      {' · '}
      {about.license === 'AGPL-3.0-only' && license ? (
        <a href={license} target="_blank" rel="noopener noreferrer" className={linkClass}>
          AGPL-3.0
        </a>
      ) : (
        about.license
      )}
      {' · '}
      {source ? (
        <a href={source} target="_blank" rel="noopener noreferrer" className={linkClass}>
          {t('Source code')}
        </a>
      ) : (
        <span title={about.source_url}>{t('Source code')}</span>
      )}
    </span>
  );
};

/** The full block, for Settings → General. */
export const SourceOffer: React.FC = () => {
  const { t } = useTranslation();
  const about = useAbout();
  if (!about) return null;
  const source = webUrl(about.source_url);
  const license = webUrl(about.license_url);

  return (
    <div className="border-t border-border pt-4">
      <p className="text-[13px] font-medium text-ink">{t('About')}</p>
      <dl className="mt-2 grid @pair/stage:grid-cols-2 gap-x-6 gap-y-1.5 text-xs">
        <div className="flex items-baseline justify-between gap-3">
          <dt className="text-tertiary">{t('Version')}</dt>
          <dd className="text-secondary font-mono">
            {about.name} {about.version}
          </dd>
        </div>
        <div className="flex items-baseline justify-between gap-3">
          <dt className="text-tertiary">{t('License')}</dt>
          <dd className="text-secondary">
            {license ? (
              <a href={license} target="_blank" rel="noopener noreferrer" className={linkClass}>
                {about.license}
              </a>
            ) : (
              about.license
            )}
          </dd>
        </div>
        <div className="flex items-baseline justify-between gap-3 @pair/stage:col-span-2">
          <dt className="text-tertiary shrink-0">{t('Source code')}</dt>
          <dd className="text-secondary truncate">
            {source ? (
              <a
                href={source}
                target="_blank"
                rel="noopener noreferrer"
                className={linkClass}
                title={about.source_url}
              >
                {about.source_url}
              </a>
            ) : (
              about.source_url
            )}
          </dd>
        </div>
      </dl>
      <p className="text-xs text-tertiary mt-2.5 leading-relaxed max-w-2xl">
        {t(
          'This coordinator is free software under the AGPL-3.0. Anyone you let use it over a network is entitled to its complete corresponding source.',
        )}{' '}
        {about.modified
          ? t('Source is served from the repository this operator configured.')
          : t(
              'If you deploy a modified build, set WRIT_SOURCE_URL to your own repository so this link stays accurate.',
            )}
      </p>
    </div>
  );
};

export default SourceOffer;
