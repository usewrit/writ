import React from 'react';
import { useTranslation } from 'react-i18next';
import client from '../../api/client';
import clsx from 'clsx';

/**
 * Renders an image served by an AUTHENTICATED, same-origin API route.
 *
 * A plain `<img src>` can't send the app's Bearer token, and our private images
 * (e.g. visual-zone monitor snapshots) aren't public — so we blob-fetch the bytes
 * through the authenticated axios `client` (token attached by its interceptor),
 * then render from an object URL that is revoked on unmount/`src` change so we
 * never leak blob URLs. `src` is a path relative to the client's `/api` base
 * (e.g. `/targets/12/changes/34/snapshot/before`).
 */
export const AuthImage: React.FC<{
  src?: string | null;
  alt: string;
  className?: string;
  /** Tile shown while loading and on error / when `src` is absent. */
  fallbackClassName?: string;
  /** Custom node rendered while loading and on error / when `src` is absent
   *  (e.g. a favicon's globe glyph). Takes precedence over `fallbackClassName`. */
  fallback?: React.ReactNode;
}> = ({ src, alt, className, fallbackClassName, fallback }) => {
  const { t } = useTranslation();
  // Both the object URL and the failure are stored WITH the `src` they belong to.
  // That makes "not loaded yet" derivable during render when `src` changes — an
  // effect resetting them would cost a render pass and, worse, leave the previous
  // image on screen under the new `src` until that pass landed.
  const [loaded, setLoaded] = React.useState<{ src: string; url: string } | null>(null);
  const [failedSrc, setFailedSrc] = React.useState<string | null>(null);

  const url = loaded && loaded.src === src ? loaded.url : null;
  const error = failedSrc != null && failedSrc === src;

  React.useEffect(() => {
    if (!src) return;
    let revoked = false;
    let objectUrl: string | null = null;
    client
      .get(src, { responseType: 'blob' })
      .then((res) => {
        if (revoked) return;
        objectUrl = URL.createObjectURL(res.data as Blob);
        setLoaded({ src, url: objectUrl });
      })
      .catch(() => {
        if (!revoked) setFailedSrc(src);
      });
    return () => {
      revoked = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [src]);

  if (error || !src) {
    if (fallback !== undefined) return <>{fallback}</>;
    return (
      <div className={clsx('flex items-center justify-center text-[10px] text-tertiary', fallbackClassName)}>
        {t('n/a')}
      </div>
    );
  }
  if (!url) {
    if (fallback !== undefined) return <>{fallback}</>;
    return (
      <div className={clsx('flex items-center justify-center', fallbackClassName)}>
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-300 border-t-zinc-800" />
      </div>
    );
  }
  return <img src={url} alt={alt} loading="lazy" className={className} onError={() => setFailedSrc(src)} />;
};

export default AuthImage;
