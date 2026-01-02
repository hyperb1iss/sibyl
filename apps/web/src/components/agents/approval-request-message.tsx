'use client';

/**
 * ApprovalRequestMessage - Inline approval request in agent chat.
 *
 * Compact version of approval UI that appears in the chat thread when
 * an agent requests human approval for a dangerous operation.
 */

import { formatDistanceToNow } from 'date-fns';
import { memo, useState } from 'react';

import { Button } from '@/components/ui/button';
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
import type { ApprovalType } from '@/lib/api';
import { useRespondToApproval } from '@/lib/hooks';

// Type config matches approval-queue.tsx
const TYPE_CONFIG: Record<ApprovalType, { icon: typeof Code; label: string; colorClass: string }> =
  {
    destructive_command: {
      icon: AlertTriangle,
      label: 'Destructive',
      colorClass: 'text-sc-red',
    },
    sensitive_file: {
      icon: WarningCircle,
      label: 'Sensitive',
      colorClass: 'text-sc-yellow',
    },
    file_write: {
      icon: Code,
      label: 'Write',
      colorClass: 'text-sc-cyan',
    },
    external_api: {
      icon: Code,
      label: 'API',
      colorClass: 'text-sc-cyan',
    },
    cost_threshold: {
      icon: AlertTriangle,
      label: 'Cost',
      colorClass: 'text-sc-yellow',
    },
    review_phase: {
      icon: Check,
      label: 'Review',
      colorClass: 'text-sc-purple',
    },
    question: {
      icon: InfoCircle,
      label: 'Question',
      colorClass: 'text-sc-cyan',
    },
    scope_change: {
      icon: AlertTriangle,
      label: 'Scope',
      colorClass: 'text-sc-yellow',
    },
    merge_conflict: {
      icon: WarningCircle,
      label: 'Conflict',
      colorClass: 'text-sc-red',
    },
    test_failure: {
      icon: WarningCircle,
      label: 'Test Fail',
      colorClass: 'text-sc-red',
    },
  };

export interface ApprovalRequestMessageProps {
  approvalId: string;
  approvalType: ApprovalType;
  title: string;
  summary: string;
  metadata?: {
    command?: string;
    file_path?: string;
    tool_name?: string;
    tool_input?: Record<string, unknown>;
    url?: string;
    pattern_matched?: string;
  };
  expiresAt?: string;
  status?: 'pending' | 'approved' | 'denied' | 'expired';
}

/**
 * Generate a permission pattern for "Always Allow" functionality.
 * Matches Claude Code's pattern format: Tool(context:pattern)
 */
function generatePermissionPattern(
  approvalType: ApprovalType,
  metadata?: ApprovalRequestMessageProps['metadata']
): string | null {
  if (!metadata) return null;

  // For file operations, generate path-based pattern
  if (metadata.file_path) {
    const toolName = metadata.tool_name || 'Write';
    // Get directory path and add wildcard
    const dirPath = metadata.file_path.substring(0, metadata.file_path.lastIndexOf('/'));
    return `${toolName}(${dirPath}/**)`;
  }

  // For bash commands, generate command-based pattern
  if (metadata.command && approvalType === 'destructive_command') {
    // Extract the base command (first word)
    const baseCmd = metadata.command.trim().split(/\s+/)[0];
    return `Bash(${baseCmd}:*)`;
  }

  // For URLs, generate domain-based pattern
  if (metadata.url) {
    try {
      const url = new URL(metadata.url);
      return `WebFetch(domain:${url.hostname})`;
    } catch {
      return null;
    }
  }

  return null;
}

export const ApprovalRequestMessage = memo(function ApprovalRequestMessage({
  approvalId,
  approvalType,
  title,
  summary: _summary,
  metadata,
  expiresAt,
  status = 'pending',
}: ApprovalRequestMessageProps) {
  const respondMutation = useRespondToApproval();
  const [isExpanded, setIsExpanded] = useState(true);
  const [showAlwaysAllow, setShowAlwaysAllow] = useState(false);

  const typeConfig = TYPE_CONFIG[approvalType] || {
    icon: InfoCircle,
    label: approvalType,
    colorClass: 'text-sc-fg-muted',
  };
  const TypeIcon = typeConfig.icon;

  const permissionPattern = generatePermissionPattern(approvalType, metadata);

  const handleAction = (action: 'approve' | 'deny') => {
    respondMutation.mutate({
      id: approvalId,
      request: { action },
    });
  };

  const handleAlwaysAllow = () => {
    // TODO: Call API to save pattern to allowlist
    // For now, just approve and show pattern
    handleAction('approve');
    // In future: savePatterMutation.mutate({ pattern: permissionPattern });
  };

  const isPending = status === 'pending';
  const isExpiredTime = expiresAt && new Date(expiresAt) < new Date();
  const isResolved = !isPending || isExpiredTime;

  // Border/background colors based on status and type
  const statusStyles = isPending
    ? approvalType === 'destructive_command'
      ? 'border-sc-red/40 bg-sc-red/5'
      : 'border-sc-purple/40 bg-sc-purple/5'
    : status === 'approved'
      ? 'border-sc-green/30 bg-sc-green/5'
      : 'border-sc-red/30 bg-sc-red/5';

  return (
    <div className={`rounded-lg border p-3 transition-all duration-200 ${statusStyles}`}>
      {/* Header */}
      <button
        type="button"
        className="w-full flex items-center gap-2 text-left"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <TypeIcon className={`h-4 w-4 flex-shrink-0 ${typeConfig.colorClass}`} />
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded font-medium uppercase tracking-wide ${typeConfig.colorClass} bg-current/10`}
        >
          {typeConfig.label}
        </span>
        <span className="text-sm font-medium text-sc-fg-primary truncate flex-1">{title}</span>
        {/* Status indicator */}
        {isResolved && (
          <span
            className={`text-xs flex items-center gap-1 ${
              status === 'approved' ? 'text-sc-green' : 'text-sc-red'
            }`}
          >
            {status === 'approved' && (
              <>
                <Check className="h-3 w-3" /> Approved
              </>
            )}
            {status === 'denied' && (
              <>
                <Xmark className="h-3 w-3" /> Denied
              </>
            )}
            {isExpiredTime && status === 'pending' && (
              <>
                <Clock className="h-3 w-3" /> Expired
              </>
            )}
          </span>
        )}
      </button>

      {/* Expandable content */}
      {isExpanded && (
        <div className="mt-3 space-y-3">
          {/* Metadata (command, file path, URL) - primary content */}
          {metadata && (metadata.command || metadata.file_path || metadata.url) && (
            <div className="p-2 bg-sc-bg-dark/50 rounded text-xs font-mono space-y-1">
              {metadata.command && (
                <div className="text-sc-coral">
                  <span className="text-sc-fg-subtle select-none">$ </span>
                  {metadata.command}
                </div>
              )}
              {metadata.file_path && <div className="text-sc-cyan">{metadata.file_path}</div>}
              {metadata.url && <div className="text-sc-cyan">{metadata.url}</div>}
            </div>
          )}

          {/* Actions for pending approvals */}
          {isPending && !isExpiredTime && (
            <div className="space-y-2">
              {/* Main action buttons */}
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  onClick={() => handleAction('approve')}
                  disabled={respondMutation.isPending}
                  className="flex-1 bg-sc-green/20 hover:bg-sc-green/30 text-sc-green border border-sc-green/30"
                >
                  {respondMutation.isPending ? (
                    <Spinner size="sm" />
                  ) : (
                    <Check className="h-3.5 w-3.5 mr-1.5" />
                  )}
                  Allow
                </Button>
                <Button
                  size="sm"
                  onClick={() => handleAction('deny')}
                  disabled={respondMutation.isPending}
                  className="flex-1 bg-sc-red/10 hover:bg-sc-red/20 text-sc-red border border-sc-red/20"
                >
                  {respondMutation.isPending ? (
                    <Spinner size="sm" />
                  ) : (
                    <Xmark className="h-3.5 w-3.5 mr-1.5" />
                  )}
                  Deny
                </Button>
              </div>

              {/* Always Allow option */}
              {permissionPattern && (
                <div className="space-y-1.5">
                  {!showAlwaysAllow ? (
                    <button
                      type="button"
                      onClick={() => setShowAlwaysAllow(true)}
                      className="text-xs text-sc-fg-subtle hover:text-sc-cyan transition-colors"
                    >
                      Always allow...
                    </button>
                  ) : (
                    <div className="p-2 bg-sc-bg-dark/30 rounded border border-sc-purple/20 space-y-2">
                      <div className="text-xs text-sc-fg-muted">Allow all matching operations:</div>
                      <code className="block text-xs text-sc-purple bg-sc-bg-dark px-2 py-1 rounded">
                        {permissionPattern}
                      </code>
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          onClick={handleAlwaysAllow}
                          disabled={respondMutation.isPending}
                          className="flex-1 bg-sc-purple/20 hover:bg-sc-purple/30 text-sc-purple border border-sc-purple/30 text-xs"
                        >
                          <Check className="h-3 w-3 mr-1" />
                          Allow & Remember
                        </Button>
                        <Button
                          size="sm"
                          onClick={() => setShowAlwaysAllow(false)}
                          className="bg-transparent hover:bg-sc-bg-hover text-sc-fg-muted border border-sc-border text-xs"
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Expiry countdown */}
              {expiresAt && (
                <div className="flex items-center gap-1 text-[10px] text-sc-fg-subtle">
                  <Clock className="h-3 w-3" />
                  Expires {formatDistanceToNow(new Date(expiresAt), { addSuffix: true })}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
});
