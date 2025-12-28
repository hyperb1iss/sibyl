'use client';

import { EditableText } from '@/components/editable';
import { AlertCircle, CheckCircle2, Pause, Play, RotateCcw, Send } from '@/components/ui/icons';
import type { TaskStatusType } from '@/lib/constants';

interface TaskQuickActionsProps {
  status: TaskStatusType;
  blockerReason: string | undefined;
  isUpdating: boolean;
  onStatusChange: (status: string) => Promise<void>;
  onUpdateField: (field: string, value: unknown) => Promise<void>;
}

/**
 * Blocker alert and status-dependent action buttons.
 */
export function TaskQuickActions({
  status,
  blockerReason,
  isUpdating,
  onStatusChange,
  onUpdateField,
}: TaskQuickActionsProps) {
  return (
    <>
      {/* Blocker Alert */}
      {status === 'blocked' && (
        <div className="mx-6 mb-4 p-4 bg-sc-red/10 border border-sc-red/30 rounded-xl">
          <div className="flex items-start gap-3">
            <AlertCircle width={20} height={20} className="text-sc-red shrink-0 mt-0.5" />
            <div className="flex-1">
              <span className="text-sm font-semibold text-sc-red">Blocked</span>
              <div className="text-sm text-sc-fg-muted mt-1">
                <EditableText
                  value={blockerReason || ''}
                  onSave={v => onUpdateField('blocker_reason', v || undefined)}
                  placeholder="What's blocking this task?"
                  multiline
                  rows={2}
                />
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Quick Actions */}
      <div className="px-6 pb-6">
        <div className="flex items-center gap-2 flex-wrap">
          {status === 'todo' && (
            <button
              type="button"
              onClick={() => onStatusChange('doing')}
              disabled={isUpdating}
              className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-purple text-white hover:bg-sc-purple/80 shadow-lg shadow-sc-purple/25 transition-all disabled:opacity-50"
            >
              <Play width={16} height={16} />
              Start Working
            </button>
          )}

          {status === 'doing' && (
            <>
              <button
                type="button"
                onClick={() => onStatusChange('review')}
                disabled={isUpdating}
                className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-purple text-white hover:bg-sc-purple/80 shadow-lg shadow-sc-purple/25 transition-all disabled:opacity-50"
              >
                <Send width={16} height={16} />
                Submit for Review
              </button>
              <button
                type="button"
                onClick={() => onStatusChange('blocked')}
                className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-bg-elevated border border-sc-fg-subtle/20 text-sc-red hover:border-sc-red/30 transition-all"
              >
                <Pause width={16} height={16} />
                Mark Blocked
              </button>
            </>
          )}

          {status === 'review' && (
            <button
              type="button"
              onClick={() => onStatusChange('done')}
              disabled={isUpdating}
              className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-green text-sc-bg-dark hover:bg-sc-green/80 shadow-lg shadow-sc-green/25 transition-all disabled:opacity-50"
            >
              <CheckCircle2 width={16} height={16} />
              Complete Task
            </button>
          )}

          {status === 'blocked' && (
            <button
              type="button"
              onClick={() => onStatusChange('doing')}
              disabled={isUpdating}
              className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-purple text-white hover:bg-sc-purple/80 shadow-lg shadow-sc-purple/25 transition-all disabled:opacity-50"
            >
              <Play width={16} height={16} />
              Unblock & Resume
            </button>
          )}

          {status === 'done' && (
            <button
              type="button"
              onClick={() => onStatusChange('todo')}
              disabled={isUpdating}
              className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-bg-elevated border border-sc-fg-subtle/20 text-sc-fg-muted hover:text-sc-fg-primary hover:border-sc-fg-subtle/40 transition-all disabled:opacity-50"
            >
              <RotateCcw width={16} height={16} />
              Reopen Task
            </button>
          )}
        </div>
      </div>
    </>
  );
}
