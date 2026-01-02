/**
 * Hook for managing file attachments in chat input.
 *
 * Handles:
 * - Paste events (images, text, files)
 * - File input selection
 * - Drag and drop
 * - Attachment expansion state
 */

import { useCallback, useState } from 'react';

// =============================================================================
// Types
// =============================================================================

export interface Attachment {
  id: string;
  type: 'file' | 'image' | 'text';
  name: string;
  content: string; // base64 for images, text content for files/text
  size?: number;
  preview?: string; // data URL for image preview
}

export interface UseAttachmentsReturn {
  attachments: Attachment[];
  expandedIds: Set<string>;
  isDragOver: boolean;
  setIsDragOver: (value: boolean) => void;
  addAttachment: (attachment: Attachment) => void;
  removeAttachment: (id: string) => void;
  toggleExpanded: (id: string) => void;
  clearAll: () => void;
  handlePaste: (e: React.ClipboardEvent) => void;
  handleFileSelect: (e: React.ChangeEvent<HTMLInputElement>) => void;
  handleDrop: (e: React.DragEvent) => void;
}

// =============================================================================
// Helpers
// =============================================================================

function generateId(): string {
  return `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Threshold for treating pasted text as an attachment */
const TEXT_ATTACHMENT_THRESHOLD = 150; // chars
const TEXT_ATTACHMENT_LINES = 3; // or more than N lines

/** Detect if text looks like code */
function looksLikeCode(text: string): boolean {
  return (
    /^(import |export |function |const |let |var |class |def |async |await |return |if |for |while |\{|\[|<\w+>|#include|package |use |fn |pub )/m.test(
      text
    ) || /[{}[\]();]/.test(text)
  );
}

// =============================================================================
// Hook
// =============================================================================

export function useAttachments(): UseAttachmentsReturn {
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [isDragOver, setIsDragOver] = useState(false);

  const addAttachment = useCallback((attachment: Attachment) => {
    setAttachments(prev => [...prev, attachment]);
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setAttachments(prev => prev.filter(a => a.id !== id));
    setExpandedIds(prev => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const toggleExpanded = useCallback((id: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const clearAll = useCallback(() => {
    setAttachments([]);
    setExpandedIds(new Set());
  }, []);

  // Handle paste (text, images, files)
  const handlePaste = useCallback(
    (e: React.ClipboardEvent) => {
      const items = e.clipboardData?.items;
      if (!items) return;

      // Check for plain text first
      const plainText = e.clipboardData.getData('text/plain');
      const lineCount = (plainText.match(/\n/g) || []).length + 1;
      const isLargeText =
        plainText.length > TEXT_ATTACHMENT_THRESHOLD || lineCount > TEXT_ATTACHMENT_LINES;

      // If it's large text with no files/images, treat as text attachment
      if (isLargeText) {
        const hasMedia = Array.from(items).some(
          item => item.type.startsWith('image/') || (item.kind === 'file' && item.type !== '')
        );

        if (!hasMedia) {
          e.preventDefault();
          const isCode = looksLikeCode(plainText);
          const name = isCode
            ? `Code snippet (${lineCount} lines)`
            : `Pasted text (${lineCount} lines)`;

          addAttachment({
            id: generateId(),
            type: 'text',
            name,
            content: plainText,
            size: plainText.length,
          });
          return;
        }
      }

      for (const item of Array.from(items)) {
        // Handle images
        if (item.type.startsWith('image/')) {
          e.preventDefault();
          const file = item.getAsFile();
          if (file) {
            const reader = new FileReader();
            reader.onload = () => {
              const dataUrl = reader.result as string;
              addAttachment({
                id: generateId(),
                type: 'image',
                name: 'Pasted image',
                content: dataUrl,
                preview: dataUrl,
                size: file.size,
              });
            };
            reader.readAsDataURL(file);
          }
        }
        // Handle files
        else if (item.kind === 'file') {
          e.preventDefault();
          const file = item.getAsFile();
          if (file) {
            const reader = new FileReader();
            reader.onload = () => {
              addAttachment({
                id: generateId(),
                type: 'file',
                name: file.name,
                content: reader.result as string,
                size: file.size,
              });
            };
            reader.readAsText(file);
          }
        }
      }
    },
    [addAttachment]
  );

  // Handle file input change
  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = e.target.files;
      if (!files) return;

      for (const file of Array.from(files)) {
        const isImage = file.type.startsWith('image/');
        const reader = new FileReader();

        reader.onload = () => {
          const content = reader.result as string;
          addAttachment({
            id: generateId(),
            type: isImage ? 'image' : 'file',
            name: file.name,
            content,
            preview: isImage ? content : undefined,
            size: file.size,
          });
        };

        if (isImage) {
          reader.readAsDataURL(file);
        } else {
          reader.readAsText(file);
        }
      }

      // Reset input
      e.target.value = '';
    },
    [addAttachment]
  );

  // Handle drag and drop
  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragOver(false);

      const files = e.dataTransfer.files;
      if (!files.length) return;

      for (const file of Array.from(files)) {
        const isImage = file.type.startsWith('image/');
        const reader = new FileReader();

        reader.onload = () => {
          const content = reader.result as string;
          addAttachment({
            id: generateId(),
            type: isImage ? 'image' : 'file',
            name: file.name,
            content,
            preview: isImage ? content : undefined,
            size: file.size,
          });
        };

        if (isImage) {
          reader.readAsDataURL(file);
        } else {
          reader.readAsText(file);
        }
      }
    },
    [addAttachment]
  );

  return {
    attachments,
    expandedIds,
    isDragOver,
    setIsDragOver,
    addAttachment,
    removeAttachment,
    toggleExpanded,
    clearAll,
    handlePaste,
    handleFileSelect,
    handleDrop,
  };
}
