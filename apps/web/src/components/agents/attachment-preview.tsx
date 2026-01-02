'use client';

/**
 * Attachment preview component for chat input.
 *
 * Renders file, image, and text attachments with expand/collapse support.
 */

import { ChevronDown, Code, FileText, Xmark } from '@/components/ui/icons';
import type { Attachment } from './use-attachments';

// =============================================================================
// Types
// =============================================================================

interface AttachmentPreviewProps {
  attachment: Attachment;
  isExpanded: boolean;
  onToggleExpanded: () => void;
  onRemove: () => void;
}

interface AttachmentListProps {
  attachments: Attachment[];
  expandedIds: Set<string>;
  onToggleExpanded: (id: string) => void;
  onRemove: (id: string) => void;
  onClearAll: () => void;
}

// =============================================================================
// Helpers
// =============================================================================

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// =============================================================================
// AttachmentPreview
// =============================================================================

/** Single attachment preview with expand/collapse support */
export function AttachmentPreview({
  attachment: att,
  isExpanded,
  onToggleExpanded,
  onRemove,
}: AttachmentPreviewProps) {
  const isTextAttachment = att.type === 'text';
  const looksLikeCode = att.name.startsWith('Code snippet');

  // For text attachments, show preview of first few lines
  const previewLines = isTextAttachment ? att.content.split('\n').slice(0, 3) : [];
  const hasMoreLines = isTextAttachment && att.content.split('\n').length > 3;

  return (
    <div
      className={`group relative rounded-lg bg-sc-bg-base border border-sc-purple/30 text-xs overflow-hidden ${
        isTextAttachment ? 'w-full max-w-md' : 'inline-flex items-center gap-2 px-2 py-1.5'
      }`}
    >
      {/* Header row */}
      <div className="flex items-center gap-2 px-2 py-1.5">
        {att.type === 'image' && att.preview ? (
          <img src={att.preview} alt={att.name} className="w-8 h-8 object-cover rounded" />
        ) : isTextAttachment ? (
          looksLikeCode ? (
            <Code width={16} height={16} className="text-sc-coral shrink-0" aria-hidden="true" />
          ) : (
            <FileText width={16} height={16} className="text-sc-cyan shrink-0" aria-hidden="true" />
          )
        ) : (
          <FileText width={16} height={16} className="text-sc-cyan shrink-0" aria-hidden="true" />
        )}
        <div className="flex flex-col min-w-0 flex-1">
          <span className="text-sc-fg-primary truncate">{att.name}</span>
          {att.size && (
            <span className="text-sc-fg-subtle text-[10px]">{formatFileSize(att.size)}</span>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {isTextAttachment && (
            <button
              type="button"
              onClick={onToggleExpanded}
              aria-expanded={isExpanded}
              aria-label={isExpanded ? 'Collapse content' : 'Expand content'}
              className={`p-0.5 rounded hover:bg-sc-purple/20 text-sc-fg-muted hover:text-sc-purple transition-all ${
                isExpanded ? 'rotate-180' : ''
              }`}
            >
              <ChevronDown width={14} height={14} aria-hidden="true" />
            </button>
          )}
          <button
            type="button"
            onClick={onRemove}
            aria-label={`Remove ${att.name}`}
            className="p-0.5 rounded hover:bg-sc-red/20 text-sc-fg-muted hover:text-sc-red transition-colors"
          >
            <Xmark width={12} height={12} aria-hidden="true" />
          </button>
        </div>
      </div>

      {/* Text preview/content */}
      {isTextAttachment && (
        <div
          className={`border-t border-sc-fg-subtle/20 transition-all duration-200 ${
            isExpanded ? 'max-h-[300px]' : 'max-h-[72px]'
          } overflow-hidden`}
        >
          <pre
            className={`p-2 text-[11px] font-mono leading-relaxed overflow-x-auto ${
              looksLikeCode ? 'text-sc-fg-secondary' : 'text-sc-fg-muted'
            }`}
          >
            {isExpanded ? att.content : previewLines.join('\n')}
            {!isExpanded && hasMoreLines && <span className="text-sc-fg-subtle">...</span>}
          </pre>
        </div>
      )}
    </div>
  );
}

// =============================================================================
// AttachmentList
// =============================================================================

/** List of attachment previews with clear all button */
export function AttachmentList({
  attachments,
  expandedIds,
  onToggleExpanded,
  onRemove,
  onClearAll,
}: AttachmentListProps) {
  if (attachments.length === 0) return null;

  return (
    <div className="flex flex-col gap-2 mb-2 animate-slide-up">
      {attachments.map(att => (
        <AttachmentPreview
          key={att.id}
          attachment={att}
          isExpanded={expandedIds.has(att.id)}
          onToggleExpanded={() => onToggleExpanded(att.id)}
          onRemove={() => onRemove(att.id)}
        />
      ))}
      {attachments.length > 1 && (
        <button
          type="button"
          onClick={onClearAll}
          aria-label="Clear all attachments"
          className="self-end text-[10px] text-sc-fg-muted hover:text-sc-red transition-colors"
        >
          Clear all
        </button>
      )}
    </div>
  );
}
