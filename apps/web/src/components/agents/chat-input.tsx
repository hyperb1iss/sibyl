'use client';

/**
 * Enhanced chat input with paste handling and file attachments.
 */

import { useCallback, useRef, useState } from 'react';
import { ChevronDown, Code, FileText, Plus, Send, Xmark } from '@/components/ui/icons';

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

interface ChatInputProps {
  onSend: (message: string, attachments: Attachment[]) => void;
  placeholder?: string;
  disabled?: boolean;
  isFocused?: boolean;
  onFocusChange?: (focused: boolean) => void;
}

// =============================================================================
// Helpers
// =============================================================================

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function generateId(): string {
  return `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// =============================================================================
// Component
// =============================================================================

export function ChatInput({
  onSend,
  placeholder = 'Send a message...',
  disabled = false,
  isFocused: externalFocused,
  onFocusChange,
}: ChatInputProps) {
  const [inputValue, setInputValue] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [expandedAttachments, setExpandedAttachments] = useState<Set<string>>(new Set());
  const [internalFocused, setInternalFocused] = useState(false);
  const [isDragOver, setIsDragOver] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const isFocused = externalFocused ?? internalFocused;

  // Auto-resize textarea
  const adjustHeight = useCallback(() => {
    const textarea = textareaRef.current;
    if (textarea) {
      textarea.style.height = 'auto';
      textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
    }
  }, []);

  const handleFocus = useCallback(() => {
    setInternalFocused(true);
    onFocusChange?.(true);
  }, [onFocusChange]);

  const handleBlur = useCallback(() => {
    setInternalFocused(false);
    onFocusChange?.(false);
  }, [onFocusChange]);

  // Threshold for treating pasted text as an attachment
  const TEXT_ATTACHMENT_THRESHOLD = 150; // chars
  const TEXT_ATTACHMENT_LINES = 3; // or more than N lines

  // Handle paste (text, images, files)
  const handlePaste = useCallback(async (e: React.ClipboardEvent) => {
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
        // Detect if it looks like code
        const looksLikeCode =
          /^(import |export |function |const |let |var |class |def |async |await |return |if |for |while |\{|\[|<\w+>|#include|package |use |fn |pub )/m.test(
            plainText
          ) || /[{}[\]();]/.test(plainText);

        const name = looksLikeCode
          ? `Code snippet (${lineCount} lines)`
          : `Pasted text (${lineCount} lines)`;

        setAttachments(prev => [
          ...prev,
          {
            id: generateId(),
            type: 'text',
            name,
            content: plainText,
            size: plainText.length,
          },
        ]);
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
            setAttachments(prev => [
              ...prev,
              {
                id: generateId(),
                type: 'image',
                name: 'Pasted image',
                content: dataUrl,
                preview: dataUrl,
                size: file.size,
              },
            ]);
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
            setAttachments(prev => [
              ...prev,
              {
                id: generateId(),
                type: 'file',
                name: file.name,
                content: reader.result as string,
                size: file.size,
              },
            ]);
          };
          reader.readAsText(file);
        }
      }
    }
  }, []);

  // Handle file input change
  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;

    for (const file of Array.from(files)) {
      const isImage = file.type.startsWith('image/');
      const reader = new FileReader();

      reader.onload = () => {
        const content = reader.result as string;
        setAttachments(prev => [
          ...prev,
          {
            id: generateId(),
            type: isImage ? 'image' : 'file',
            name: file.name,
            content,
            preview: isImage ? content : undefined,
            size: file.size,
          },
        ]);
      };

      if (isImage) {
        reader.readAsDataURL(file);
      } else {
        reader.readAsText(file);
      }
    }

    // Reset input
    e.target.value = '';
  }, []);

  // Handle drag and drop
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);

    const files = e.dataTransfer.files;
    if (!files.length) return;

    for (const file of Array.from(files)) {
      const isImage = file.type.startsWith('image/');
      const reader = new FileReader();

      reader.onload = () => {
        const content = reader.result as string;
        setAttachments(prev => [
          ...prev,
          {
            id: generateId(),
            type: isImage ? 'image' : 'file',
            name: file.name,
            content,
            preview: isImage ? content : undefined,
            size: file.size,
          },
        ]);
      };

      if (isImage) {
        reader.readAsDataURL(file);
      } else {
        reader.readAsText(file);
      }
    }
  }, []);

  // Remove attachment
  const removeAttachment = useCallback((id: string) => {
    setAttachments(prev => prev.filter(a => a.id !== id));
    setExpandedAttachments(prev => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  // Toggle text attachment expansion
  const toggleExpanded = useCallback((id: string) => {
    setExpandedAttachments(prev => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  // Handle submit
  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      const hasContent = inputValue.trim() || attachments.length > 0;
      if (!hasContent || disabled) return;

      // Build message with attachments
      let message = inputValue.trim();

      // Append file contents as code blocks
      for (const att of attachments) {
        if (att.type === 'file' || att.type === 'text') {
          message += `\n\n<attached_file name="${att.name}">\n${att.content}\n</attached_file>`;
        } else if (att.type === 'image') {
          message += `\n\n[Attached image: ${att.name}]`;
        }
      }

      onSend(message, attachments);
      setInputValue('');
      setAttachments([]);

      // Reset textarea height
      if (textareaRef.current) {
        textareaRef.current.style.height = 'auto';
      }
    },
    [inputValue, attachments, disabled, onSend]
  );

  // Handle keyboard shortcuts
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      // Submit on Enter (without shift)
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSubmit(e);
      }
    },
    [handleSubmit]
  );

  const hasContent = inputValue.trim() || attachments.length > 0;

  return (
    <form
      onSubmit={handleSubmit}
      className="shrink-0 p-3 border-t border-sc-fg-subtle/20 bg-sc-bg-elevated"
    >
      {/* Attachment previews */}
      {attachments.length > 0 && (
        <div className="flex flex-col gap-2 mb-2 animate-slide-up">
          {attachments.map(att => {
            const isExpanded = expandedAttachments.has(att.id);
            const isTextAttachment = att.type === 'text';
            const looksLikeCode = att.name.startsWith('Code snippet');

            // For text attachments, show preview of first few lines
            const previewLines = isTextAttachment ? att.content.split('\n').slice(0, 3) : [];
            const hasMoreLines = isTextAttachment && att.content.split('\n').length > 3;

            return (
              <div
                key={att.id}
                className={`group relative rounded-lg bg-sc-bg-base border border-sc-purple/30 text-xs overflow-hidden ${
                  isTextAttachment
                    ? 'w-full max-w-md'
                    : 'inline-flex items-center gap-2 px-2 py-1.5'
                }`}
              >
                {/* Header row */}
                <div className="flex items-center gap-2 px-2 py-1.5">
                  {att.type === 'image' && att.preview ? (
                    <img
                      src={att.preview}
                      alt={att.name}
                      className="w-8 h-8 object-cover rounded"
                    />
                  ) : isTextAttachment ? (
                    looksLikeCode ? (
                      <Code width={16} height={16} className="text-sc-coral shrink-0" />
                    ) : (
                      <FileText width={16} height={16} className="text-sc-cyan shrink-0" />
                    )
                  ) : (
                    <FileText width={16} height={16} className="text-sc-cyan shrink-0" />
                  )}
                  <div className="flex flex-col min-w-0 flex-1">
                    <span className="text-sc-fg-primary truncate">{att.name}</span>
                    {att.size && (
                      <span className="text-sc-fg-subtle text-[10px]">
                        {formatFileSize(att.size)}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    {isTextAttachment && (
                      <button
                        type="button"
                        onClick={() => toggleExpanded(att.id)}
                        className={`p-0.5 rounded hover:bg-sc-purple/20 text-sc-fg-muted hover:text-sc-purple transition-all ${
                          isExpanded ? 'rotate-180' : ''
                        }`}
                        title={isExpanded ? 'Collapse' : 'Expand'}
                      >
                        <ChevronDown width={14} height={14} />
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => removeAttachment(att.id)}
                      className="p-0.5 rounded hover:bg-sc-red/20 text-sc-fg-muted hover:text-sc-red transition-colors"
                    >
                      <Xmark width={12} height={12} />
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
                      {!isExpanded && hasMoreLines && (
                        <span className="text-sc-fg-subtle">...</span>
                      )}
                    </pre>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Drop zone overlay */}
      <div
        className={`relative transition-all duration-200 ${
          isDragOver ? 'ring-2 ring-sc-purple ring-dashed rounded-xl' : ''
        }`}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        {isDragOver && (
          <div className="absolute inset-0 bg-sc-purple/10 rounded-xl flex items-center justify-center z-10 pointer-events-none">
            <span className="text-sc-purple font-medium">Drop files here</span>
          </div>
        )}

        {/* Input container */}
        <div
          className={`flex items-end gap-2 p-1 rounded-xl transition-all duration-300 ${
            isFocused
              ? 'bg-gradient-to-r from-sc-purple/10 via-sc-bg-base to-sc-cyan/10 ring-2 ring-sc-purple/30 shadow-lg shadow-sc-purple/10'
              : 'bg-sc-bg-base'
          }`}
        >
          {/* Attach button */}
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="p-2 rounded-lg text-sc-fg-muted hover:text-sc-purple hover:bg-sc-purple/10 transition-colors"
            title="Attach file"
          >
            <Plus width={18} height={18} />
          </button>

          {/* Hidden file input */}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            onChange={handleFileSelect}
            className="hidden"
            accept="*/*"
          />

          {/* Text input */}
          <textarea
            ref={textareaRef}
            value={inputValue}
            onChange={e => {
              setInputValue(e.target.value);
              adjustHeight();
            }}
            onPaste={handlePaste}
            onKeyDown={handleKeyDown}
            onFocus={handleFocus}
            onBlur={handleBlur}
            placeholder={placeholder}
            disabled={disabled}
            rows={1}
            className="flex-1 px-2 py-2 bg-transparent border-none text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:outline-none focus-visible:outline-none resize-none min-h-[36px] max-h-[200px]"
          />

          {/* Send button */}
          <button
            type="submit"
            disabled={!hasContent || disabled}
            className={`p-2.5 rounded-lg transition-all duration-200 ${
              hasContent && !disabled
                ? 'bg-gradient-to-r from-sc-purple to-sc-purple/80 hover:from-sc-purple/90 hover:to-sc-purple/70 text-white shadow-lg shadow-sc-purple/30 hover:scale-105 active:scale-95'
                : 'bg-sc-fg-subtle/20 text-sc-fg-muted cursor-not-allowed'
            }`}
          >
            <Send width={16} height={16} />
          </button>
        </div>
      </div>

      {/* Helper text */}
      <div className="flex items-center justify-between mt-1.5 px-1">
        <span className="text-[10px] text-sc-fg-subtle">
          Paste images or drag files • Shift+Enter for new line
        </span>
        {attachments.length > 0 && (
          <button
            type="button"
            onClick={() => setAttachments([])}
            className="text-[10px] text-sc-fg-muted hover:text-sc-red transition-colors"
          >
            Clear all
          </button>
        )}
      </div>
    </form>
  );
}
