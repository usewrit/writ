import React from 'react';
import { Link } from 'react-router-dom';
import { ArrowRightIcon } from '@heroicons/react/24/outline';
import { SectionHead } from '../common/SectionHead';
import { Button } from '../ui/Button';

/**
 * A shallow pointer section — the full UI for this concern lives on a dedicated
 * page (Developers for API Keys, Fleet for Agents). Settings shows a summary +
 * a link rather than duplicating the surface.
 */
export const PointerSection: React.FC<{
  title: string;
  description: string;
  linkTo: string;
  linkLabel: string;
  bullets?: string[];
}> = ({ title, description, linkTo, linkLabel, bullets }) => (
  <div className="space-y-6">
    <SectionHead title={title} description={description} />
    <div className="border-t border-border pt-4">
      {bullets && bullets.length > 0 && (
        <ul className="mb-4 space-y-1.5 text-sm text-secondary list-disc pl-5">
          {bullets.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      )}
      <Link to={linkTo}>
        <Button variant="secondary">
          {linkLabel}
          <ArrowRightIcon className="w-4 h-4" />
        </Button>
      </Link>
    </div>
  </div>
);

export default PointerSection;
