import React, { useRef, useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import clsx from 'clsx';

/**
 * PageTransition — fades in content on route changes WITHOUT remounting.
 * Uses a ref + class toggle instead of key-based unmount/remount,
 * which eliminates the blank flash between pages.
 */
export const PageTransition: React.FC<{ children: React.ReactNode; className?: string }> = ({
  children,
  className,
}) => {
  const location = useLocation();
  const ref = useRef<HTMLDivElement>(null);
  const prevPath = useRef(location.pathname);

  useEffect(() => {
    // Only animate when the top-level route segment actually changes
    const prev = prevPath.current.split('/').slice(0, 3).join('/');
    const next = location.pathname.split('/').slice(0, 3).join('/');
    prevPath.current = location.pathname;
    if (prev === next) return;

    const el = ref.current;
    if (!el) return;

    // Remove then re-add the animation class to replay it
    el.classList.remove('animate-page-enter');
    // Force reflow so the browser registers the removal
    void el.offsetHeight;
    el.classList.add('animate-page-enter');
  }, [location.pathname]);

  return (
    <div ref={ref} className={clsx('animate-page-enter h-full', className)}>
      {children}
    </div>
  );
};

/**
 * SwapFade — replays a subtle enter animation whenever `swapKey` changes, so a
 * master-detail pane that swaps its content on selection glides in instead of
 * hard-cutting (the "item switch flickers" tell). Uses the same class-toggle +
 * forced-reflow trick as PageTransition, so the wrapper itself never remounts —
 * children manage their own mounting (a keyed child still remounts as before,
 * this just wraps the change in a soft fade). The `fade-in-scale` keyframe starts
 * at opacity 0.5 (not 0), so the pane never blanks to white mid-swap.
 */
export const SwapFade: React.FC<{
  swapKey: React.Key | null | undefined;
  children: React.ReactNode;
  className?: string;
}> = ({ swapKey, children, className }) => {
  const ref = useRef<HTMLDivElement>(null);
  const prevKey = useRef(swapKey);

  useEffect(() => {
    if (prevKey.current === swapKey) return;
    prevKey.current = swapKey;
    const el = ref.current;
    if (!el) return;
    el.classList.remove('animate-fade-in-scale');
    void el.offsetHeight; // Force reflow so the replay registers
    el.classList.add('animate-fade-in-scale');
  }, [swapKey]);

  return (
    <div ref={ref} className={clsx('animate-fade-in-scale', className)}>
      {children}
    </div>
  );
};

/**
 * Stagger — renders children with staggered fade-in animation.
 * Uses IntersectionObserver so items only animate once when scrolled into view.
 * Items start visible (no flicker) and the animation is purely additive via CSS class.
 */
interface StaggerProps {
  children: React.ReactNode;
  className?: string;
  staggerMs?: number;
}

export const Stagger: React.FC<StaggerProps> = ({
  children,
  className,
  staggerMs = 40,
}) => {
  const ref = useRef<HTMLDivElement>(null);
  const hasAnimated = useRef(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || hasAnimated.current) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          hasAnimated.current = true;
          observer.disconnect();
          // Add stagger animation to each direct child wrapper
          const wrappers = el.querySelectorAll(':scope > [data-stagger-item]');
          wrappers.forEach((child, i) => {
            const htmlChild = child as HTMLElement;
            htmlChild.style.animationDelay = `${i * staggerMs}ms`;
            htmlChild.classList.add('stagger-item-enter');
          });
        }
      },
      { threshold: 0.02 },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [staggerMs]);

  const items = React.Children.toArray(children);

  return (
    <div ref={ref} className={className}>
      {items.map((child, i) => (
        <div key={i} data-stagger-item="" className="stagger-item">
          {child}
        </div>
      ))}
    </div>
  );
};

/**
 * FadeIn — simple wrapper that fades in its children when mounted.
 * Useful for content that loads after a fetch.
 */
interface FadeInProps {
  children: React.ReactNode;
  className?: string;
  delay?: number;
  show?: boolean;
}

export const FadeIn: React.FC<FadeInProps> = ({
  children,
  className,
  delay = 0,
  show = true,
}) => {
  return (
    <div
      className={clsx(
        'transition-all duration-300 ease-out',
        show ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-2',
        className,
      )}
      style={{ transitionDelay: show ? `${delay}ms` : '0ms' }}
    >
      {children}
    </div>
  );
};

/**
 * AnimatedDropdown — renders a dropdown with enter/leave CSS transitions.
 * Keeps the element in the DOM during exit animation to avoid flicker.
 */
interface AnimatedDropdownProps {
  open: boolean;
  children: React.ReactNode;
  className?: string;
}

export const AnimatedDropdown: React.FC<AnimatedDropdownProps> = ({
  open,
  children,
  className,
}) => {
  // `visible` drives the enter/leave classes; `closing` keeps the node in the
  // DOM for the 150ms leave transition. Both are flipped off the previous `open`
  // DURING render — from an effect the mount would land a commit after the open,
  // and `visible` would still read true on the first closed frame, so the leave
  // transition would never play from its open state.
  const [visible, setVisible] = useState(false);
  const [closing, setClosing] = useState(false);
  const [wasOpen, setWasOpen] = useState(open);
  if (wasOpen !== open) {
    setWasOpen(open);
    setVisible(false); // every enter starts from the closed style
    setClosing(!open);
  }

  const shouldRender = open || closing;

  // Two frames after the closed style is committed, flip to the open style so
  // the browser has something to transition FROM.
  useEffect(() => {
    if (!open) return;
    let inner = 0;
    const outer = requestAnimationFrame(() => {
      inner = requestAnimationFrame(() => setVisible(true));
    });
    return () => {
      cancelAnimationFrame(outer);
      cancelAnimationFrame(inner);
    };
  }, [open]);

  // Unmount once the leave transition has run.
  useEffect(() => {
    if (!closing) return;
    const timer = setTimeout(() => setClosing(false), 150);
    return () => clearTimeout(timer);
  }, [closing]);

  if (!shouldRender) return null;

  return (
    <div
      className={clsx(
        'transition-all duration-150 ease-out origin-top-right',
        visible
          ? 'opacity-100 scale-100 translate-y-0'
          : 'opacity-0 scale-[0.97] -translate-y-1',
        className,
      )}
    >
      {children}
    </div>
  );
};
