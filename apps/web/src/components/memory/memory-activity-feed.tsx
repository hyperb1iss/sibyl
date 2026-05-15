'use client';

import Link from 'next/link';
import {
  Database,
  Eye,
  Key,
  LightBulb,
  Search,
  Send,
  Users,
  WarningCircle,
} from '@/components/ui/icons';
import type { MemoryAuditEvent } from '@/lib/api';
import { formatDistanceToNow } from '@/lib/constants';

interface MemoryActivityFeedProps {
  events: MemoryAuditEvent[];
  title?: string;
  emptyLabel?: string;
}

/**
 * Translate memory.* audit actions into short human verbs.
 * Server actions stay stable; this layer is the human-facing alias.
 */
const ACTION_LABELS: Record<string, string> = {
  'memory.recall': 'Memory recalled',
  'memory.context_pack': 'Context pack built',
  'memory.context_pack.deny': 'Context pack denied',
  'memory.remember': 'Memory saved',
  'memory.reflect': 'Reflection captured',
  'memory.reflect.deny': 'Reflection denied',
  'memory.reflect.promote': 'Reflection promoted',
  'memory.reflect.promote.preview': 'Promotion preview',
  'memory.reflect.dream_promote': 'Automatic promotion',
  'memory.reflect.dream_review': 'Exception routed',
  'memory.inspect': 'Source inspected',
  'memory.access.preview': 'Access preview',
  'memory.share.preview': 'Share preview',
  'memory.correction': 'Memory corrected',
  'memory.hide': 'Memory hidden',
  'memory.redact': 'Memory redacted',
  'memory.restore': 'Memory restored',
  'memory.delete': 'Memory deleted',
  'memory.policy_deny': 'Access denied',
  'memory.task_learning.episode': 'Task learning saved',
  'memory.task_learning.procedure': 'Task procedure saved',
  'memory.task_learning.manage_denied': 'Task learning denied',
};

/**
 * Translate stable policy reason codes into short human phrases.
 * Keep the underlying code for hover/title so power users can map back.
 */
const POLICY_REASON_LABELS: Record<string, string> = {
  private_principal_bound: 'Private to you',
  same_scope_read_allowed: 'Same scope',
  same_scope_write_allowed: 'Same scope · write',
  delegated_access_verified: 'Delegated access',
  project_access_verified: 'Project access',
  graph_projection_allowed: 'Graph projection',
  agent_diary_private_read_allowed: 'Agent diary',
  org_role_owner_inherit: 'Owner inherits',
  context_pack_rendered: 'Context pack',
  promoted: 'Promoted',
  promotion_preview_allowed: 'Promotion preview',
  unverified_membership: 'Unverified membership',
  scope_not_enabled: 'Scope not enabled',
  share_not_enabled: 'Share not enabled',
  missing_memory_scope: 'Missing memory scope',
  missing_scope_key: 'Missing scope key',
  missing_actor: 'Missing actor',
  missing_organization: 'Missing organization',
  missing_policy_context: 'Missing policy context',
  organization_mismatch: 'Organization mismatch',
  principal_mismatch: 'Principal mismatch',
  project_mismatch: 'Project mismatch',
  source_not_found: 'Source not found',
  memory_source_not_found: 'Source not found',
  candidate_not_found: 'Candidate not found',
  candidate_archived: 'Candidate archived',
  candidate_already_promoted: 'Already promoted',
  auto_promote_candidate: 'Auto-promote candidate',
  duplicate_candidate: 'Duplicate candidate',
  stale_candidate: 'Stale candidate',
  contradiction_candidate: 'Contradiction candidate',
  sensitive_candidate: 'Sensitive candidate',
  not_reflection_candidate: 'Not a reflection',
  invalid_correction_action: 'Invalid correction',
  scope_crossing_requires_promotion: 'Needs promotion to cross scopes',
  promote_to_scope_must_match_broadest_input_scope: 'Promotion scope mismatch',
  archived_project: 'Project archived',
  disabled: 'Disabled',
  rate_limited: 'Rate limited',
  not_configured: 'Not configured',
  not_owner: 'Not owner',
  no_citable_sources: 'No citable sources',
  no_materialized_sources: 'No sources materialized',
  required_source_ids_not_found: 'Required sources missing',
  missing_freshness_metadata: 'Freshness missing',
  unresolved_claim: 'Unresolved claim',
  duplicate_dedupe_key: 'Duplicate dedupe key',
  user_not_found: 'User not found',
  token_not_found: 'Token not found',
  missing_graph_project_id: 'Missing graph project',
};

const SURFACE_LABELS: Record<string, string> = {
  cli: 'CLI',
  mcp: 'MCP',
  api: 'API',
  web: 'Web',
  job: 'Background job',
  prompt_hook: 'Prompt hook',
  mcp_context: 'MCP context',
  memory_access_preview: 'Access preview',
  raw_recall: 'Raw recall',
  reflection_dream_cycle: 'Dream cycle',
  reflection_promote: 'Reflection promotion',
};

function shortId(value: string): string {
  if (value.length <= 28) return value;
  return `${value.slice(0, 12)}...${value.slice(-8)}`;
}

function receiptLinks(
  ids: string[],
  kind: 'source' | 'derived'
): Array<{ href: string; id: string; label: string }> {
  // Dedupe before slicing — duplicate ids are functionally redundant as
  // receipt links and otherwise collide on the React key.
  const seen = new Set<string>();
  const unique: string[] = [];
  for (const id of ids) {
    if (seen.has(id)) continue;
    seen.add(id);
    unique.push(id);
  }
  return unique.slice(0, 2).map(id => ({
    href:
      kind === 'source'
        ? `/memory/sources/${encodeURIComponent(id)}`
        : `/entities/${encodeURIComponent(id)}`,
    id,
    label: shortId(id),
  }));
}

function truncatedCount(ids: string[], truncated: number | null): number {
  return Math.max(ids.length - 2, 0) + (truncated ?? 0);
}

function actionLabel(action: string): string {
  if (ACTION_LABELS[action]) return ACTION_LABELS[action];
  // Fallback: strip "memory." prefix and title-case the rest
  return action
    .replace(/^memory\./, '')
    .split(/[._-]+/)
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function eventIcon(action: string) {
  if (action.includes('recall') || action.includes('context_pack')) return Search;
  if (action.includes('access')) return Key;
  if (action.includes('share')) return Send;
  if (action.includes('reflect')) return LightBulb;
  if (action.includes('inspect')) return Eye;
  if (action.includes('policy_deny') || action.includes('correction')) return WarningCircle;
  if (action.includes('remember')) return Database;
  return Users;
}

function eventTone(event: MemoryAuditEvent): string {
  if (event.policy_allowed === false) return 'text-sc-red bg-sc-red/10 border-sc-red/25';
  if (event.action.includes('access')) return 'text-sc-purple bg-sc-purple/10 border-sc-purple/25';
  if (event.action.includes('recall')) return 'text-sc-cyan bg-sc-cyan/10 border-sc-cyan/25';
  if (event.action.includes('reflect')) return 'text-sc-coral bg-sc-coral/10 border-sc-coral/25';
  return 'text-sc-green bg-sc-green/10 border-sc-green/25';
}

function policyLabel(event: MemoryAuditEvent): { label: string; raw: string | null } {
  const raw = event.policy_reason;
  if (!raw) {
    return {
      label:
        event.policy_allowed === true
          ? 'allowed'
          : event.policy_allowed === false
            ? 'denied'
            : 'recorded',
      raw: null,
    };
  }
  return { label: POLICY_REASON_LABELS[raw] ?? raw.replace(/_/g, ' '), raw };
}

function surfaceLabel(surface: string | null): string | null {
  if (!surface) return null;
  if (SURFACE_LABELS[surface]) return SURFACE_LABELS[surface];
  return surface
    .split(/[._-]+/)
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

export function MemoryActivityFeed({
  events,
  title = 'Activity Feed',
  emptyLabel = 'No memory activity yet',
}: MemoryActivityFeedProps) {
  return (
    <section className="rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-base shadow-card overflow-hidden">
      <div className="flex items-center justify-between border-b border-sc-fg-subtle/10 px-4 py-2.5">
        <h2 className="text-sm font-semibold text-sc-fg-primary">{title}</h2>
        <span className="rounded-full bg-sc-bg-highlight px-2 py-0.5 text-[11px] font-medium text-sc-fg-muted">
          {events.length}
        </span>
      </div>
      <div className="divide-y divide-sc-fg-subtle/10">
        {events.length === 0 ? (
          <p className="px-4 py-6 text-sm text-sc-fg-muted">{emptyLabel}</p>
        ) : (
          events.map(event => {
            const Icon = eventIcon(event.action);
            const policy = policyLabel(event);
            const surface = surfaceLabel(event.source_surface);
            return (
              <article
                key={event.id}
                className="grid grid-cols-[auto_minmax(0,1fr)] gap-3 px-4 py-2.5"
              >
                <span
                  className={`mt-0.5 flex h-7 w-7 items-center justify-center rounded-md border ${eventTone(event)}`}
                  title={event.action}
                >
                  <Icon width={13} height={13} />
                </span>
                <div className="min-w-0">
                  <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                    <p className="truncate text-sm font-medium text-sc-fg-primary">
                      {actionLabel(event.action)}
                    </p>
                    <span
                      className="rounded border border-sc-fg-subtle/15 px-1.5 py-0.5 text-[10px] text-sc-fg-muted"
                      title={policy.raw ?? undefined}
                    >
                      {policy.label}
                    </span>
                  </div>
                  <div className="mt-0.5 flex flex-wrap gap-x-2.5 gap-y-1 text-[11px] text-sc-fg-subtle">
                    {event.memory_scope && <span>{event.memory_scope}</span>}
                    {surface && <span>{surface}</span>}
                    {event.created_at && <span>{formatDistanceToNow(event.created_at)}</span>}
                  </div>
                  {(event.source_ids.length > 0 || event.derived_ids.length > 0) && (
                    <div className="mt-2 flex flex-wrap gap-1.5 text-[11px]">
                      {receiptLinks(event.source_ids, 'source').map(link => (
                        <Link
                          key={`source-${link.id}`}
                          href={link.href}
                          className="rounded border border-sc-cyan/20 bg-sc-cyan/10 px-1.5 py-0.5 text-sc-cyan transition-colors hover:border-sc-cyan/40 hover:bg-sc-cyan/15"
                          title={link.id}
                        >
                          source:{link.label}
                        </Link>
                      ))}
                      {truncatedCount(event.source_ids, event.source_ids_truncated) > 0 && (
                        <span className="rounded border border-sc-fg-subtle/15 px-1.5 py-0.5 text-sc-fg-subtle">
                          +{truncatedCount(event.source_ids, event.source_ids_truncated)} sources
                        </span>
                      )}
                      {receiptLinks(event.derived_ids, 'derived').map(link => (
                        <Link
                          key={`derived-${link.id}`}
                          href={link.href}
                          className="rounded border border-sc-purple/20 bg-sc-purple/10 px-1.5 py-0.5 text-sc-purple transition-colors hover:border-sc-purple/40 hover:bg-sc-purple/15"
                          title={link.id}
                        >
                          entity:{link.label}
                        </Link>
                      ))}
                      {truncatedCount(event.derived_ids, event.derived_ids_truncated) > 0 && (
                        <span className="rounded border border-sc-fg-subtle/15 px-1.5 py-0.5 text-sc-fg-subtle">
                          +{truncatedCount(event.derived_ids, event.derived_ids_truncated)} entities
                        </span>
                      )}
                    </div>
                  )}
                </div>
              </article>
            );
          })
        )}
      </div>
    </section>
  );
}
