'use client';

import { useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { AlertTriangle, CheckCircle, EditPencil } from '@/components/ui/icons';
import { Input, Label, Textarea } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type {
  MemoryCorrectionAction,
  MemoryCorrectionResponse,
  MemorySourceInspectResponse,
} from '@/lib/api';
import { useApplyMemoryCorrection, usePreviewMemoryCorrection } from '@/lib/hooks';

const CORRECTION_ACTIONS: Array<{ value: MemoryCorrectionAction; label: string }> = [
  { value: 'hide', label: 'Hide' },
  { value: 'redact', label: 'Redact' },
  { value: 'mark_wrong', label: 'Mark wrong' },
  { value: 'mark_stale', label: 'Mark stale' },
  { value: 'mark_sensitive', label: 'Mark sensitive' },
  { value: 'mark_duplicate', label: 'Mark duplicate' },
  { value: 'supersede', label: 'Supersede' },
  { value: 'restore', label: 'Restore' },
  { value: 'delete', label: 'Delete' },
];

interface SourceCorrectionDialogProps {
  source: MemorySourceInspectResponse;
  onApplied?: () => void;
}

function impactEntries(value: Record<string, unknown>): [string, string][] {
  return Object.entries(value).map(([key, entry]) => [key, String(entry)]);
}

function ImpactList({ title, value }: { title: string; value: Record<string, unknown> }) {
  const entries = impactEntries(value);
  return (
    <div className="rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-highlight/50 p-3">
      <p className="text-xs font-medium uppercase tracking-[0.1em] text-sc-fg-subtle">{title}</p>
      {entries.length === 0 ? (
        <p className="mt-2 text-sm text-sc-fg-muted">No impact reported</p>
      ) : (
        <dl className="mt-2 grid gap-1 text-sm">
          {entries.map(([key, entry]) => (
            <div key={key} className="flex items-center justify-between gap-3">
              <dt className="text-sc-fg-muted">{key.replace(/_/g, ' ')}</dt>
              <dd className="truncate text-sc-fg-primary">{entry}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

function PreviewSummary({ preview }: { preview: MemoryCorrectionResponse }) {
  return (
    <div className="space-y-3 rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight p-3">
      <div className="flex items-center justify-between gap-3">
        <span
          className={`inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs ${
            preview.allowed
              ? 'border-sc-green/30 bg-sc-green/10 text-sc-green'
              : 'border-sc-red/30 bg-sc-red/10 text-sc-red'
          }`}
        >
          {preview.allowed ? (
            <CheckCircle width={13} height={13} />
          ) : (
            <AlertTriangle width={13} height={13} />
          )}
          {preview.reason}
        </span>
        <span className="text-xs text-sc-fg-subtle">
          {preview.target_lifecycle_state}
          {preview.target_lifecycle_flags.length > 0 &&
            ` · ${preview.target_lifecycle_flags.join(', ')}`}
        </span>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <ImpactList title="Recall" value={preview.recall_impact} />
        <ImpactList title="Synthesis" value={preview.synthesis_impact} />
      </div>
      {preview.policy_reasons.length > 0 && (
        <p className="text-xs text-sc-fg-muted">{preview.policy_reasons.join(', ')}</p>
      )}
    </div>
  );
}

export function SourceCorrectionDialog({ source, onApplied }: SourceCorrectionDialogProps) {
  const [open, setOpen] = useState(false);
  const [action, setAction] = useState<MemoryCorrectionAction>('hide');
  const [reason, setReason] = useState('');
  const [replacementSourceId, setReplacementSourceId] = useState('');
  const [duplicateOfSourceId, setDuplicateOfSourceId] = useState('');
  const [preview, setPreview] = useState<MemoryCorrectionResponse | null>(null);

  const previewCorrection = usePreviewMemoryCorrection();
  const applyCorrection = useApplyMemoryCorrection();

  const request = {
    action,
    reason: reason.trim() || null,
    replacement_source_id: replacementSourceId.trim() || null,
    duplicate_of_source_id: duplicateOfSourceId.trim() || null,
    metadata: { surface: 'web_memory_source_inspect' },
  };

  async function handlePreview() {
    const result = await previewCorrection.mutateAsync({ sourceId: source.id, request });
    setPreview(result);
  }

  async function handleApply() {
    if (!preview?.allowed) return;
    await applyCorrection.mutateAsync({ sourceId: source.id, request });
    toast.success('Correction applied');
    setOpen(false);
    setPreview(null);
    onApplied?.();
  }

  function resetPreview() {
    setPreview(null);
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight px-3 py-1.5 text-sm font-medium text-sc-fg-primary transition-colors hover:border-sc-purple/50 hover:text-sc-purple">
        <EditPencil width={15} height={15} />
        Correction
      </DialogTrigger>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Source Correction</DialogTitle>
          <DialogDescription>{source.title || source.source_id}</DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="grid gap-2">
            <Label htmlFor="memory-correction-action">Action</Label>
            <Select
              value={action}
              onValueChange={value => {
                setAction(value as MemoryCorrectionAction);
                resetPreview();
              }}
            >
              <SelectTrigger id="memory-correction-action" aria-label="Correction action">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CORRECTION_ACTIONS.map(option => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div>
            <Label htmlFor="memory-correction-reason">Reason</Label>
            <Textarea
              id="memory-correction-reason"
              value={reason}
              onChange={event => {
                setReason(event.target.value);
                resetPreview();
              }}
              rows={3}
              placeholder="Policy, freshness, duplication, or provenance note"
            />
          </div>

          {action === 'supersede' && (
            <div>
              <Label htmlFor="memory-correction-replacement">Replacement source</Label>
              <Input
                id="memory-correction-replacement"
                value={replacementSourceId}
                onChange={event => {
                  setReplacementSourceId(event.target.value);
                  resetPreview();
                }}
                placeholder="source or raw memory id"
              />
            </div>
          )}

          {action === 'mark_duplicate' && (
            <div>
              <Label htmlFor="memory-correction-duplicate">Duplicate of</Label>
              <Input
                id="memory-correction-duplicate"
                value={duplicateOfSourceId}
                onChange={event => {
                  setDuplicateOfSourceId(event.target.value);
                  resetPreview();
                }}
                placeholder="canonical source or raw memory id"
              />
            </div>
          )}

          {preview && <PreviewSummary preview={preview} />}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            variant="secondary"
            loading={previewCorrection.isPending}
            onClick={() => void handlePreview()}
          >
            Preview
          </Button>
          <Button
            variant="primary"
            disabled={!preview?.allowed}
            loading={applyCorrection.isPending}
            onClick={() => void handleApply()}
          >
            Apply
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
