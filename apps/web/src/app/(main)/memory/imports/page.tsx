'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { type FormEvent, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { useSetBreadcrumb } from '@/components/layout/breadcrumb';
import { PageHeader } from '@/components/layout/page-header';
import { SourceImportProgress } from '@/components/memory/source-import-progress';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { EnhancedEmptyState } from '@/components/ui/empty-state';
import { FormField } from '@/components/ui/form-field';
import { Database, RefreshCw, StopCircle, Upload } from '@/components/ui/icons';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { LoadingState } from '@/components/ui/spinner';
import { ErrorState } from '@/components/ui/tooltip';
import type { MemoryScope } from '@/lib/api';
import {
  useCancelSourceImport,
  useMemorySourceImport,
  useResumeSourceImport,
  useSourceImportAdapters,
  useStartSourceImport,
} from '@/lib/hooks';

const MEMORY_SCOPES: Array<{ value: MemoryScope; label: string }> = [
  { value: 'private', label: 'Private' },
  { value: 'delegated', label: 'Delegated' },
  { value: 'project', label: 'Project' },
  { value: 'team', label: 'Team' },
  { value: 'organization', label: 'Organization' },
  { value: 'shared', label: 'Shared' },
  { value: 'public', label: 'Public' },
];

function normalizeImportId(value: string | null): string {
  return value?.trim() ?? '';
}

function parseBatchSize(value: string): number | undefined {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? Math.min(parsed, 1000) : undefined;
}

export default function MemoryImportsPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const importId = normalizeImportId(searchParams.get('id'));

  useSetBreadcrumb([
    { label: 'Home', href: '/' },
    { label: 'Memory', href: '/memory', icon: Database },
    { label: 'Imports', icon: Upload },
  ]);

  const adaptersQuery = useSourceImportAdapters();
  const statusQuery = useMemorySourceImport(importId, { enabled: Boolean(importId) });
  const startImport = useStartSourceImport();
  const resumeImport = useResumeSourceImport();
  const cancelImport = useCancelSourceImport();

  const [sourceUri, setSourceUri] = useState('');
  const [adapterName, setAdapterName] = useState('mbox');
  const [memoryScope, setMemoryScope] = useState<MemoryScope>('private');
  const [scopeKey, setScopeKey] = useState('');
  const [batchSize, setBatchSize] = useState('100');
  const [promotionPreviewApproved, setPromotionPreviewApproved] = useState(false);

  const adapters = adaptersQuery.data?.adapters ?? [];
  const selectedAdapter = useMemo(
    () => adapters.find(adapter => adapter.name === adapterName) ?? null,
    [adapterName, adapters]
  );

  useEffect(() => {
    if (adapters.length > 0 && !adapters.some(adapter => adapter.name === adapterName)) {
      setAdapterName(adapters[0].name);
    }
  }, [adapterName, adapters]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedSourceUri = sourceUri.trim();
    if (!trimmedSourceUri) {
      toast.error('Source URI is required');
      return;
    }

    try {
      const status = await startImport.mutateAsync({
        source_uri: trimmedSourceUri,
        adapter_name: adapterName.trim() || undefined,
        target_memory_scope: memoryScope,
        target_scope_key: scopeKey.trim() || null,
        batch_size: parseBatchSize(batchSize),
        promotion_preview_approved: promotionPreviewApproved,
      });
      toast.success('Import started');
      router.replace(`/memory/imports?id=${encodeURIComponent(status.import_id)}`, {
        scroll: false,
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Failed to start import');
    }
  }

  async function handleResume() {
    if (!importId) return;
    try {
      await resumeImport.mutateAsync({
        importId,
        request: {
          batch_size: parseBatchSize(batchSize) ?? null,
          promotion_preview_approved: promotionPreviewApproved,
        },
      });
      toast.success('Import resumed');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Failed to resume import');
    }
  }

  async function handleCancel() {
    if (!importId) return;
    try {
      await cancelImport.mutateAsync(importId);
      toast.success('Import canceled');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Failed to cancel import');
    }
  }

  return (
    <div className="space-y-4 animate-fade-in">
      <PageHeader
        title="Memory Imports"
        description="Bring source archives into a scoped memory space with visible checkpoints and dedupe receipts"
      />

      <div className="grid gap-4 xl:grid-cols-[minmax(320px,420px)_minmax(0,1fr)]">
        <section className="space-y-4 rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
          <div>
            <h2 className="text-sm font-semibold text-sc-fg-primary">Start Import</h2>
            <p className="mt-1 text-sm text-sc-fg-muted">
              Imports write raw memory first, then expose extraction and promotion status.
            </p>
          </div>

          {adaptersQuery.error && (
            <div className="rounded-lg border border-sc-yellow/30 bg-sc-yellow/10 px-3 py-2 text-sm text-sc-yellow">
              Adapter metadata is unavailable. Manual adapter names can still be submitted.
            </div>
          )}

          <form className="space-y-4" onSubmit={handleSubmit}>
            <FormField label="Source URI" required>
              {field => (
                <Input
                  {...field}
                  value={sourceUri}
                  onChange={event => setSourceUri(event.target.value)}
                  placeholder="mbox:///Users/bliss/archive/messages.mbox"
                />
              )}
            </FormField>

            <FormField label="Adapter">
              {field => (
                <>
                  {adapters.length > 0 ? (
                    <Select value={adapterName} onValueChange={setAdapterName}>
                      <SelectTrigger id={field.id} aria-describedby={field['aria-describedby']}>
                        <SelectValue placeholder="Select adapter" />
                      </SelectTrigger>
                      <SelectContent>
                        {adapters.map(adapter => (
                          <SelectItem key={adapter.name} value={adapter.name}>
                            {adapter.display_name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : (
                    <Input
                      {...field}
                      value={adapterName}
                      onChange={event => setAdapterName(event.target.value)}
                      placeholder="mbox"
                    />
                  )}
                </>
              )}
            </FormField>

            {selectedAdapter && (
              <div className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight/40 p-3 text-sm">
                <p className="font-medium text-sc-fg-primary">{selectedAdapter.display_name}</p>
                <p className="mt-1 text-sc-fg-muted">
                  {selectedAdapter.source_type} · {selectedAdapter.transform_behavior}
                </p>
                <p className="mt-2 text-xs text-sc-fg-subtle">
                  Default privacy: {selectedAdapter.default_privacy_class}
                </p>
              </div>
            )}

            <div className="grid gap-3 sm:grid-cols-2">
              <FormField label="Memory Scope">
                {field => (
                  <Select
                    value={memoryScope}
                    onValueChange={value => setMemoryScope(value as MemoryScope)}
                  >
                    <SelectTrigger id={field.id} aria-describedby={field['aria-describedby']}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {MEMORY_SCOPES.map(scope => (
                        <SelectItem key={scope.value} value={scope.value}>
                          {scope.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </FormField>

              <FormField label="Scope Key">
                {field => (
                  <Input
                    {...field}
                    value={scopeKey}
                    onChange={event => setScopeKey(event.target.value)}
                    placeholder="project id or team slug"
                  />
                )}
              </FormField>
            </div>

            <FormField label="Batch Size">
              {field => (
                <Input
                  {...field}
                  type="number"
                  min={1}
                  max={1000}
                  step={1}
                  value={batchSize}
                  onChange={event => setBatchSize(event.target.value)}
                />
              )}
            </FormField>

            <Checkbox
              checked={promotionPreviewApproved}
              onCheckedChange={checked => setPromotionPreviewApproved(checked === true)}
              label="Approve promotion preview"
              description="Required before imports can prepare broader sharing or promotion actions."
            />

            <Button type="submit" loading={startImport.isPending} icon={<Upload width={16} />}>
              Start Import
            </Button>
          </form>
        </section>

        <div className="space-y-4">
          {importId && (
            <div className="flex flex-wrap justify-end gap-2">
              <Button
                variant="secondary"
                size="sm"
                loading={statusQuery.isFetching}
                icon={<RefreshCw width={15} />}
                onClick={() => statusQuery.refetch()}
              >
                Refresh
              </Button>
              <Button
                variant="secondary"
                size="sm"
                loading={resumeImport.isPending}
                disabled={
                  !statusQuery.data || !['paused', 'failed'].includes(statusQuery.data.status)
                }
                icon={<RefreshCw width={15} />}
                onClick={handleResume}
              >
                Resume
              </Button>
              <Button
                variant="danger"
                size="sm"
                loading={cancelImport.isPending}
                disabled={
                  !statusQuery.data ||
                  ['completed', 'failed', 'canceled'].includes(statusQuery.data.status)
                }
                icon={<StopCircle width={15} />}
                onClick={handleCancel}
              >
                Cancel
              </Button>
            </div>
          )}

          {!importId ? (
            <EnhancedEmptyState
              icon={<Upload width={40} height={40} className="text-sc-cyan" />}
              title="No import selected"
              description="Start an import or open an import id to watch checkpoints, dedupe, and extraction progress."
            />
          ) : statusQuery.isLoading ? (
            <LoadingState message="Loading import status..." />
          ) : statusQuery.error ? (
            <ErrorState
              title="Failed to load import"
              message={
                statusQuery.error instanceof Error ? statusQuery.error.message : 'Unknown error'
              }
            />
          ) : statusQuery.data ? (
            <SourceImportProgress status={statusQuery.data} />
          ) : (
            <EnhancedEmptyState
              icon={<Upload width={40} height={40} className="text-sc-fg-subtle" />}
              title="Import not found"
              description="The requested import id is unavailable from the current memory scope."
            />
          )}
        </div>
      </div>
    </div>
  );
}
