'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { AlertCircle, Archive, Check, Download, Upload } from '@/components/ui/icons';
import { api, type BackupData, type BackupResponse } from '@/lib/api';

export default function DataSettingsPage() {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [restoreFile, setRestoreFile] = useState<BackupData | null>(null);
  const [restoreFileName, setRestoreFileName] = useState<string>('');
  const [skipExisting, setSkipExisting] = useState(true);

  // Backup mutation
  const backupMutation = useMutation({
    mutationFn: () => api.admin.backup(),
    onSuccess: (data: BackupResponse) => {
      if (data.success && data.backup_data) {
        // Download the backup as JSON file
        const blob = new Blob([JSON.stringify(data.backup_data, null, 2)], {
          type: 'application/json',
        });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `sibyl-backup-${new Date().toISOString().split('T')[0]}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      }
    },
  });

  // Restore mutation
  const restoreMutation = useMutation({
    mutationFn: (backupData: BackupData) => api.admin.restore(backupData, skipExisting),
    onSuccess: () => {
      // Invalidate all queries to refresh data
      queryClient.invalidateQueries();
      setRestoreFile(null);
      setRestoreFileName('');
    },
  });

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = event => {
      try {
        const data = JSON.parse(event.target?.result as string) as BackupData;
        // Validate it looks like a backup
        if (data.version && data.entities && data.relationships) {
          setRestoreFile(data);
          setRestoreFileName(file.name);
        } else {
          alert('Invalid backup file format');
        }
      } catch {
        alert('Failed to parse backup file');
      }
    };
    reader.readAsText(file);
  };

  return (
    <div className="space-y-8">
      {/* Backup Section */}
      <section className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <div className="flex items-start gap-4">
          <div className="p-3 rounded-lg bg-sc-cyan/10">
            <Download className="w-6 h-6 text-sc-cyan" />
          </div>
          <div className="flex-1">
            <h2 className="text-lg font-semibold text-sc-fg-primary mb-1">Backup Graph Data</h2>
            <p className="text-sm text-sc-fg-muted mb-4">
              Export all entities and relationships to a JSON file. This backup can be used to
              restore your data or migrate to another organization.
            </p>

            <Button
              variant="secondary"
              onClick={() => backupMutation.mutate()}
              loading={backupMutation.isPending}
              icon={<Download className="w-4 h-4" />}
            >
              {backupMutation.isPending ? 'Creating backup...' : 'Download Backup'}
            </Button>

            {backupMutation.isSuccess && (
              <div className="mt-4 flex items-center gap-2 text-sc-green text-sm">
                <Check className="w-4 h-4" />
                Backup downloaded successfully!
              </div>
            )}

            {backupMutation.isError && (
              <div className="mt-4 flex items-center gap-2 text-sc-red text-sm">
                <AlertCircle className="w-4 h-4" />
                {backupMutation.error instanceof Error
                  ? backupMutation.error.message
                  : 'Backup failed'}
              </div>
            )}
          </div>
        </div>
      </section>

      {/* Restore Section */}
      <section className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <div className="flex items-start gap-4">
          <div className="p-3 rounded-lg bg-sc-purple/10">
            <Upload className="w-6 h-6 text-sc-purple" />
          </div>
          <div className="flex-1">
            <h2 className="text-lg font-semibold text-sc-fg-primary mb-1">Restore from Backup</h2>
            <p className="text-sm text-sc-fg-muted mb-4">
              Import entities and relationships from a previously exported backup file. By default,
              existing entities are skipped to prevent duplicates.
            </p>

            {/* File Input */}
            <input
              ref={fileInputRef}
              type="file"
              accept=".json"
              onChange={handleFileSelect}
              className="hidden"
            />

            {!restoreFile ? (
              <Button
                variant="secondary"
                onClick={() => fileInputRef.current?.click()}
                icon={<Archive className="w-4 h-4" />}
              >
                Select Backup File
              </Button>
            ) : (
              <div className="space-y-4">
                {/* Selected File Info */}
                <div className="flex items-center gap-3 p-3 rounded-lg bg-sc-bg-highlight border border-sc-fg-subtle/10">
                  <Archive className="w-5 h-5 text-sc-coral" />
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-sc-fg-primary truncate">
                      {restoreFileName}
                    </p>
                    <p className="text-xs text-sc-fg-muted">
                      {restoreFile.entity_count} entities, {restoreFile.relationship_count}{' '}
                      relationships
                    </p>
                  </div>
                  <Button
                    variant="link"
                    onClick={() => {
                      setRestoreFile(null);
                      setRestoreFileName('');
                    }}
                  >
                    Change
                  </Button>
                </div>

                {/* Options */}
                <Checkbox
                  checked={skipExisting}
                  onCheckedChange={checked => setSkipExisting(checked === true)}
                  label="Skip existing entities (recommended)"
                />

                {/* Restore Button */}
                <Button
                  onClick={() => restoreMutation.mutate(restoreFile)}
                  loading={restoreMutation.isPending}
                  icon={<Upload className="w-4 h-4" />}
                >
                  {restoreMutation.isPending ? 'Restoring...' : 'Restore Backup'}
                </Button>
              </div>
            )}

            {/* Restore Result */}
            {restoreMutation.isSuccess && (
              <div className="mt-4 p-4 rounded-lg bg-sc-green/10 border border-sc-green/30">
                <div className="flex items-center gap-2 text-sc-green text-sm font-medium mb-2">
                  <Check className="w-4 h-4" />
                  Restore completed!
                </div>
                <div className="text-xs text-sc-fg-muted space-y-1">
                  <p>
                    Restored: {restoreMutation.data.entities_restored} entities,{' '}
                    {restoreMutation.data.relationships_restored} relationships
                  </p>
                  {(restoreMutation.data.entities_skipped > 0 ||
                    restoreMutation.data.relationships_skipped > 0) && (
                    <p>
                      Skipped: {restoreMutation.data.entities_skipped} entities,{' '}
                      {restoreMutation.data.relationships_skipped} relationships
                    </p>
                  )}
                  <p>Duration: {restoreMutation.data.duration_seconds.toFixed(2)}s</p>
                </div>
              </div>
            )}

            {restoreMutation.isError && (
              <div className="mt-4 flex items-center gap-2 text-sc-red text-sm">
                <AlertCircle className="w-4 h-4" />
                {restoreMutation.error instanceof Error
                  ? restoreMutation.error.message
                  : 'Restore failed'}
              </div>
            )}
          </div>
        </div>
      </section>

      {/* Info Section */}
      <section className="bg-sc-bg-base/50 rounded-lg border border-sc-fg-subtle/5 p-4">
        <h3 className="text-sm font-medium text-sc-fg-muted mb-2">About Backups</h3>
        <ul className="text-xs text-sc-fg-subtle space-y-1">
          <li>
            Backups include all entities (patterns, rules, tasks, etc.) and their relationships.
          </li>
          <li>Backup files are JSON format and can be opened in any text editor.</li>
          <li>
            Restoring to a different organization will import data into that org&apos;s graph.
          </li>
          <li>Large graphs may take longer to backup and restore.</li>
        </ul>
      </section>
    </div>
  );
}
