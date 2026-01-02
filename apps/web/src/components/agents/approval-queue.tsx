'use client';

/**
 * Approval Queue - Human-in-the-loop agent coordination.
 *
 * Displays pending approvals that require human action before agents can proceed.
 * Supports approve, deny, and edit actions with optional response messages.
 */

import { formatDistanceToNow } from 'date-fns';
import { memo, useState } from 'react';

import { Button, IconButton } from '@/components/ui/button';
import { Card, Section } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  AlertTriangle,
  Check,
  Clock,
  Code,
  InfoCircle,
  MoreHoriz,
  WarningCircle,
  Xmark,
} from '@/components/ui/icons';
import { Textarea } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import type { Approval, ApprovalType } from '@/lib/api';
import { TASK_PRIORITY_CONFIG, type TaskPriorityType } from '@/lib/constants';
import { usePendingApprovals, useRespondToApproval } from '@/lib/hooks';

// =============================================================================
// Type Icons & Colors
// =============================================================================

const TYPE_CONFIG: Record<ApprovalType, { icon: typeof Code; label: string; colorClass: string }> =
  {
    destructive_command: {
      icon: AlertTriangle,
      label: 'Destructive Command',
      colorClass: 'text-sc-red',
    },
    sensitive_file: {
      icon: WarningCircle,
      label: 'Sensitive File',
      colorClass: 'text-sc-yellow',
    },
    file_write: {
      icon: WarningCircle,
      label: 'File Write',
      colorClass: 'text-sc-yellow',
    },
    external_api: {
      icon: Code,
      label: 'External API',
      colorClass: 'text-sc-cyan',
    },
    cost_threshold: {
      icon: AlertTriangle,
      label: 'Cost Threshold',
      colorClass: 'text-sc-yellow',
    },
    review_phase: {
      icon: Check,
      label: 'Review Phase',
      colorClass: 'text-sc-purple',
    },
    question: {
      icon: InfoCircle,
      label: 'Question',
      colorClass: 'text-sc-cyan',
    },
    scope_change: {
      icon: AlertTriangle,
      label: 'Scope Change',
      colorClass: 'text-sc-yellow',
    },
    merge_conflict: {
      icon: WarningCircle,
      label: 'Merge Conflict',
      colorClass: 'text-sc-red',
    },
    test_failure: {
      icon: WarningCircle,
      label: 'Test Failure',
      colorClass: 'text-sc-red',
    },
  };

// Priority styles use shared TASK_PRIORITY_CONFIG from constants

// =============================================================================
// Approval Card Component
// =============================================================================

interface ApprovalCardProps {
  approval: Approval;
  onRespond: (id: string, action: 'approve' | 'deny' | 'edit', message?: string) => void;
  isResponding: boolean;
}

const ApprovalCard = memo(function ApprovalCard({
  approval,
  onRespond,
  isResponding,
}: ApprovalCardProps) {
  const [showResponseDialog, setShowResponseDialog] = useState(false);
  const [responseAction, setResponseAction] = useState<'approve' | 'deny' | 'edit'>('approve');
  const [responseMessage, setResponseMessage] = useState('');

  const typeConfig = TYPE_CONFIG[approval.approval_type] || {
    icon: InfoCircle,
    label: approval.approval_type,
    colorClass: 'text-sc-fg-muted',
  };
  const priorityConfig =
    TASK_PRIORITY_CONFIG[approval.priority as TaskPriorityType] ?? TASK_PRIORITY_CONFIG.medium;
  const TypeIcon = typeConfig.icon;

  const handleQuickAction = (action: 'approve' | 'deny') => {
    onRespond(approval.id, action);
  };

  const handleDetailedResponse = () => {
    onRespond(approval.id, responseAction, responseMessage || undefined);
    setShowResponseDialog(false);
    setResponseMessage('');
  };

  const openResponseDialog = (action: 'approve' | 'deny' | 'edit') => {
    setResponseAction(action);
    setShowResponseDialog(true);
  };

  const createdAt = approval.created_at ? new Date(approval.created_at) : null;
  const expiresAt = approval.expires_at ? new Date(approval.expires_at) : null;

  return (
    <>
      <Card
        variant={
          approval.priority === 'critical' || approval.priority === 'high' ? 'warning' : 'default'
        }
        className="relative"
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="flex items-center gap-2 min-w-0">
            <TypeIcon className={`h-5 w-5 flex-shrink-0 ${typeConfig.colorClass}`} />
            <h3 className="text-base font-semibold text-sc-fg-primary truncate">
              {approval.title}
            </h3>
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            <span
              className={`px-2 py-0.5 text-xs font-medium rounded-full border ${priorityConfig.bgClass} ${priorityConfig.textClass} ${priorityConfig.borderClass}`}
            >
              {approval.priority}
            </span>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <IconButton
                  icon={<MoreHoriz className="h-4 w-4" />}
                  label="More actions"
                  size="sm"
                />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onClick={() => openResponseDialog('approve')}>
                  <Check className="mr-2 h-4 w-4 text-sc-green" />
                  Approve with message
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => openResponseDialog('deny')} destructive>
                  <Xmark className="mr-2 h-4 w-4" />
                  Deny with message
                </DropdownMenuItem>
                {approval.actions.includes('edit') && (
                  <DropdownMenuItem onClick={() => openResponseDialog('edit')}>
                    <Code className="mr-2 h-4 w-4 text-sc-cyan" />
                    Edit response
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>

        {/* Meta info */}
        <p className="text-xs text-sc-fg-muted mb-2">
          {approval.agent_name || 'Agent'} • {typeConfig.label}
        </p>

        {/* Summary */}
        <p className="text-sm text-sc-fg-muted mb-3">{approval.summary}</p>

        {/* Timestamps */}
        <div className="flex items-center gap-4 text-xs text-sc-fg-subtle mb-4">
          {createdAt && (
            <span className="flex items-center gap-1">
              <Clock className="h-3 w-3" />
              {formatDistanceToNow(createdAt, { addSuffix: true })}
            </span>
          )}
          {expiresAt && (
            <span className="flex items-center gap-1 text-sc-yellow">
              <AlertTriangle className="h-3 w-3" />
              Expires {formatDistanceToNow(expiresAt, { addSuffix: true })}
            </span>
          )}
        </div>

        {/* Actions */}
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="primary"
            onClick={() => handleQuickAction('approve')}
            disabled={isResponding}
            className="flex-1 bg-sc-green hover:bg-sc-green/80"
            icon={<Check className="h-4 w-4" />}
          >
            Approve
          </Button>
          <Button
            size="sm"
            variant="danger"
            onClick={() => handleQuickAction('deny')}
            disabled={isResponding}
            className="flex-1"
            icon={<Xmark className="h-4 w-4" />}
          >
            Deny
          </Button>
        </div>
      </Card>

      <Dialog open={showResponseDialog} onOpenChange={setShowResponseDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {responseAction === 'approve' && 'Approve Request'}
              {responseAction === 'deny' && 'Deny Request'}
              {responseAction === 'edit' && 'Edit Response'}
            </DialogTitle>
            <DialogDescription>Add an optional message to explain your decision.</DialogDescription>
          </DialogHeader>
          <div className="py-4">
            <Textarea
              placeholder="Optional message..."
              value={responseMessage}
              onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
                setResponseMessage(e.target.value)
              }
              rows={4}
            />
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setShowResponseDialog(false)}>
              Cancel
            </Button>
            <Button
              variant={responseAction === 'deny' ? 'danger' : 'primary'}
              onClick={handleDetailedResponse}
              disabled={isResponding}
            >
              {responseAction === 'approve' && 'Approve'}
              {responseAction === 'deny' && 'Deny'}
              {responseAction === 'edit' && 'Submit Edit'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
});

// =============================================================================
// Approval Queue Component
// =============================================================================

interface ApprovalQueueProps {
  projectId?: string;
  maxHeight?: string;
  className?: string;
}

export function ApprovalQueue({ projectId, maxHeight = '400px', className }: ApprovalQueueProps) {
  const { data, isLoading, error } = usePendingApprovals(projectId);
  const respondMutation = useRespondToApproval();

  const handleRespond = (id: string, action: 'approve' | 'deny' | 'edit', message?: string) => {
    respondMutation.mutate({
      id,
      request: { action, message },
    });
  };

  if (isLoading) {
    return (
      <Section
        title="Approval Queue"
        icon={<Clock className="h-5 w-5 animate-pulse" />}
        className={className}
      >
        <div className="flex items-center justify-center py-8">
          <Spinner size="lg" />
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
          <p className="text-sm text-sc-red">Error loading approvals: {error.message}</p>
        </Card>
      </Section>
    );
  }

  const approvals = data?.approvals || [];
  const pendingCount = data?.by_status?.pending || 0;

  if (approvals.length === 0) {
    return (
      <Section
        title="Approval Queue"
        icon={<Check className="h-5 w-5 text-sc-green" />}
        description="No pending approvals. All agents are running smoothly."
        className={className}
      >
        <div className="text-center py-4 text-sc-fg-subtle">All clear</div>
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
          <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-sc-yellow/20 text-sc-yellow border border-sc-yellow/40">
            {pendingCount}
          </span>
        )
      }
      className={className}
    >
      <div className="overflow-y-auto pr-1" style={{ maxHeight }}>
        <div className="space-y-3">
          {approvals.map(approval => (
            <ApprovalCard
              key={approval.id}
              approval={approval}
              onRespond={handleRespond}
              isResponding={respondMutation.isPending}
            />
          ))}
        </div>
      </div>
    </Section>
  );
}
