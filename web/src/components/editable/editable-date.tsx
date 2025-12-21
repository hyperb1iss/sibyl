'use client';

import * as Popover from '@radix-ui/react-popover';
import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useRef, useState } from 'react';
import { Calendar, X } from '@/components/ui/icons';

interface EditableDateProps {
  value: string | undefined;
  onSave: (value: string | undefined) => Promise<void> | void;
  placeholder?: string;
  disabled?: boolean;
  showIcon?: boolean;
  formatDisplay?: (date: Date) => string;
}

function defaultFormat(date: Date): string {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const tomorrow = new Date(today);
  tomorrow.setDate(tomorrow.getDate() + 1);
  const dateOnly = new Date(date.getFullYear(), date.getMonth(), date.getDate());

  if (dateOnly.getTime() === today.getTime()) return 'Today';
  if (dateOnly.getTime() === tomorrow.getTime()) return 'Tomorrow';

  const diffDays = Math.ceil((dateOnly.getTime() - today.getTime()) / (1000 * 60 * 60 * 24));
  if (diffDays < 0) return `${Math.abs(diffDays)}d overdue`;
  if (diffDays <= 7) return `${diffDays}d left`;

  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

export function EditableDate({
  value,
  onSave,
  placeholder = 'Set date',
  disabled = false,
  showIcon = true,
  formatDisplay = defaultFormat,
}: EditableDateProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const date = value ? new Date(value) : undefined;
  const isOverdue = date && date < new Date();

  const handleChange = useCallback(
    async (newValue: string) => {
      setIsSaving(true);
      try {
        await onSave(newValue || undefined);
      } finally {
        setIsSaving(false);
      }
      setIsOpen(false);
    },
    [onSave]
  );

  const handleClear = useCallback(
    async (e: React.MouseEvent) => {
      e.stopPropagation();
      setIsSaving(true);
      try {
        await onSave(undefined);
      } finally {
        setIsSaving(false);
      }
    },
    [onSave]
  );

  return (
    <Popover.Root open={isOpen} onOpenChange={setIsOpen}>
      <Popover.Trigger asChild disabled={disabled || isSaving}>
        <button
          type="button"
          className={`
            inline-flex items-center gap-1.5 rounded-md px-1 -mx-1 py-0.5 -my-0.5
            transition-all duration-150 group
            ${disabled ? 'cursor-default opacity-60' : 'cursor-pointer hover:bg-sc-bg-highlight/50'}
            focus:outline-none focus:ring-2 focus:ring-sc-purple/30
            ${!value ? 'text-sc-fg-subtle' : isOverdue ? 'text-sc-red' : 'text-sc-fg-primary'}
          `}
        >
          {showIcon && <Calendar width={14} height={14} className="opacity-60" />}
          <span className={!value ? 'italic' : ''}>{date ? formatDisplay(date) : placeholder}</span>
          {value && !disabled && (
            <span
              onClick={handleClear}
              onKeyDown={e => e.key === 'Enter' && handleClear(e as unknown as React.MouseEvent)}
              role="button"
              tabIndex={0}
              className="opacity-0 group-hover:opacity-60 hover:opacity-100 transition-opacity"
            >
              <X width={12} height={12} />
            </span>
          )}
          {isSaving && (
            <div className="w-3 h-3 border-2 border-current border-t-transparent rounded-full animate-spin" />
          )}
        </button>
      </Popover.Trigger>

      <AnimatePresence>
        {isOpen && (
          <Popover.Portal forceMount>
            <Popover.Content
              align="start"
              sideOffset={4}
              asChild
              onEscapeKeyDown={() => setIsOpen(false)}
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
                className="z-50 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-xl shadow-xl shadow-black/30 p-3"
              >
                <input
                  ref={inputRef}
                  type="date"
                  value={value || ''}
                  onChange={e => handleChange(e.target.value)}
                  className="bg-sc-bg-highlight border border-sc-fg-subtle/30 rounded-lg px-3 py-2 text-sc-fg-primary focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/20"
                />
                <div className="flex gap-2 mt-2">
                  <button
                    type="button"
                    onClick={() => {
                      const today = new Date().toISOString().split('T')[0];
                      handleChange(today);
                    }}
                    className="flex-1 px-2 py-1 text-xs text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight rounded transition-colors"
                  >
                    Today
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      const tomorrow = new Date();
                      tomorrow.setDate(tomorrow.getDate() + 1);
                      handleChange(tomorrow.toISOString().split('T')[0]);
                    }}
                    className="flex-1 px-2 py-1 text-xs text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight rounded transition-colors"
                  >
                    Tomorrow
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      const nextWeek = new Date();
                      nextWeek.setDate(nextWeek.getDate() + 7);
                      handleChange(nextWeek.toISOString().split('T')[0]);
                    }}
                    className="flex-1 px-2 py-1 text-xs text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight rounded transition-colors"
                  >
                    +1 week
                  </button>
                </div>
              </motion.div>
            </Popover.Content>
          </Popover.Portal>
        )}
      </AnimatePresence>
    </Popover.Root>
  );
}
