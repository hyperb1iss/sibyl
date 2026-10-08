'use client';

import Link from 'next/link';
import { useMemo } from 'react';
import { EnhancedEmptyState } from '@/components/ui/empty-state';
import { Activity, Folder, InfoCircle, Users, X } from '@/components/ui/icons';
import type { TeamActivityItem, TeamActivityPerson } from '@/lib/api/activity';
import {
  activityDayKey,
  formatActivityDay,
  formatActivityTime,
  TEAM_ACTIVITY_RECENT_LIMIT,
  teamActivityKindConfig,
} from '@/lib/constants/activity';
import { formatDateTime } from '@/lib/constants/formatting';
import { PersonAvatar } from './person-avatar';

interface ActivityFeedProps {
  /** Items to show, newest first, already narrowed to `person` when set. */
  items: TeamActivityItem[];
  /** Fallback names and avatars for items that arrive without them. */
  people: Map<string, TeamActivityPerson>;
  /** Fallback names for items that arrive without a project name. */
  projectNames: Record<string, string>;
  /** The page is scoped to this one project, so its badge would repeat. */
  scopedProjectId?: string;
  truncated: boolean;
  windowPhrase: string;
  person?: TeamActivityPerson;
  /**
   * The items are the server's answer for `person`, not the team feed
   * narrowed in the browser while that answer loads.
   */
  personScoped: boolean;
  onClearPerson: () => void;
  now?: number;
}

interface DayGroup {
  key: string;
  label: string;
  items: TeamActivityItem[];
}

function groupByDay(items: TeamActivityItem[], now?: number): DayGroup[] {
  const groups: DayGroup[] = [];
  for (const item of items) {
    const key = activityDayKey(item.at);
    const last = groups[groups.length - 1];
    if (last && last.key === key) {
      last.items.push(item);
    } else {
      groups.push({ key, label: formatActivityDay(item.at, now), items: [item] });
    }
  }
  return groups;
}

function kindLabel(item: TeamActivityItem): string {
  if (item.kind === 'entity' && item.entity_type) {
    return `Added ${item.entity_type.replace(/_/g, ' ')}`;
  }
  return teamActivityKindConfig(item.kind).label;
}

function FeedRow({
  item,
  actor,
  projectName,
  now,
}: {
  item: TeamActivityItem;
  actor?: TeamActivityPerson;
  projectName?: string;
  now?: number;
}) {
  const config = teamActivityKindConfig(item.kind);
  const Icon = config.icon;
  // The item names its actor; the people list covers older answers.
  const actorName = item.actor_name ?? actor?.name ?? 'Unknown member';
  const actorAvatar = item.actor_avatar_url ?? actor?.avatar_url;

  return (
    <li className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-3 px-4 py-3 transition-colors hover:bg-sc-bg-highlight/40">
      <span
        className={`mt-0.5 flex h-8 w-8 items-center justify-center rounded-lg border ${config.chip} ${config.text}`}
        aria-hidden="true"
      >
        <Icon width={15} height={15} />
      </span>
      <div className="min-w-0">
        <Link
          href={item.href}
          className="line-clamp-2 rounded text-sm font-medium break-words text-sc-fg-primary transition-colors hover:text-sc-purple focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
        >
          {item.title || 'Untitled'}
        </Link>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-sc-fg-muted">
          <span className={`font-medium ${config.text}`}>{kindLabel(item)}</span>
          <span aria-hidden="true" className="text-sc-fg-subtle">
            ·
          </span>
          <span className="inline-flex min-w-0 items-center gap-1.5">
            <PersonAvatar size="xs" name={actorName} seed={item.actor_id} avatarUrl={actorAvatar} />
            <span className="truncate">{actorName}</span>
          </span>
          {projectName && (
            <span className="inline-flex max-w-[12rem] items-center gap-1 rounded border border-sc-fg-subtle/20 bg-sc-bg-highlight px-1.5 py-0.5 text-[10px] text-sc-fg-secondary">
              <Folder width={10} height={10} aria-hidden="true" className="shrink-0" />
              <span className="truncate">{projectName}</span>
            </span>
          )}
        </div>
      </div>
      <time
        dateTime={item.at}
        title={formatDateTime(item.at)}
        className="pt-0.5 text-xs whitespace-nowrap text-sc-fg-muted tabular-nums"
      >
        {formatActivityTime(item.at, now)}
      </time>
    </li>
  );
}

/** The team's recent work, newest first, one block per day. */
export function ActivityFeed({
  items,
  people,
  projectNames,
  scopedProjectId,
  truncated,
  windowPhrase,
  person,
  personScoped,
  onClearPerson,
  now,
}: ActivityFeedProps) {
  const lookFurther = scopedProjectId
    ? ''
    : ' Pick one project to look further back in its history.';
  const groups = useMemo(() => groupByDay(items, now), [items, now]);

  return (
    <section
      aria-labelledby="team-activity-feed-heading"
      className="rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-elevated shadow-card"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-sc-fg-subtle/10 px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-sc-cyan/20 bg-sc-cyan/10">
            <Activity width={16} height={16} className="text-sc-cyan" aria-hidden="true" />
          </span>
          <h2
            id="team-activity-feed-heading"
            className="text-base font-semibold text-sc-fg-primary"
          >
            Activity
          </h2>
          <span className="rounded-full bg-sc-bg-highlight px-2 py-0.5 text-[11px] font-medium text-sc-fg-muted tabular-nums">
            {items.length}
          </span>
        </div>
        {person && (
          <button
            type="button"
            onClick={onClearPerson}
            className="inline-flex max-w-full items-center gap-2 rounded-full border border-sc-purple/30 bg-sc-purple/10 py-1 pr-2 pl-1 text-xs font-medium text-sc-purple transition-colors hover:bg-sc-purple/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
            aria-label={`Show everyone, not only ${person.name}`}
          >
            <PersonAvatar
              size="xs"
              name={person.name}
              seed={person.user_id}
              avatarUrl={person.avatar_url}
            />
            <span className="truncate">Only {person.name}</span>
            <X width={12} height={12} aria-hidden="true" className="shrink-0" />
          </button>
        )}
      </div>

      {items.length === 0 ? (
        person ? (
          personScoped ? (
            <EnhancedEmptyState
              icon={<Users width={40} height={40} className="text-sc-yellow" />}
              title={`Nothing from ${person.name} in ${windowPhrase}`}
              description="Their captures, tasks, decisions, and notes land here as they happen."
              variant="filtered"
              actions={[{ label: 'Show everyone', onClick: onClearPerson, variant: 'secondary' }]}
            />
          ) : (
            <EnhancedEmptyState
              icon={<Users width={40} height={40} className="text-sc-cyan" />}
              title={`Looking for ${person.name}'s work`}
              description={`Checking ${windowPhrase} past the team's latest ${TEAM_ACTIVITY_RECENT_LIMIT} updates.`}
            />
          )
        ) : (
          <EnhancedEmptyState
            icon={<Activity width={40} height={40} className="text-sc-fg-subtle" />}
            title={`No team activity in ${windowPhrase}`}
            description="Captures, tasks, decisions, and notes from everyone on the team show up here as they happen."
          />
        )
      ) : (
        <div className="divide-y divide-sc-fg-subtle/10">
          {groups.map(group => (
            <section key={group.key} aria-label={group.label}>
              <h3 className="flex items-center justify-between bg-sc-bg-highlight/40 px-4 py-1.5 text-[11px] font-semibold tracking-wider text-sc-fg-muted uppercase">
                <span>{group.label}</span>
                <span className="font-medium tabular-nums">{group.items.length}</span>
              </h3>
              <ul className="divide-y divide-sc-fg-subtle/10">
                {group.items.map(item => (
                  <FeedRow
                    key={`${item.kind}:${item.id}`}
                    item={item}
                    actor={people.get(item.actor_id)}
                    projectName={
                      item.project_id && item.project_id !== scopedProjectId
                        ? (item.project_name ?? projectNames[item.project_id])
                        : undefined
                    }
                    now={now}
                  />
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}

      {truncated && (!person || (personScoped && items.length > 0)) && (
        <p className="flex items-start gap-2 border-t border-sc-fg-subtle/10 px-4 py-3 text-xs text-sc-fg-muted">
          <InfoCircle
            width={14}
            height={14}
            aria-hidden="true"
            className="mt-px shrink-0 text-sc-cyan"
          />
          <span>
            {person
              ? `Showing the latest ${TEAM_ACTIVITY_RECENT_LIMIT} updates from ${person.name}.`
              : `Showing the latest ${TEAM_ACTIVITY_RECENT_LIMIT} team updates.`}
            {lookFurther}
          </span>
        </p>
      )}
    </section>
  );
}
