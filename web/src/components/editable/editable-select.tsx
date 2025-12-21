'use client';

import * as Popover from '@radix-ui/react-popover';
import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useState } from 'react';
import { Check } from '@/components/ui/icons';

interface Option {
  value: string;
  label: string;
  icon?: React.ReactNode;
  color?: string;
}

interface EditableSelectProps {
  value: string;
  options: Option[];
  onSave: (value: string) => Promise<void> | void;
  renderValue?: (option: Option | undefined) => React.ReactNode;
  disabled?: boolean;
  align?: 'start' | 'center' | 'end';
}

export function EditableSelect({
  value,
  options,
  onSave,
  renderValue,
  disabled = false,
  align = 'start',
}: EditableSelectProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  const currentOption = options.find(o => o.value === value);

  const handleSelect = useCallback(
    async (newValue: string) => {
      if (newValue !== value) {
        setIsSaving(true);
        try {
          await onSave(newValue);
        } finally {
          setIsSaving(false);
        }
      }
      setIsOpen(false);
    },
    [value, onSave]
  );

  const defaultRenderValue = (option: Option | undefined) => (
    <span className="inline-flex items-center gap-1.5">
      {option?.icon}
      {option?.label || value}
    </span>
  );

  return (
    <Popover.Root open={isOpen} onOpenChange={setIsOpen}>
      <Popover.Trigger asChild disabled={disabled || isSaving}>
        <button
          type="button"
          className={`
            inline-flex items-center gap-1 rounded-md px-1 -mx-1 py-0.5 -my-0.5
            transition-all duration-150
            ${disabled ? 'cursor-default opacity-60' : 'cursor-pointer hover:bg-sc-bg-highlight/50'}
            focus:outline-none focus:ring-2 focus:ring-sc-purple/30
          `}
        >
          {renderValue ? renderValue(currentOption) : defaultRenderValue(currentOption)}
          {isSaving && (
            <div className="w-3 h-3 border-2 border-current border-t-transparent rounded-full animate-spin ml-1" />
          )}
        </button>
      </Popover.Trigger>

      <AnimatePresence>
        {isOpen && (
          <Popover.Portal forceMount>
            <Popover.Content
              align={align}
              sideOffset={4}
              asChild
              onEscapeKeyDown={() => setIsOpen(false)}
            >
              <motion.div
                initial={{ opacity: 0, y: -4, scale: 0.96 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: -4, scale: 0.96 }}
                transition={{ duration: 0.15, ease: 'easeOut' }}
                className="z-50 min-w-[140px] bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-xl shadow-xl shadow-black/30 overflow-hidden"
              >
                <div className="py-1">
                  {options.map(option => {
                    const isSelected = option.value === value;
                    return (
                      <button
                        key={option.value}
                        type="button"
                        onClick={() => handleSelect(option.value)}
                        className={`
                          w-full flex items-center gap-2 px-3 py-2 text-sm text-left
                          transition-colors duration-100
                          ${isSelected ? 'bg-sc-bg-highlight' : 'hover:bg-sc-bg-highlight/50'}
                        `}
                      >
                        <span className={option.color || 'text-sc-fg-primary'}>{option.icon}</span>
                        <span
                          className={
                            isSelected ? 'text-sc-fg-primary font-medium' : 'text-sc-fg-muted'
                          }
                        >
                          {option.label}
                        </span>
                        {isSelected && <Check width={14} height={14} className="ml-auto text-sc-green" />}
                      </button>
                    );
                  })}
                </div>
              </motion.div>
            </Popover.Content>
          </Popover.Portal>
        )}
      </AnimatePresence>
    </Popover.Root>
  );
}
