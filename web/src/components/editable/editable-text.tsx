'use client';

import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useEffect, useRef, useState } from 'react';

interface EditableTextProps {
  value: string;
  onSave: (value: string) => Promise<void> | void;
  placeholder?: string;
  className?: string;
  inputClassName?: string;
  multiline?: boolean;
  rows?: number;
  disabled?: boolean;
  required?: boolean;
}

export function EditableText({
  value,
  onSave,
  placeholder = 'Click to edit...',
  className = '',
  inputClassName = '',
  multiline = false,
  rows = 3,
  disabled = false,
  required = false,
}: EditableTextProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editValue, setEditValue] = useState(value);
  const [isSaving, setIsSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement>(null);

  // Sync with external value changes
  useEffect(() => {
    if (!isEditing) {
      setEditValue(value);
    }
  }, [value, isEditing]);

  // Focus input when editing starts
  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      // Move cursor to end
      const len = editValue.length;
      inputRef.current.setSelectionRange(len, len);
    }
  }, [isEditing, editValue.length]);

  const handleSave = useCallback(async () => {
    const trimmed = editValue.trim();
    if (required && !trimmed) {
      setEditValue(value);
      setIsEditing(false);
      return;
    }

    if (trimmed !== value) {
      setIsSaving(true);
      try {
        await onSave(trimmed);
      } finally {
        setIsSaving(false);
      }
    }
    setIsEditing(false);
  }, [editValue, value, onSave, required]);

  const handleCancel = useCallback(() => {
    setEditValue(value);
    setIsEditing(false);
  }, [value]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        handleCancel();
      }
      if (e.key === 'Enter' && !multiline) {
        e.preventDefault();
        void handleSave();
      }
      if (e.key === 'Enter' && e.metaKey && multiline) {
        e.preventDefault();
        void handleSave();
      }
    },
    [handleCancel, handleSave, multiline]
  );

  const handleClick = useCallback(() => {
    if (!disabled && !isEditing) {
      setIsEditing(true);
    }
  }, [disabled, isEditing]);

  const baseInputClass = `
    w-full bg-transparent border-none outline-none
    focus:ring-2 focus:ring-sc-purple/30 focus:bg-sc-bg-highlight
    rounded-md px-1 -mx-1 py-0.5 -my-0.5
    transition-all duration-150
    ${inputClassName}
  `;

  if (isEditing) {
    return (
      <AnimatePresence mode="wait">
        <motion.div initial={{ opacity: 0.8 }} animate={{ opacity: 1 }} className="relative">
          {multiline ? (
            <textarea
              ref={inputRef as React.RefObject<HTMLTextAreaElement>}
              value={editValue}
              onChange={e => setEditValue(e.target.value)}
              onBlur={handleSave}
              onKeyDown={handleKeyDown}
              placeholder={placeholder}
              rows={rows}
              disabled={isSaving}
              className={`${baseInputClass} resize-y min-h-[60px] ${className}`}
            />
          ) : (
            <input
              ref={inputRef as React.RefObject<HTMLInputElement>}
              type="text"
              value={editValue}
              onChange={e => setEditValue(e.target.value)}
              onBlur={handleSave}
              onKeyDown={handleKeyDown}
              placeholder={placeholder}
              disabled={isSaving}
              className={`${baseInputClass} ${className}`}
            />
          )}
          {isSaving && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="absolute right-2 top-1/2 -translate-y-1/2"
            >
              <div className="w-3 h-3 border-2 border-sc-purple border-t-transparent rounded-full animate-spin" />
            </motion.div>
          )}
        </motion.div>
      </AnimatePresence>
    );
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={disabled}
      className={`
        ${className}
        ${disabled ? 'cursor-default' : 'cursor-text hover:bg-sc-bg-highlight/50'}
        rounded-md px-1 -mx-1 py-0.5 -my-0.5
        transition-colors duration-150
        focus:outline-none focus:ring-2 focus:ring-sc-purple/30
        ${!value && placeholder ? 'text-sc-fg-subtle italic' : ''}
        text-left
      `}
    >
      {value || placeholder}
    </button>
  );
}
