'use client';

import type { MemoryScope } from '@/lib/api';

export type MemoryScopeFilter = 'all' | MemoryScope;

const SCOPES: Array<{ value: MemoryScopeFilter; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'private', label: 'Private' },
  { value: 'delegated', label: 'Delegated' },
  { value: 'project', label: 'Project' },
  { value: 'team', label: 'Team' },
  { value: 'organization', label: 'Org' },
  { value: 'shared', label: 'Shared' },
  { value: 'public', label: 'Public' },
];

interface MemoryScopeSwitcherProps {
  value: MemoryScopeFilter;
  onChange: (value: MemoryScopeFilter) => void;
  counts?: Partial<Record<MemoryScopeFilter, number>>;
}

export function MemoryScopeSwitcher({ value, onChange, counts = {} }: MemoryScopeSwitcherProps) {
  return (
    <div className="overflow-x-auto">
      <div className="inline-flex min-w-max items-center rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base p-1 shadow-card">
        {SCOPES.map(scope => {
          const active = scope.value === value;
          const count = counts[scope.value];
          return (
            <button
              key={scope.value}
              type="button"
              onClick={() => onChange(scope.value)}
              className={`flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-medium transition-colors ${
                active
                  ? 'bg-sc-purple/20 text-sc-purple'
                  : 'text-sc-fg-muted hover:bg-sc-bg-highlight hover:text-sc-fg-primary'
              }`}
              aria-pressed={active}
            >
              <span>{scope.label}</span>
              {typeof count === 'number' && (
                <span className={active ? 'text-sc-purple/80' : 'text-sc-fg-subtle'}>{count}</span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
