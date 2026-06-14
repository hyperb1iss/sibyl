'use client';

import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { SettingsPageHeader, SettingsSectionSkeleton } from '@/components/settings/primitives';
import { StatusBadge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Archive,
  Calendar,
  Check,
  Clock,
  Database,
  Download,
  Play,
  RefreshDouble,
  Trash,
} from '@/components/ui/icons';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Spinner } from '@/components/ui/spinner';
import { Switch } from '@/components/ui/switch';
import type { BackupInfo, BackupStatus } from '@/lib/api';
import { api } from '@/lib/api';
import {
  useBackupSettings,
  useBackups,
  useCreateBackup,
  useDeleteBackup,
  useUpdateBackupSettings,
} from '@/lib/hooks';

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / k ** i).toFixed(1))} ${sizes[i]}`;
}

function formatDate(dateString: string | null): string {
  if (!dateString) return 'Never';
  const date = new Date(dateString);
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}

// Human-friendly schedule options
const SCHEDULE_OPTIONS = [
  { label: 'Daily at 2 AM', value: '0 2 * * *' },
  { label: 'Daily at 6 AM', value: '0 6 * * *' },
  { label: 'Daily at midnight', value: '0 0 * * *' },
  { label: 'Every 12 hours', value: '0 */12 * * *' },
  { label: 'Every 6 hours', value: '0 */6 * * *' },
  { label: 'Weekly (Sunday 2 AM)', value: '0 2 * * 0' },
  { label: 'Weekly (Monday 2 AM)', value: '0 2 * * 1' },
];

const RETENTION_OPTIONS = [
  { label: '7 days', value: 7 },
  { label: '14 days', value: 14 },
  { label: '30 days', value: 30 },
  { label: '60 days', value: 60 },
  { label: '90 days', value: 90 },
  { label: '180 days', value: 180 },
  { label: '365 days', value: 365 },
];

function backupStatusBadgeProps(status: BackupStatus): {
  status: 'warning' | 'running' | 'healthy' | 'unhealthy';
  label: string;
  pulse?: boolean;
} {
  if (status === 'pending') {
    return { status: 'warning', label: 'Pending' };
  }
  if (status === 'in_progress') {
    return { status: 'running', label: 'In Progress', pulse: true };
  }
  if (status === 'completed') {
    return { status: 'healthy', label: 'Completed' };
  }
  return { status: 'unhealthy', label: 'Failed' };
}

function BackupRow({
  backup,
  onDelete,
  isDeleting,
}: {
  backup: BackupInfo;
  onDelete: (id: string) => void;
  isDeleting: boolean;
}) {
  const handleDownload = () => {
    if (backup.status === 'completed' && backup.filename) {
      window.open(api.backups.download(backup.backup_id), '_blank');
    }
  };

  return (
    <div className="flex items-center justify-between p-4 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10">
      <div className="flex items-center gap-4 flex-1 min-w-0">
        <Archive width={20} height={20} className="text-sc-coral flex-shrink-0" />
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium text-sc-fg-primary truncate">{backup.backup_id}</p>
          <div className="flex items-center gap-3 text-xs text-sc-fg-muted mt-1">
            <span>{formatDate(backup.created_at)}</span>
            {backup.status === 'completed' && (
              <>
                <span className="text-sc-fg-subtle">|</span>
                <span>{formatBytes(backup.size_bytes)}</span>
                <span className="text-sc-fg-subtle">|</span>
                <span>{backup.entity_count} entities</span>
                <span className="text-sc-fg-subtle">|</span>
                <span>{backup.relationship_count} relationships</span>
              </>
            )}
            {backup.triggered_by && (
              <>
                <span className="text-sc-fg-subtle">|</span>
                <span className="capitalize">{backup.triggered_by}</span>
              </>
            )}
          </div>
          {backup.error && <p className="text-xs text-sc-red mt-1 truncate">{backup.error}</p>}
        </div>
      </div>
      <div className="flex items-center gap-3">
        <StatusBadge {...backupStatusBadgeProps(backup.status)} variant="chip" />
        {backup.status === 'completed' && (
          <button
            type="button"
            onClick={handleDownload}
            className="p-2 rounded-lg text-sc-fg-muted transition-colors duration-200 hover:bg-sc-bg-highlight hover:text-sc-cyan focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
            aria-label={`Download backup ${backup.backup_id}`}
            title="Download backup"
          >
            <Download width={16} height={16} />
          </button>
        )}
        <button
          type="button"
          onClick={() => onDelete(backup.backup_id)}
          disabled={isDeleting || backup.status === 'in_progress'}
          className="p-2 rounded-lg text-sc-fg-muted transition-colors duration-200 hover:bg-sc-bg-highlight hover:text-sc-red focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated disabled:opacity-50"
          aria-label={`Delete backup ${backup.backup_id}`}
          title="Delete backup"
        >
          {isDeleting ? <Spinner size="xs" /> : <Trash width={16} height={16} />}
        </button>
      </div>
    </div>
  );
}

function Toggle({
  enabled,
  onChange,
  disabled,
  label,
}: {
  enabled: boolean;
  onChange: () => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <Switch checked={enabled} onCheckedChange={onChange} disabled={disabled} aria-label={label} />
  );
}

// Create Backup Modal
function CreateBackupModal({
  isOpen,
  onClose,
  onConfirm,
  isPending,
  archiveContents,
  lastBackupSize,
}: {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void;
  isPending: boolean;
  archiveContents: string[];
  lastBackupSize?: number;
}) {
  const [step, setStep] = useState<'options' | 'running' | 'success'>('options');

  useEffect(() => {
    if (isOpen) {
      setStep('options');
    }
  }, [isOpen]);

  useEffect(() => {
    if (step === 'running' && !isPending) {
      setStep('success');
    }
  }, [step, isPending]);

  const handleConfirm = () => {
    setStep('running');
    onConfirm();
  };

  const archiveSummary = archiveContents.length > 0 ? archiveContents.join(', ') : 'runtime data';

  return (
    <Dialog
      open={isOpen}
      onOpenChange={open => {
        // Don't let an outside-click / Escape abandon an in-flight backup.
        if (!open && step !== 'running') onClose();
      }}
    >
      <DialogContent size="md" showClose={step !== 'running'} className="p-0 overflow-hidden">
        {/* Header */}
        <DialogHeader className="mb-0 px-6 py-4 border-b border-sc-fg-subtle/10">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-sc-purple/10">
              <Archive width={20} height={20} className="text-sc-purple" />
            </div>
            <div>
              <DialogTitle>Create Backup</DialogTitle>
              <DialogDescription className="text-xs">Full organization archive</DialogDescription>
            </div>
          </div>
        </DialogHeader>

        {/* Content */}
        <div className="px-6 py-5">
          {step === 'options' && (
            <div className="space-y-4">
              <div className="w-full p-4 rounded-lg border bg-sc-cyan/5 border-sc-cyan/30">
                <div className="flex items-start gap-3">
                  <div className="mt-0.5 w-5 h-5 rounded border-2 flex items-center justify-center bg-sc-cyan border-sc-cyan">
                    <Check width={12} height={12} className="text-sc-on-accent" />
                  </div>
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <Database width={16} height={16} className="text-sc-cyan" />
                      <span className="font-medium text-sc-fg-primary">Full Surreal Archive</span>
                    </div>
                    <p className="text-xs text-sc-fg-muted mt-1">
                      Auth, content, crawl data, API keys, settings, and the knowledge graph for
                      this organization.
                    </p>
                  </div>
                </div>
              </div>

              {/* Info */}
              <div className="flex items-start gap-2 p-3 rounded-lg bg-sc-bg-highlight/50 text-xs text-sc-fg-muted">
                <Clock width={14} height={14} className="flex-shrink-0 mt-0.5" />
                <div>
                  <p>Backups are compressed and include {archiveSummary}.</p>
                  {lastBackupSize && lastBackupSize > 0 && (
                    <p className="mt-1">
                      Last backup size:{' '}
                      <span className="text-sc-fg-secondary">{formatBytes(lastBackupSize)}</span>
                    </p>
                  )}
                </div>
              </div>
            </div>
          )}

          {step === 'running' && (
            <div className="py-8 text-center">
              <div className="relative mx-auto w-16 h-16 mb-4">
                <div className="absolute inset-0 rounded-full border-4 border-sc-purple/20" />
                <div className="absolute inset-0 rounded-full border-4 border-sc-purple border-t-transparent animate-spin" />
                <Archive
                  width={24}
                  height={24}
                  className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 text-sc-purple"
                />
              </div>
              <h3 className="text-lg font-semibold text-sc-fg-primary mb-1">Creating Backup</h3>
              <p className="text-sm text-sc-fg-muted">Exporting the full organization archive...</p>
              <div className="mt-4 flex items-center justify-center gap-4 text-xs text-sc-fg-muted">
                <span className="flex items-center gap-1.5">
                  <Database width={12} height={12} className="text-sc-cyan" />
                  Surreal
                </span>
                <span className="flex items-center gap-1.5">
                  <Archive width={12} height={12} className="text-sc-coral" />
                  Graph
                </span>
              </div>
            </div>
          )}

          {step === 'success' && (
            <div className="py-8 text-center">
              <div className="mx-auto w-16 h-16 rounded-full bg-sc-green/10 flex items-center justify-center mb-4">
                <Check width={32} height={32} className="text-sc-green" />
              </div>
              <h3 className="text-lg font-semibold text-sc-fg-primary mb-1">Backup Created</h3>
              <p className="text-sm text-sc-fg-muted">
                Your backup has been queued and will appear in the archives shortly.
              </p>
            </div>
          )}
        </div>

        {/* Footer */}
        <DialogFooter className="mt-0 px-6 py-4 border-t border-sc-fg-subtle/10">
          {step === 'options' && (
            <>
              <Button variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              <Button onClick={handleConfirm} icon={<Play width={14} height={14} />}>
                Start Backup
              </Button>
            </>
          )}
          {step === 'running' && <p className="text-xs text-sc-fg-muted">Please wait...</p>}
          {step === 'success' && (
            <Button variant="primary" onClick={onClose}>
              Done
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function BackupsPage() {
  const { data: settings, isLoading: settingsLoading } = useBackupSettings();
  const { data: backupsData, isLoading: backupsLoading, refetch: refetchBackups } = useBackups();
  const updateSettings = useUpdateBackupSettings();
  const createBackup = useCreateBackup();
  const deleteBackup = useDeleteBackup();

  const [isModalOpen, setIsModalOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  const isLoading = settingsLoading || backupsLoading;

  // Get last backup size for estimation
  const lastCompletedBackup = backupsData?.backups.find(b => b.status === 'completed');

  const handleCreateBackup = async () => {
    try {
      await createBackup.mutateAsync({});
      refetchBackups();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Failed to create backup');
    }
  };

  const handleConfirmDelete = async () => {
    if (!pendingDelete) return;
    try {
      await deleteBackup.mutateAsync(pendingDelete);
      toast.success('Backup deleted');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Failed to delete backup');
    } finally {
      setPendingDelete(null);
    }
  };

  const handleUpdateSetting = async (updates: {
    enabled?: boolean;
    schedule?: string;
    retention_days?: number;
  }) => {
    try {
      await updateSettings.mutateAsync(updates);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Failed to update backup settings');
    }
  };

  if (isLoading) {
    return (
      <div className="space-y-6">
        <SettingsPageHeader
          icon={Archive}
          iconColor="text-sc-coral"
          title="Backups"
          description="Schedule and manage data + graph archives."
        />
        <SettingsSectionSkeleton rows={4} rowHeight={64} />
        <SettingsSectionSkeleton rows={5} rowHeight={72} />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Create Backup Modal */}
      <CreateBackupModal
        isOpen={isModalOpen}
        onClose={() => setIsModalOpen(false)}
        onConfirm={handleCreateBackup}
        isPending={createBackup.isPending}
        archiveContents={settings?.archive_contents ?? []}
        lastBackupSize={lastCompletedBackup?.size_bytes}
      />

      <SettingsPageHeader
        icon={Archive}
        iconColor="text-sc-coral"
        title="Backups"
        description="Create and manage archives of your data and knowledge graph."
        actions={
          <Button
            variant="primary"
            size="sm"
            icon={<Play width={14} height={14} />}
            onClick={() => setIsModalOpen(true)}
          >
            Create backup
          </Button>
        }
      />

      {/* Scheduled Backup Configuration */}
      {settings && (
        <div className="bg-sc-bg-elevated shadow-card rounded-lg border border-sc-fg-subtle/10 p-6">
          <h3 className="font-semibold text-sc-fg-primary mb-4">Scheduled Backups</h3>
          <div className="space-y-4">
            {/* Enable Toggle */}
            <div className="flex items-center justify-between p-3 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10">
              <div className="flex items-center gap-3">
                <Clock width={18} height={18} className="text-sc-cyan" />
                <div>
                  <p className="text-sm font-medium text-sc-fg-primary">Auto Backup</p>
                  <p className="text-xs text-sc-fg-muted">
                    Automatically create backups on schedule
                  </p>
                </div>
              </div>
              <Toggle
                label="Enable automatic backups"
                enabled={settings.enabled}
                onChange={() => handleUpdateSetting({ enabled: !settings.enabled })}
                disabled={updateSettings.isPending}
              />
            </div>

            {/* Schedule */}
            <div className="flex items-center justify-between p-3 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10">
              <div className="flex items-center gap-3">
                <Calendar width={18} height={18} className="text-sc-coral" />
                <div>
                  <p className="text-sm font-medium text-sc-fg-primary">Schedule</p>
                  <p className="text-xs text-sc-fg-muted">When to run automatic backups</p>
                </div>
              </div>
              <Select
                value={settings.schedule}
                onValueChange={value => handleUpdateSetting({ schedule: value })}
                disabled={updateSettings.isPending}
              >
                <SelectTrigger className="w-auto min-w-[180px]" aria-label="Backup schedule">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SCHEDULE_OPTIONS.map(opt => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Retention */}
            <div className="flex items-center justify-between p-3 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10">
              <div className="flex items-center gap-3">
                <Archive width={18} height={18} className="text-sc-yellow" />
                <div>
                  <p className="text-sm font-medium text-sc-fg-primary">Retention</p>
                  <p className="text-xs text-sc-fg-muted">How long to keep old backups</p>
                </div>
              </div>
              <Select
                value={String(settings.retention_days)}
                onValueChange={value => handleUpdateSetting({ retention_days: Number(value) })}
                disabled={updateSettings.isPending}
              >
                <SelectTrigger className="w-auto min-w-[140px]" aria-label="Backup retention">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {RETENTION_OPTIONS.map(opt => (
                    <SelectItem key={opt.value} value={String(opt.value)}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Last Backup */}
            <div className="flex items-center justify-between p-3 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10">
              <div className="flex items-center gap-3">
                <Check width={18} height={18} className="text-sc-green" />
                <div>
                  <p className="text-sm font-medium text-sc-fg-primary">Last Backup</p>
                  <p className="text-xs text-sc-fg-muted">Most recent completed backup</p>
                </div>
              </div>
              <span className="text-sm text-sc-fg-muted">
                {formatDate(settings.last_backup_at)}
              </span>
            </div>

            {/* Contents */}
            <div className="pt-4 border-t border-sc-fg-subtle/10">
              <p className="text-xs text-sc-fg-muted uppercase tracking-wide mb-3">
                Backup Contents
              </p>
              <div className="flex items-center justify-between p-3 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10">
                <div className="flex items-center gap-3">
                  <Database width={18} height={18} className="text-sc-cyan" />
                  <div>
                    <p className="text-sm font-medium text-sc-fg-primary">Full Organization</p>
                    <p className="text-xs text-sc-fg-muted">
                      {(settings.archive_contents.length > 0
                        ? settings.archive_contents
                        : ['auth.json', 'content.json', 'graph.json', 'metadata.json']
                      ).join(', ')}
                    </p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Backup List */}
      <div className="bg-sc-bg-elevated shadow-card rounded-lg border border-sc-fg-subtle/10 p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-semibold text-sc-fg-primary">Archives ({backupsData?.total ?? 0})</h3>
          <button
            type="button"
            onClick={() => refetchBackups()}
            aria-label="Refresh backups list"
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-sc-bg-highlight border border-sc-fg-subtle/20 text-sm text-sc-fg-secondary transition-colors duration-200 hover:bg-sc-bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
          >
            <RefreshDouble width={14} height={14} />
            Refresh
          </button>
        </div>

        {backupsData?.backups.length === 0 ? (
          <div className="text-center py-8 text-sc-fg-muted">
            <Archive width={40} height={40} className="mx-auto mb-3 opacity-50" />
            <p>No backups yet</p>
            <p className="text-sm mt-1">
              Create your first backup to protect your knowledge graph.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {backupsData?.backups.map(backup => (
              <BackupRow
                key={backup.id}
                backup={backup}
                onDelete={setPendingDelete}
                isDeleting={deleteBackup.isPending}
              />
            ))}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={!!pendingDelete}
        onOpenChange={open => {
          if (!open) setPendingDelete(null);
        }}
        title="Delete backup?"
        description={
          pendingDelete
            ? `Backup "${pendingDelete}" will be permanently removed. This cannot be undone.`
            : undefined
        }
        confirmLabel="Delete Backup"
        variant="danger"
        loading={deleteBackup.isPending}
        onConfirm={handleConfirmDelete}
      />
    </div>
  );
}
