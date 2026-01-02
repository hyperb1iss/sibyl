'use client';

/**
 * Enhanced chat input with paste handling and file attachments.
 */

import { useCallback, useRef, useState } from 'react';
import { Plus, Send } from '@/components/ui/icons';
import { AttachmentList } from './attachment-preview';
import { type Attachment, useAttachments } from './use-attachments';

// =============================================================================
// Types
// =============================================================================

interface ChatInputProps {
  onSend: (message: string, attachments: Attachment[]) => void;
  placeholder?: string;
  disabled?: boolean;
  isFocused?: boolean;
  onFocusChange?: (focused: boolean) => void;
}

// Re-export Attachment type for consumers
export type { Attachment };

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
  const [internalFocused, setInternalFocused] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Use extracted attachment management hook
  const {
    attachments,
    expandedIds,
    isDragOver,
    setIsDragOver,
    removeAttachment,
    toggleExpanded,
    clearAll,
    handlePaste,
    handleFileSelect,
    handleDrop,
  } = useAttachments();

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

  // Handle drag over
  const handleDragOver = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragOver(true);
    },
    [setIsDragOver]
  );

  const handleDragLeave = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragOver(false);
    },
    [setIsDragOver]
  );

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
      clearAll();

      // Reset textarea height
      if (textareaRef.current) {
        textareaRef.current.style.height = 'auto';
      }
    },
    [inputValue, attachments, disabled, onSend, clearAll]
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
      aria-label="Chat message input"
    >
      {/* Attachment previews */}
      <AttachmentList
        attachments={attachments}
        expandedIds={expandedIds}
        onToggleExpanded={toggleExpanded}
        onRemove={removeAttachment}
        onClearAll={clearAll}
      />

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
            aria-label="Attach file"
          >
            <Plus width={18} height={18} aria-hidden="true" />
          </button>

          {/* Hidden file input */}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            onChange={handleFileSelect}
            className="hidden"
            accept="*/*"
            aria-label="Select files to attach"
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
            aria-label="Message input"
            className="flex-1 px-2 py-2 bg-transparent border-none text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:outline-none focus-visible:outline-none resize-none min-h-[36px] max-h-[200px]"
          />

          {/* Send button */}
          <button
            type="submit"
            disabled={!hasContent || disabled}
            aria-label="Send message"
            className={`p-2.5 rounded-lg transition-all duration-200 ${
              hasContent && !disabled
                ? 'bg-gradient-to-r from-sc-purple to-sc-purple/80 hover:from-sc-purple/90 hover:to-sc-purple/70 text-white shadow-lg shadow-sc-purple/30 hover:scale-105 active:scale-95'
                : 'bg-sc-fg-subtle/20 text-sc-fg-muted cursor-not-allowed'
            }`}
          >
            <Send width={16} height={16} aria-hidden="true" />
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
            onClick={clearAll}
            aria-label="Clear all attachments"
            className="text-[10px] text-sc-fg-muted hover:text-sc-red transition-colors"
          >
            Clear all
          </button>
        )}
      </div>
    </form>
  );
}
