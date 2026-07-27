import React from 'react';

interface StatsGridProps {
  children: React.ReactNode;
  columns?: 2 | 3 | 4;
}

export const StatsGrid: React.FC<StatsGridProps> = ({ children, columns = 4 }) => {
  const colClass = {
    2: '@pair/stage:grid-cols-2',
    3: '@rail/stage:grid-cols-3',
    4: '@pair/stage:grid-cols-2 @split/stage:grid-cols-4',
  }[columns];

  return (
    <div className={`grid grid-cols-1 gap-4 ${colClass}`}>
      {children}
    </div>
  );
};
