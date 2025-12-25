'use client';

import * as SwitchPrimitive from '@radix-ui/react-switch';
import { type ComponentPropsWithoutRef, type ElementRef, forwardRef, useId } from 'react';

interface SwitchProps extends ComponentPropsWithoutRef<typeof SwitchPrimitive.Root> {
  label?: string;
  description?: string;
  size?: 'sm' | 'md' | 'lg';
}

const sizes = {
  sm: {
    track: 'h-4 w-7',
    thumb: 'h-3 w-3 data-[state=checked]:translate-x-3',
  },
  md: {
    track: 'h-5 w-9',
    thumb: 'h-4 w-4 data-[state=checked]:translate-x-4',
  },
  lg: {
    track: 'h-6 w-11',
    thumb: 'h-5 w-5 data-[state=checked]:translate-x-5',
  },
};

const Switch = forwardRef<ElementRef<typeof SwitchPrimitive.Root>, SwitchProps>(
  ({ className = '', label, description, size = 'md', id: propId, ...props }, ref) => {
    const generatedId = useId();
    const id = propId ?? generatedId;
    const sizeConfig = sizes[size];

    const switchElement = (
      <SwitchPrimitive.Root
        ref={ref}
        id={id}
        className={`
          peer inline-flex shrink-0 cursor-pointer items-center rounded-full
          border-2 border-transparent
          transition-colors duration-200
          focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base
          disabled:cursor-not-allowed disabled:opacity-50
          data-[state=unchecked]:bg-sc-bg-highlight
          data-[state=checked]:bg-sc-purple
          ${sizeConfig.track}
          ${className}
        `}
        {...props}
      >
        <SwitchPrimitive.Thumb
          className={`
            pointer-events-none block rounded-full bg-white shadow-lg
            ring-0 transition-transform duration-200
            data-[state=unchecked]:translate-x-0
            ${sizeConfig.thumb}
          `}
        />
      </SwitchPrimitive.Root>
    );

    if (!label && !description) {
      return switchElement;
    }

    return (
      <div className="flex items-center gap-3">
        {switchElement}
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
Switch.displayName = SwitchPrimitive.Root.displayName;

export { Switch };
