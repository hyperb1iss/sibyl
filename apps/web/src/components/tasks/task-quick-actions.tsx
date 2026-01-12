'use client';

import { EditableText } from '@/components/editable';
import { AlertCircle } from '@/components/ui/icons';
import type { TaskStatusType } from '@/lib/constants';

interface TaskQuickActionsProps {
  status: TaskStatusType;
  blockerReason: string | undefined;
  onUpdateField: (field: string, value: unknown) => Promise<void>;
}

/**
 * Contextual alerts for task status (e.g., blocker reason when blocked).
 * Status changes are handled via the status badge dropdown in TaskHeader.
 */
export function TaskQuickActions({
  status,
  blockerReason,
  onUpdateField,
}: TaskQuickActionsProps) {
  if (status !== 'blocked') return null;

  return (
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
  );
}
