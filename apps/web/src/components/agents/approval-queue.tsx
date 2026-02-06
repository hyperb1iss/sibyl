'use client';

/**
 * Approval Queue - Compact approval cards for agents page.
 *
 * Clickable cards that link to agent chat threads.
 */

import { formatDistanceToNow } from 'date-fns';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { memo, useMemo } from 'react';

import { Card, Section } from '@/components/ui/card';
import {
  AlertTriangle,
  Check,
  Clock,
  Code,
  InfoCircle,
  WarningCircle,
  Xmark,
} from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import type { Approval, ApprovalType } from '@/lib/api';
import { useDismissApproval, usePendingApprovals, useRespondToApproval } from '@/lib/hooks';

// Type config - minimal
const TYPE_CONFIG: Record<ApprovalType, { icon: typeof Code; color: string }> = {
  destructive_command: { icon: AlertTriangle, color: 'text-sc-red' },
  sensitive_file: { icon: WarningCircle, color: 'text-sc-yellow' },
  file_write: { icon: Code, color: 'text-sc-purple' },
  external_api: { icon: Code, color: 'text-sc-cyan' },
  cost_threshold: { icon: AlertTriangle, color: 'text-sc-yellow' },
  review_phase: { icon: Check, color: 'text-sc-purple' },
  question: { icon: InfoCircle, color: 'text-sc-cyan' },
  scope_change: { icon: AlertTriangle, color: 'text-sc-yellow' },
  merge_conflict: { icon: WarningCircle, color: 'text-sc-red' },
  test_failure: { icon: WarningCircle, color: 'text-sc-red' },
};

/** Extract display text from approval */
function getDisplayText(approval: Approval): string {
  const metadata = approval.metadata as Record<string, unknown> | undefined;
  if (metadata?.file_path) {
    const path = metadata.file_path as string;
    const parts = path.split('/');
    return parts.length > 2 ? `.../${parts.slice(-2).join('/')}` : path;
  }
  if (metadata?.command) {
    const cmd = metadata.command as string;
    return cmd.length > 40 ? `${cmd.slice(0, 37)}...` : cmd;
  }
  if (metadata?.url) {
    try {
      return new URL(metadata.url as string).hostname;
    } catch {
      return (metadata.url as string).slice(0, 30);
    }
  }
  return approval.title.slice(0, 40);
}

// =============================================================================
// Compact Approval Card
// =============================================================================

interface ApprovalCardProps {
  approval: Approval;
  onRespond: (id: string, action: 'approve' | 'deny') => void;
  onDismiss: (id: string) => void;
  isResponding: boolean;
  isDismissing: boolean;
  projectFilter?: string;
  onAgentClick?: (id: string) => void;
}

const ApprovalCard = memo(function ApprovalCard({
  approval,
  onRespond,
  onDismiss,
  isResponding,
  isDismissing,
  projectFilter,
  onAgentClick,
}: ApprovalCardProps) {
  const config = TYPE_CONFIG[approval.approval_type] || {
    icon: InfoCircle,
    color: 'text-sc-fg-muted',
  };
  const Icon = config.icon;
  const displayText = getDisplayText(approval);
  const createdAt = approval.created_at ? new Date(approval.created_at) : null;

  const headerContent = (
    <>
      <div className="flex items-center gap-2">
        <Icon className={`h-4 w-4 shrink-0 ${config.color}`} />
        <code className="text-xs text-sc-fg-primary truncate flex-1">{displayText}</code>
        {createdAt && (
          <span className="text-[10px] text-sc-fg-subtle shrink-0">
            {formatDistanceToNow(createdAt, { addSuffix: true })}
          </span>
        )}
      </div>
      {approval.agent_name && (
        <p className="text-[10px] text-sc-fg-muted mt-0.5 ml-6 truncate">{approval.agent_name}</p>
      )}
    </>
  );

  // Build link to agent chat
  const agentLink = projectFilter
    ? `/agents?id=${approval.agent_id}&project=${projectFilter}`
    : `/agents?id=${approval.agent_id}`;

  return (
    <div className="group rounded-lg border border-sc-fg-subtle/30 bg-sc-bg-elevated/80 hover:bg-sc-bg-elevated hover:border-sc-purple/40 transition-all">
      {/* Clickable header */}
      <div className="flex items-start">
        {onAgentClick ? (
          <button
            type="button"
            onClick={() => onAgentClick(approval.agent_id)}
            className="flex-1 block px-3 py-2 min-w-0 text-left"
          >
            {headerContent}
          </button>
        ) : (
          <Link href={agentLink} className="flex-1 block px-3 py-2 min-w-0">
            {headerContent}
          </Link>
        )}
        {/* Dismiss button - for stale approvals */}
        <button
          type="button"
          onClick={() => onDismiss(approval.id)}
          disabled={isDismissing}
          className="p-2 text-sc-fg-muted hover:text-sc-fg-subtle opacity-0 group-hover:opacity-100 transition-opacity disabled:opacity-50"
          title="Dismiss (for stale approvals)"
        >
          <Xmark className="h-3 w-3" />
        </button>
      </div>

      {/* Action buttons */}
      <div className="flex border-t border-sc-fg-subtle/20">
        <button
          type="button"
          onClick={e => {
            e.preventDefault();
            onRespond(approval.id, 'approve');
          }}
          disabled={isResponding}
          className="flex-1 flex items-center justify-center gap-1.5 py-1.5 text-xs text-sc-green hover:bg-sc-green/10 transition-colors disabled:opacity-50 border-r border-sc-fg-subtle/20"
        >
          <Check className="h-3 w-3" />
          Allow
        </button>
        <button
          type="button"
          onClick={e => {
            e.preventDefault();
            onRespond(approval.id, 'deny');
          }}
          disabled={isResponding}
          className="flex-1 flex items-center justify-center gap-1.5 py-1.5 text-xs text-sc-red hover:bg-sc-red/10 transition-colors disabled:opacity-50"
        >
          <Xmark className="h-3 w-3" />
          Deny
        </button>
      </div>
    </div>
  );
});

// =============================================================================
// Approval Queue
// =============================================================================

interface ApprovalQueueProps {
  projectId?: string;
  agentId?: string;
  onAgentClick?: (id: string) => void;
  maxHeight?: string;
  className?: string;
}

export function ApprovalQueue({
  projectId,
  agentId,
  onAgentClick,
  maxHeight = '400px',
  className,
}: ApprovalQueueProps) {
  const searchParams = useSearchParams();
  const projectFilter = projectId || searchParams.get('project') || undefined;

  const { data, isLoading, error } = usePendingApprovals(projectFilter);
  const respondMutation = useRespondToApproval();
  const dismissMutation = useDismissApproval();

  // Client-side filter by agent when specified
  const approvals = useMemo(() => {
    const all = data?.approvals || [];
    if (!agentId) return all;
    return all.filter(a => a.agent_id === agentId);
  }, [data?.approvals, agentId]);

  const pendingCount = agentId ? approvals.length : data?.by_status?.pending || 0;

  const handleRespond = (id: string, action: 'approve' | 'deny') => {
    respondMutation.mutate({ id, request: { action } });
  };

  const handleDismiss = (id: string) => {
    dismissMutation.mutate(id);
  };

  if (isLoading) {
    return (
      <Section
        title="Approval Queue"
        icon={<Clock className="h-5 w-5 animate-pulse" />}
        className={className}
      >
        <div className="flex items-center justify-center py-6">
          <Spinner size="md" />
        </div>
      </Section>
    );
  }

  if (error) {
    return (
      <Section
        title="Approval Queue"
        icon={<WarningCircle className="h-5 w-5 text-sc-red" />}
        className={className}
      >
        <Card variant="error">
          <p className="text-xs text-sc-red">Error: {error.message}</p>
        </Card>
      </Section>
    );
  }

  if (approvals.length === 0) {
    return (
      <Section
        title="Approval Queue"
        icon={<Check className="h-5 w-5 text-sc-green" />}
        className={className}
      >
        <p className="text-xs text-sc-fg-subtle text-center py-4">No pending approvals</p>
      </Section>
    );
  }

  return (
    <Section
      title="Approval Queue"
      icon={<AlertTriangle className="h-5 w-5 text-sc-yellow" />}
      description="Actions requiring your approval before agents can proceed."
      actions={
        pendingCount > 0 && (
          <span className="px-1.5 py-0.5 text-[10px] font-medium rounded bg-sc-yellow/20 text-sc-yellow">
            {pendingCount}
          </span>
        )
      }
      className={className}
    >
      <div className="overflow-y-auto" style={{ maxHeight }}>
        <div className="space-y-2">
          {approvals.map(approval => (
            <ApprovalCard
              key={approval.id}
              approval={approval}
              onRespond={handleRespond}
              onDismiss={handleDismiss}
              isResponding={respondMutation.isPending}
              isDismissing={dismissMutation.isPending}
              projectFilter={projectFilter}
              onAgentClick={onAgentClick}
            />
          ))}
        </div>
      </div>
    </Section>
  );
}
