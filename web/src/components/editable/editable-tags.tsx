'use client';

import * as Popover from '@radix-ui/react-popover';
import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useRef, useState } from 'react';
import { Plus, X } from '@/components/ui/icons';

interface EditableTagsProps {
  values: string[];
  onSave: (values: string[]) => Promise<void> | void;
  placeholder?: string;
  addPlaceholder?: string;
  tagClassName?: string;
  disabled?: boolean;
  suggestions?: string[];
}

export function EditableTags({
  values,
  onSave,
  placeholder = 'Add...',
  addPlaceholder = 'Type and press Enter',
  tagClassName = 'bg-sc-cyan/10 text-sc-cyan border-sc-cyan/20',
  disabled = false,
  suggestions = [],
}: EditableTagsProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [inputValue, setInputValue] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleAdd = useCallback(
    async (tag: string) => {
      const trimmed = tag.trim();
      if (!trimmed || values.includes(trimmed)) {
        setInputValue('');
        return;
      }

      setIsSaving(true);
      try {
        await onSave([...values, trimmed]);
        setInputValue('');
      } finally {
        setIsSaving(false);
      }
    },
    [values, onSave]
  );

  const handleRemove = useCallback(
    async (tag: string) => {
      setIsSaving(true);
      try {
        await onSave(values.filter(v => v !== tag));
      } finally {
        setIsSaving(false);
      }
    },
    [values, onSave]
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        handleAdd(inputValue);
      }
      if (e.key === 'Escape') {
        setIsOpen(false);
      }
      if (e.key === 'Backspace' && !inputValue && values.length > 0) {
        handleRemove(values[values.length - 1]);
      }
    },
    [inputValue, values, handleAdd, handleRemove]
  );

  // Filter suggestions that aren't already selected
  const filteredSuggestions = suggestions.filter(
    s => !values.includes(s) && s.toLowerCase().includes(inputValue.toLowerCase())
  );

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <AnimatePresence mode="popLayout">
        {values.map(tag => (
          <motion.span
            key={tag}
            initial={{ opacity: 0, scale: 0.8 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.8 }}
            transition={{ duration: 0.15 }}
            className={`
              inline-flex items-center gap-1 px-2 py-0.5 text-sm rounded-md border
              ${tagClassName}
              ${disabled ? '' : 'group'}
            `}
          >
            {tag}
            {!disabled && (
              <button
                type="button"
                onClick={() => handleRemove(tag)}
                disabled={isSaving}
                className="opacity-0 group-hover:opacity-60 hover:opacity-100 transition-opacity"
              >
                <X width={12} height={12} />
              </button>
            )}
          </motion.span>
        ))}
      </AnimatePresence>

      {!disabled && (
        <Popover.Root open={isOpen} onOpenChange={setIsOpen}>
          <Popover.Trigger asChild>
            <button
              type="button"
              className={`
                inline-flex items-center gap-1 px-2 py-0.5 text-sm rounded-md
                text-sc-fg-subtle hover:text-sc-fg-muted hover:bg-sc-bg-highlight/50
                transition-colors duration-150
                focus:outline-none focus:ring-2 focus:ring-sc-purple/30
              `}
            >
              <Plus width={14} height={14} />
              {values.length === 0 && placeholder}
            </button>
          </Popover.Trigger>

          <AnimatePresence>
            {isOpen && (
              <Popover.Portal forceMount>
                <Popover.Content
                  align="start"
                  sideOffset={4}
                  asChild
                  onOpenAutoFocus={e => {
                    e.preventDefault();
                    inputRef.current?.focus();
                  }}
                >
                  <motion.div
                    initial={{ opacity: 0, y: -4, scale: 0.96 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={{ opacity: 0, y: -4, scale: 0.96 }}
                    transition={{ duration: 0.15, ease: 'easeOut' }}
                    className="z-50 w-[200px] bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-xl shadow-xl shadow-black/30 overflow-hidden"
                  >
                    <div className="p-2">
                      <input
                        ref={inputRef}
                        type="text"
                        value={inputValue}
                        onChange={e => setInputValue(e.target.value)}
                        onKeyDown={handleKeyDown}
                        placeholder={addPlaceholder}
                        disabled={isSaving}
                        className="w-full px-2 py-1.5 bg-sc-bg-highlight border border-sc-fg-subtle/30 rounded-lg text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none"
                      />
                    </div>

                    {filteredSuggestions.length > 0 && (
                      <div className="border-t border-sc-fg-subtle/10 py-1 max-h-[150px] overflow-y-auto">
                        {filteredSuggestions.slice(0, 5).map(suggestion => (
                          <button
                            key={suggestion}
                            type="button"
                            onClick={() => handleAdd(suggestion)}
                            className="w-full px-3 py-1.5 text-sm text-left text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight/50 transition-colors"
                          >
                            {suggestion}
                          </button>
                        ))}
                      </div>
                    )}
                  </motion.div>
                </Popover.Content>
              </Popover.Portal>
            )}
          </AnimatePresence>
        </Popover.Root>
      )}

      {isSaving && (
        <div className="w-3 h-3 border-2 border-sc-purple border-t-transparent rounded-full animate-spin" />
      )}
    </div>
  );
}
