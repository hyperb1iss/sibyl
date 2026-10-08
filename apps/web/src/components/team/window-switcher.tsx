'use client';

import type { TeamActivityWindow } from '@/lib/api/activity';
import { TEAM_ACTIVITY_WINDOW_CONFIG, TEAM_ACTIVITY_WINDOWS } from '@/lib/constants/activity';

interface WindowSwitcherProps {
  value: TeamActivityWindow;
  onChange: (value: TeamActivityWindow) => void;
}

/** Segmented 24h / 7 days / 30 days control. */
export function WindowSwitcher({ value, onChange }: WindowSwitcherProps) {
  return (
    <div
      role="group"
      aria-label="Time window"
      className="flex items-center rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base p-1 shadow-card xs:inline-flex"
    >
      {TEAM_ACTIVITY_WINDOWS.map(option => {
        const active = option === value;
        return (
          <button
            key={option}
            type="button"
            aria-pressed={active}
            onClick={() => {
              if (!active) onChange(option);
            }}
            className={`flex h-8 flex-1 items-center justify-center rounded-lg px-3 xs:flex-none text-xs font-medium whitespace-nowrap transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base ${
              active
                ? 'bg-sc-purple/20 text-sc-purple'
                : 'text-sc-fg-muted hover:bg-sc-bg-highlight hover:text-sc-fg-primary'
            }`}
          >
            {TEAM_ACTIVITY_WINDOW_CONFIG[option].label}
          </button>
        );
      })}
    </div>
  );
}
