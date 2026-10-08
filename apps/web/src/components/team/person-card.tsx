'use client';

import type { TeamActivityCounts, TeamActivityPerson } from '@/lib/api/activity';
import {
  formatActivityTime,
  TEAM_ACTIVITY_COUNT_CONFIG,
  TEAM_ACTIVITY_COUNT_KEYS,
  teamActivityTotal,
  teamRoleConfig,
} from '@/lib/constants/activity';
import { PersonAvatar } from './person-avatar';

interface PersonCardProps {
  person: TeamActivityPerson;
  /** The busiest teammate's total, so bar lengths compare across cards. */
  maxTotal: number;
  /** "the last 7 days" */
  windowPhrase: string;
  selected: boolean;
  isYou: boolean;
  onToggle: (userId: string) => void;
  now?: number;
}

function ActivityBar({
  counts,
  total,
  maxTotal,
}: {
  counts: TeamActivityCounts;
  total: number;
  maxTotal: number;
}) {
  const width = maxTotal > 0 ? Math.max(6, (total / maxTotal) * 100) : 0;
  return (
    <div
      className="mt-4 h-1.5 w-full overflow-hidden rounded-full bg-sc-bg-dark"
      aria-hidden="true"
    >
      <div className="flex h-full overflow-hidden rounded-full" style={{ width: `${width}%` }}>
        {TEAM_ACTIVITY_COUNT_KEYS.filter(key => counts[key] > 0).map(key => (
          <div
            key={key}
            className={TEAM_ACTIVITY_COUNT_CONFIG[key].fill}
            style={{ width: `${(counts[key] / total) * 100}%` }}
          />
        ))}
      </div>
    </div>
  );
}

/**
 * One teammate's window at a glance. The whole card toggles the feed filter;
 * the name is the button, stretched over the card, so screen readers hear a
 * short name and the counts stay readable content.
 */
export function PersonCard({
  person,
  maxTotal,
  windowPhrase,
  selected,
  isYou,
  onToggle,
  now,
}: PersonCardProps) {
  const total = teamActivityTotal(person.counts);
  const quiet = total === 0;
  const role = teamRoleConfig(person.role);
  const RoleIcon = role.icon;
  const lastActive = person.last_active_at
    ? `Last active ${formatActivityTime(person.last_active_at, now)}`
    : 'No activity yet';

  return (
    <article
      data-quiet={quiet ? '' : undefined}
      className={`relative rounded-xl border p-4 transition-all duration-200 ${
        selected
          ? 'border-sc-purple/60 bg-sc-bg-elevated shadow-glow-purple'
          : quiet
            ? 'border-dashed border-sc-fg-subtle/25 bg-sc-bg-elevated/50 hover:border-sc-fg-subtle/50'
            : 'border-sc-fg-subtle/20 bg-sc-bg-elevated shadow-card hover:border-sc-purple/40 hover:shadow-card-hover'
      }`}
    >
      <div className="flex items-start gap-3">
        <PersonAvatar
          size="md"
          name={person.name}
          seed={person.user_id}
          avatarUrl={person.avatar_url}
          muted={quiet}
        />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-1.5">
            <button
              type="button"
              aria-pressed={selected}
              onClick={() => onToggle(person.user_id)}
              title={
                selected ? 'Show everyone in the feed' : `Show only ${person.name} in the feed`
              }
              className={`min-w-0 truncate text-left text-sm font-semibold transition-colors after:absolute after:inset-0 after:rounded-xl after:content-[''] focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-sc-cyan focus-visible:after:ring-offset-2 focus-visible:after:ring-offset-sc-bg-dark ${
                quiet ? 'text-sc-fg-secondary' : 'text-sc-fg-primary'
              }`}
            >
              {person.name}
            </button>
            {isYou && <span className="shrink-0 text-xs text-sc-purple">(you)</span>}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-sc-fg-muted">
            <span
              className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium ${role.badge}`}
            >
              <RoleIcon width={10} height={10} aria-hidden="true" />
              {role.label}
            </span>
            <span>{lastActive}</span>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <p
            className={`text-2xl font-bold leading-none tabular-nums ${
              quiet ? 'text-sc-fg-muted' : 'text-sc-fg-primary'
            }`}
          >
            {total}
          </p>
          <p className="mt-1 text-[10px] uppercase tracking-wider text-sc-fg-muted">
            {total === 1 ? 'update' : 'updates'}
          </p>
        </div>
      </div>

      {quiet ? (
        <p className="mt-4 text-xs text-sc-fg-muted">No activity in {windowPhrase}</p>
      ) : (
        <>
          <ActivityBar counts={person.counts} total={total} maxTotal={maxTotal} />
          <ul className="mt-3 flex flex-wrap gap-1.5" aria-label={`${person.name}'s activity`}>
            {TEAM_ACTIVITY_COUNT_KEYS.filter(key => person.counts[key] > 0).map(key => {
              const config = TEAM_ACTIVITY_COUNT_CONFIG[key];
              const Icon = config.icon;
              const count = person.counts[key];
              return (
                <li
                  key={key}
                  className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] ${config.chip}`}
                >
                  <Icon width={11} height={11} className={config.text} aria-hidden="true" />
                  <span className="font-semibold tabular-nums text-sc-fg-primary">{count}</span>
                  <span className="text-sc-fg-muted">
                    {count === 1 ? config.singular : config.label}
                  </span>
                </li>
              );
            })}
          </ul>
        </>
      )}

      {selected && (
        <span className="pointer-events-none absolute -top-2 right-3 rounded-full bg-sc-purple px-2 py-0.5 text-[10px] font-semibold text-sc-on-accent shadow-glow-purple">
          Filtering feed
        </span>
      )}
    </article>
  );
}
