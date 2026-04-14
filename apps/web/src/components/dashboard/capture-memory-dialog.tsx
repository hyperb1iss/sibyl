'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Sparkles, Xmark } from '@/components/ui/icons';
import { useCreateEntity } from '@/lib/hooks';

const CAPTURE_TITLE_CHARS = 72;

const CAPTURE_TYPES = [
  {
    value: 'episode',
    label: 'Learning',
    description: 'Save a fresh insight or debugging note',
    classes: 'border-sc-green/30 bg-sc-green/10 text-sc-green',
  },
  {
    value: 'pattern',
    label: 'Pattern',
    description: 'Promote something repeatable',
    classes: 'border-sc-purple/30 bg-sc-purple/10 text-sc-purple',
  },
  {
    value: 'error_pattern',
    label: 'Failure Mode',
    description: 'Capture a trap worth avoiding later',
    classes: 'border-sc-coral/30 bg-sc-coral/10 text-sc-coral',
  },
] as const;

function deriveCaptureTitle(content: string) {
  const compact = content.replace(/\s+/g, ' ').trim();
  if (!compact) return 'Untitled capture';
  if (compact.length <= CAPTURE_TITLE_CHARS) return compact;
  return `${compact.slice(0, CAPTURE_TITLE_CHARS - 1).replace(/[ ,;:-]+$/, '')}…`;
}

interface CaptureMemoryDialogProps {
  isOpen: boolean;
  onClose: () => void;
}

export function CaptureMemoryDialog({ isOpen, onClose }: CaptureMemoryDialogProps) {
  const createEntity = useCreateEntity();
  const titleRef = useRef<HTMLInputElement>(null);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [tags, setTags] = useState('');
  const [entityType, setEntityType] = useState<(typeof CAPTURE_TYPES)[number]['value']>('episode');

  useEffect(() => {
    if (!isOpen) return;
    setTitle('');
    setContent('');
    setTags('');
    setEntityType('episode');
    setTimeout(() => titleRef.current?.focus(), 0);
  }, [isOpen]);

  const resolvedTitle = useMemo(() => {
    if (title.trim()) return title.trim();
    return deriveCaptureTitle(content);
  }, [content, title]);

  const handleSubmit = useCallback(
    async (event?: React.FormEvent) => {
      event?.preventDefault();
      if (!content.trim() || createEntity.isPending) return;

      try {
        await createEntity.mutateAsync({
          name: resolvedTitle,
          content: content.trim(),
          entity_type: entityType,
          tags: tags
            ? tags
                .split(',')
                .map(tag => tag.trim())
                .filter(Boolean)
            : undefined,
          metadata: { capture_mode: 'quick', capture_surface: 'dashboard' },
        });
        toast.success(`Captured ${entityType.replace('_', ' ')}`);
        onClose();
      } catch {
        toast.error('Failed to capture memory');
      }
    },
    [content, createEntity, entityType, onClose, resolvedTitle, tags]
  );

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      }
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault();
        void handleSubmit();
      }
    },
    [handleSubmit, onClose]
  );

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[8vh]"
      role="presentation"
    >
      <button
        type="button"
        className="absolute inset-0 cursor-default bg-sc-bg-dark/80 backdrop-blur-sm"
        onClick={onClose}
        aria-label="Close modal"
      />

      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="capture-memory-title"
        className="relative mx-4 w-full max-w-2xl overflow-hidden rounded-2xl border border-sc-purple/20 bg-sc-bg-base shadow-2xl shadow-sc-purple/10"
        onKeyDown={handleKeyDown}
      >
        <div className="border-b border-sc-fg-subtle/10 bg-gradient-to-r from-sc-purple/10 via-transparent to-sc-cyan/10 px-5 py-4">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h2
                id="capture-memory-title"
                className="flex items-center gap-2 text-lg font-semibold text-sc-fg-primary"
              >
                <Sparkles width={18} height={18} className="text-sc-cyan" />
                Capture Memory
              </h2>
              <p className="mt-1 text-sm text-sc-fg-muted">
                Drop a fresh learning now. Promote it to a pattern later if it earns it.
              </p>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="text-sc-fg-subtle transition-colors hover:text-sc-fg-primary"
              aria-label="Close capture dialog"
            >
              <Xmark width={18} height={18} />
            </button>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-5 p-5">
          <div className="grid gap-3 sm:grid-cols-3">
            {CAPTURE_TYPES.map(option => {
              const active = option.value === entityType;
              return (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => setEntityType(option.value)}
                  className={`rounded-xl border px-3 py-3 text-left transition-all ${
                    active
                      ? option.classes
                      : 'border-sc-fg-subtle/20 bg-sc-bg-highlight/40 text-sc-fg-muted hover:border-sc-fg-subtle/40'
                  }`}
                >
                  <div className="text-sm font-medium">{option.label}</div>
                  <div className="mt-1 text-xs opacity-80">{option.description}</div>
                </button>
              );
            })}
          </div>

          <div className="space-y-3">
            <div>
              <label htmlFor="capture-title" className="mb-2 block text-sm text-sc-fg-muted">
                Title
              </label>
              <input
                id="capture-title"
                ref={titleRef}
                type="text"
                value={title}
                onChange={event => setTitle(event.target.value)}
                placeholder="Optional. We’ll derive one from the memory if you leave it blank."
                className="w-full rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-highlight px-4 py-3 text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-cyan focus:outline-none focus:ring-2 focus:ring-sc-cyan/10"
              />
            </div>

            <div>
              <label htmlFor="capture-content" className="mb-2 block text-sm text-sc-fg-muted">
                Memory
              </label>
              <textarea
                id="capture-content"
                value={content}
                onChange={event => setContent(event.target.value)}
                rows={8}
                placeholder="What just happened, what worked, what failed, or what future-you should remember."
                className="w-full resize-none rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-highlight px-4 py-3 text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-cyan focus:outline-none focus:ring-2 focus:ring-sc-cyan/10"
              />
            </div>

            <div>
              <label htmlFor="capture-tags" className="mb-2 block text-sm text-sc-fg-muted">
                Tags
              </label>
              <input
                id="capture-tags"
                type="text"
                value={tags}
                onChange={event => setTags(event.target.value)}
                placeholder="optional, comma-separated"
                className="w-full rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-highlight px-4 py-3 text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-cyan focus:outline-none focus:ring-2 focus:ring-sc-cyan/10"
              />
            </div>
          </div>

          <div className="flex flex-col gap-3 border-t border-sc-fg-subtle/10 pt-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="text-xs text-sc-fg-subtle">
              {title.trim() ? 'Title locked in.' : `Auto title: ${resolvedTitle}`}
            </div>
            <div className="flex items-center gap-2">
              <Button variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              <Button
                type="submit"
                loading={createEntity.isPending}
                disabled={!content.trim()}
                icon={<Sparkles width={16} height={16} />}
              >
                Capture Memory
              </Button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
