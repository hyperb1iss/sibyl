'use client';

import * as RadioGroupPrimitive from '@radix-ui/react-radio-group';
import { type ComponentPropsWithoutRef, type ElementRef, forwardRef } from 'react';
import { Circle } from '@/components/ui/icons';

interface RadioGroupProps extends ComponentPropsWithoutRef<typeof RadioGroupPrimitive.Root> {
  orientation?: 'horizontal' | 'vertical';
}

const RadioGroup = forwardRef<ElementRef<typeof RadioGroupPrimitive.Root>, RadioGroupProps>(
  ({ className = '', orientation = 'vertical', ...props }, ref) => (
    <RadioGroupPrimitive.Root
      ref={ref}
      className={`
        grid gap-3
        ${orientation === 'horizontal' ? 'grid-flow-col auto-cols-max' : ''}
        ${className}
      `}
      {...props}
    />
  )
);
RadioGroup.displayName = RadioGroupPrimitive.Root.displayName;

interface RadioGroupItemProps extends ComponentPropsWithoutRef<typeof RadioGroupPrimitive.Item> {
  label?: string;
  description?: string;
}

const RadioGroupItem = forwardRef<ElementRef<typeof RadioGroupPrimitive.Item>, RadioGroupItemProps>(
  ({ className = '', label, description, ...props }, ref) => {
    const radio = (
      <RadioGroupPrimitive.Item
        ref={ref}
        className={`
          aspect-square h-5 w-5 rounded-full
          border border-sc-fg-subtle/30
          bg-sc-bg-highlight
          transition-all duration-150
          hover:border-sc-purple/50
          focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base
          disabled:cursor-not-allowed disabled:opacity-50
          data-[state=checked]:border-sc-purple
          ${className}
        `}
        {...props}
      >
        <RadioGroupPrimitive.Indicator className="flex items-center justify-center">
          <Circle className="h-2.5 w-2.5 fill-sc-purple text-sc-purple" />
        </RadioGroupPrimitive.Indicator>
      </RadioGroupPrimitive.Item>
    );

    if (!label && !description) {
      return radio;
    }

    return (
      <div className="flex items-start gap-3">
        {radio}
        <div className="grid gap-0.5 leading-none">
          {label && (
            <label className="text-sm font-medium text-sc-fg-primary cursor-pointer">{label}</label>
          )}
          {description && <p className="text-xs text-sc-fg-muted">{description}</p>}
        </div>
      </div>
    );
  }
);
RadioGroupItem.displayName = RadioGroupPrimitive.Item.displayName;

export { RadioGroup, RadioGroupItem };
