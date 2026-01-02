'use client';

/**
 * ApprovalRequestMessage - Compact inline approval in agent chat.
 *
 * Sleek, minimal design that fits naturally in the message flow.
 */

import { formatDistanceToNow } from 'date-fns';
import { memo, useState } from 'react';

import {
  AlertTriangle,
  Check,
  ChevronDown,
  Clock,
  Code,
  InfoCircle,
  WarningCircle,
  Xmark,
} from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import type { ApprovalType } from '@/lib/api';
import { useRespondToApproval } from '@/lib/hooks';

// Simplified type config with accent colors
const TYPE_CONFIG: Record<
  ApprovalType,
  { icon: typeof Code; accent: string; bg: string; border: string }
> = {
  destructive_command: {
    icon: AlertTriangle,
    accent: 'text-sc-red',
    bg: 'bg-sc-red/8',
    border: 'border-sc-red/25',
  },
  sensitive_file: {
    icon: WarningCircle,
    accent: 'text-sc-yellow',
    bg: 'bg-sc-yellow/8',
    border: 'border-sc-yellow/25',
  },
  file_write: {
    icon: Code,
    accent: 'text-sc-purple',
    bg: 'bg-sc-purple/8',
    border: 'border-sc-purple/25',
  },
  external_api: {
    icon: Code,
    accent: 'text-sc-cyan',
    bg: 'bg-sc-cyan/8',
    border: 'border-sc-cyan/25',
  },
  cost_threshold: {
    icon: AlertTriangle,
    accent: 'text-sc-yellow',
    bg: 'bg-sc-yellow/8',
    border: 'border-sc-yellow/25',
  },
  review_phase: {
    icon: Check,
    accent: 'text-sc-purple',
    bg: 'bg-sc-purple/8',
    border: 'border-sc-purple/25',
  },
  question: {
    icon: InfoCircle,
    accent: 'text-sc-cyan',
    bg: 'bg-sc-cyan/8',
    border: 'border-sc-cyan/25',
  },
  scope_change: {
    icon: AlertTriangle,
    accent: 'text-sc-yellow',
    bg: 'bg-sc-yellow/8',
    border: 'border-sc-yellow/25',
  },
  merge_conflict: {
    icon: WarningCircle,
    accent: 'text-sc-red',
    bg: 'bg-sc-red/8',
    border: 'border-sc-red/25',
  },
  test_failure: {
    icon: WarningCircle,
    accent: 'text-sc-red',
    bg: 'bg-sc-red/8',
    border: 'border-sc-red/25',
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

/** Get short display text from metadata */
function getDisplayText(metadata?: ApprovalRequestMessageProps['metadata']): string | null {
  if (!metadata) return null;
  if (metadata.file_path) {
    // Show just filename or last 2 segments
    const parts = metadata.file_path.split('/');
    return parts.length > 2 ? `.../${parts.slice(-2).join('/')}` : metadata.file_path;
  }
  if (metadata.command) {
    return metadata.command.length > 50 ? `${metadata.command.slice(0, 47)}...` : metadata.command;
  }
  if (metadata.url) {
    try {
      return new URL(metadata.url).hostname;
    } catch {
      return metadata.url.slice(0, 40);
    }
  }
  return null;
}

/** Generate permission pattern for always-allow */
function generatePermissionPattern(
  approvalType: ApprovalType,
  metadata?: ApprovalRequestMessageProps['metadata']
): string | null {
  if (metadata?.file_path) {
    const toolName = metadata.tool_name || 'Write';
    const dirPath = metadata.file_path.substring(0, metadata.file_path.lastIndexOf('/'));
    return `${toolName}(${dirPath}/**)`;
  }
  if (metadata?.command && approvalType === 'destructive_command') {
    const baseCmd = metadata.command.trim().split(/\s+/)[0];
    return `Bash(${baseCmd}:*)`;
  }
  if (metadata?.url) {
    try {
      return `WebFetch(domain:${new URL(metadata.url).hostname})`;
    } catch {
      /* fall through */
    }
  }
  if (metadata?.tool_name) return `${metadata.tool_name}(*)`;
  return null;
}

export const ApprovalRequestMessage = memo(function ApprovalRequestMessage({
  approvalId,
  approvalType,
  metadata,
  expiresAt,
  status = 'pending',
}: ApprovalRequestMessageProps) {
  const respondMutation = useRespondToApproval();
  const [showDetails, setShowDetails] = useState(false);

  const config = TYPE_CONFIG[approvalType] || TYPE_CONFIG.file_write;
  const Icon = config.icon;
  const displayText = getDisplayText(metadata);
  const permissionPattern = generatePermissionPattern(approvalType, metadata);

  const isPending = status === 'pending';
  const isExpiredTime = expiresAt && new Date(expiresAt) < new Date();
  const isResolved = !isPending || isExpiredTime;

  const handleAction = (action: 'approve' | 'deny') => {
    respondMutation.mutate({ id: approvalId, request: { action } });
  };

  // Resolved state - ultra compact
  if (isResolved) {
    const resolvedColor =
      status === 'approved'
        ? 'text-sc-green'
        : status === 'denied'
          ? 'text-sc-red'
          : 'text-sc-fg-muted';
    const resolvedBg =
      status === 'approved'
        ? 'bg-sc-green/5 border-sc-green/20'
        : status === 'denied'
          ? 'bg-sc-red/5 border-sc-red/20'
          : 'bg-sc-bg-elevated border-sc-fg-subtle/30';
    const resolvedIcon = status === 'approved' ? Check : status === 'denied' ? Xmark : Clock;
    const ResolvedIcon = resolvedIcon;

    return (
      <div
        className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md border text-xs font-mono ${resolvedBg}`}
      >
        <ResolvedIcon className={`h-3 w-3 ${resolvedColor}`} />
        <span className={resolvedColor}>
          {status === 'approved' ? 'Allowed' : status === 'denied' ? 'Denied' : 'Expired'}
        </span>
        {displayText && (
          <span className="text-sc-fg-muted truncate max-w-[200px]">{displayText}</span>
        )}
      </div>
    );
  }

  // Pending state - action required
  return (
    <div className={`max-w-sm rounded-lg border ${config.border} ${config.bg} overflow-hidden`}>
      {/* Main row - icon, text, actions */}
      <div className="flex items-center gap-2 px-3 py-2">
        <Icon className={`h-4 w-4 shrink-0 ${config.accent}`} />

        <div className="flex-1 min-w-0">
          {displayText ? (
            <code className="text-xs text-sc-fg-primary truncate block">{displayText}</code>
          ) : (
            <span className="text-xs text-sc-fg-muted">Approval required</span>
          )}
        </div>

        {/* Inline action buttons */}
        <div className="flex items-center gap-1 shrink-0">
          <button
            type="button"
            onClick={() => handleAction('approve')}
            disabled={respondMutation.isPending}
            className="p-1.5 rounded-md bg-sc-green/15 hover:bg-sc-green/25 text-sc-green transition-colors disabled:opacity-50"
            title="Allow"
          >
            {respondMutation.isPending ? <Spinner size="sm" /> : <Check className="h-3.5 w-3.5" />}
          </button>
          <button
            type="button"
            onClick={() => handleAction('deny')}
            disabled={respondMutation.isPending}
            className="p-1.5 rounded-md bg-sc-red/10 hover:bg-sc-red/20 text-sc-red transition-colors disabled:opacity-50"
            title="Deny"
          >
            <Xmark className="h-3.5 w-3.5" />
          </button>
          {permissionPattern && (
            <button
              type="button"
              onClick={() => setShowDetails(!showDetails)}
              className="p-1.5 rounded-md hover:bg-sc-fg-subtle/10 text-sc-fg-muted transition-colors"
              title="More options"
            >
              <ChevronDown
                className={`h-3.5 w-3.5 transition-transform ${showDetails ? 'rotate-180' : ''}`}
              />
            </button>
          )}
        </div>
      </div>

      {/* Expandable details */}
      {showDetails && (
        <div className="px-3 pb-2 pt-1 border-t border-current/10 space-y-2">
          {/* Full command/path if truncated */}
          {metadata?.command && metadata.command.length > 50 && (
            <pre className="text-[11px] text-sc-coral bg-sc-bg-dark/50 p-1.5 rounded overflow-x-auto">
              $ {metadata.command}
            </pre>
          )}
          {metadata?.file_path && (
            <div className="text-[11px] text-sc-cyan font-mono">{metadata.file_path}</div>
          )}

          {/* Always allow option */}
          {permissionPattern && (
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => {
                  handleAction('approve');
                  // TODO: Save pattern
                }}
                disabled={respondMutation.isPending}
                className="text-[11px] text-sc-purple hover:text-sc-purple/80 transition-colors"
              >
                Always allow <code className="bg-sc-bg-dark px-1 rounded">{permissionPattern}</code>
              </button>
            </div>
          )}

          {/* Expiry */}
          {expiresAt && (
            <div className="flex items-center gap-1 text-[10px] text-sc-fg-subtle">
              <Clock className="h-3 w-3" />
              {formatDistanceToNow(new Date(expiresAt), { addSuffix: true })}
            </div>
          )}
        </div>
      )}
    </div>
  );
});
