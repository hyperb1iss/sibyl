'use client';

import * as CheckboxPrimitive from '@radix-ui/react-checkbox';
import { type ComponentPropsWithoutRef, type ElementRef, forwardRef, useId } from 'react';
import { Check } from '@/components/ui/icons';

interface CheckboxProps extends ComponentPropsWithoutRef<typeof CheckboxPrimitive.Root> {
  label?: string;
  description?: string;
}

const Checkbox = forwardRef<ElementRef<typeof CheckboxPrimitive.Root>, CheckboxProps>(
  ({ className = '', label, description, id: propId, ...props }, ref) => {
    const generatedId = useId();
    const id = propId ?? generatedId;

    const checkbox = (
      <CheckboxPrimitive.Root
        ref={ref}
        id={id}
        className={`
          peer h-5 w-5 shrink-0 rounded
          border border-sc-fg-subtle/30
          bg-sc-bg-highlight
          transition-all duration-150
          hover:border-sc-purple/50
          focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base
          disabled:cursor-not-allowed disabled:opacity-50
          data-[state=checked]:bg-sc-purple data-[state=checked]:border-sc-purple
          data-[state=indeterminate]:bg-sc-purple data-[state=indeterminate]:border-sc-purple
          ${className}
        `}
        {...props}
      >
        <CheckboxPrimitive.Indicator className="flex items-center justify-center text-white">
          {props.checked === 'indeterminate' ? (
            <span className="h-0.5 w-2.5 bg-current rounded-full" />
          ) : (
            <Check className="h-3.5 w-3.5" />
          )}
        </CheckboxPrimitive.Indicator>
      </CheckboxPrimitive.Root>
    );

    if (!label && !description) {
      return checkbox;
    }

    return (
      <div className="flex items-start gap-3">
        {checkbox}
        <div className="grid gap-0.5 leading-none">
          {label && (
            <label
              htmlFor={id}
              className="text-sm font-medium text-sc-fg-primary cursor-pointer peer-disabled:cursor-not-allowed peer-disabled:opacity-50"
            >
              {label}
            </label>
          )}
          {description && <p className="text-xs text-sc-fg-muted">{description}</p>}
        </div>
      </div>
    );
  }
);
Checkbox.displayName = CheckboxPrimitive.Root.displayName;

export { Checkbox };
