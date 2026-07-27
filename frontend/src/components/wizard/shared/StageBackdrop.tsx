import React from 'react';
import clsx from 'clsx';

interface StageBackdropProps {
  /** Stage content (floating cards / forms). */
  children: React.ReactNode;
  /** Dim the dot-grid (used when a sheet floats over the stage). */
  dimmed?: boolean;
  /** Extra classes for the scrolling content wrapper. */
  className?: string;
}

/**
 * StageBackdrop — the shared dot-grid "stage" the Studio shell renders under the
 * app-bar. Setup (floating intent/form cards) and the non-recorder modes render
 * their content centered on this canvas; Finalize dims it and floats a sheet on
 * top. The recorder owns its own full-bleed browser stage (BrowserRecorder), so
 * this backdrop is only used by the form/picker states.
 */
export const StageBackdrop: React.FC<StageBackdropProps> = ({ children, dimmed, className }) => {
  return (
    <div className="relative flex-1 min-h-0 overflow-hidden bg-canvas">
      {/* Dot-grid canvas — calmed to 0.12 (design language). */}
      <div
        className="absolute inset-0 pointer-events-none transition-opacity duration-300"
        style={{
          backgroundImage: 'radial-gradient(circle, #D6D7DC 1px, transparent 1px)',
          backgroundSize: '24px 24px',
          opacity: dimmed ? 0.05 : 0.12,
        }}
      />
      {/* Dim veil when a sheet floats over the stage. */}
      {dimmed && (
        <div className="absolute inset-0 bg-ink/[0.06] backdrop-blur-[1px] pointer-events-none transition-opacity duration-300" />
      )}
      <div className={clsx('relative z-10 h-full min-h-0 overflow-y-auto', className)}>
        {children}
      </div>
    </div>
  );
};
