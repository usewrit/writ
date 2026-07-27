import clsx from 'clsx';
import { tintStyle, type Tint } from '../../utils/tint';

type IconType = React.ComponentType<{ className?: string }>;

interface IconTileProps {
  icon: IconType;
  /** Category tint — the soft thumbnail color. Defaults to neutral (gray). */
  tint?: Tint;
  size?: 'xs' | 'sm' | 'md' | 'lg';
  /** Radius override (defaults to rounded-lg; pass e.g. 'rounded-md' for tighter rows). */
  rounded?: string;
  className?: string;
}

const TILE: Record<NonNullable<IconTileProps['size']>, string> = {
  xs: 'h-6 w-6',
  sm: 'h-8 w-8',
  md: 'h-10 w-10',
  lg: 'h-12 w-12',
};
const GLYPH: Record<NonNullable<IconTileProps['size']>, string> = {
  xs: 'h-3.5 w-3.5',
  sm: 'h-4 w-4',
  md: 'h-5 w-5',
  lg: 'h-6 w-6',
};

/**
 * IconTile — a soft, color-tinted thumbnail behind an item's icon. The single
 * primitive for "color from content" across the app: workflow/monitor/persona/
 * template/listing tiles all use it so each item carries a calm color identity
 * while the surrounding chrome stays monochrome. The tint resolves to a per-theme
 * gradient (see styles/theme.css + utils/tint.ts), so it's dark-mode correct.
 */
export function IconTile({ icon: Icon, tint = 'neutral', size = 'md', rounded = 'rounded-lg', className }: IconTileProps) {
  return (
    <span
      aria-hidden="true"
      style={tintStyle(tint)}
      className={clsx('inline-grid shrink-0 place-items-center', TILE[size], rounded, className)}
    >
      <Icon className={GLYPH[size]} />
    </span>
  );
}

export default IconTile;
