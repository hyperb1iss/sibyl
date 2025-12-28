'use client';

import * as SelectPrimitive from '@radix-ui/react-select';
import { type ComponentPropsWithoutRef, type ElementRef, forwardRef } from 'react';
import { Check, NavArrowDown } from '@/components/ui/icons';

const Select = SelectPrimitive.Root;
const SelectGroup = SelectPrimitive.Group;
const SelectValue = SelectPrimitive.Value;

const SelectTrigger = forwardRef<
  ElementRef<typeof SelectPrimitive.Trigger>,
  ComponentPropsWithoutRef<typeof SelectPrimitive.Trigger>
>(({ className = '', children, ...props }, ref) => (
  <SelectPrimitive.Trigger
    ref={ref}
    className={`
      inline-flex items-center justify-between gap-2
      px-3 py-2 w-full min-w-[180px]
      bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg
      text-sm text-sc-fg-primary
      transition-all duration-200 ease-out
      hover:border-sc-purple/50 hover:shadow-lg hover:shadow-sc-purple/10
      focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base
      disabled:opacity-50 disabled:cursor-not-allowed
      data-[placeholder]:text-sc-fg-muted
      ${className}
    `}
    {...props}
  >
    {children}
    <SelectPrimitive.Icon asChild>
      <NavArrowDown className="h-4 w-4 text-sc-fg-muted transition-transform duration-200 data-[state=open]:rotate-180" />
    </SelectPrimitive.Icon>
  </SelectPrimitive.Trigger>
));
SelectTrigger.displayName = SelectPrimitive.Trigger.displayName;

const SelectContent = forwardRef<
  ElementRef<typeof SelectPrimitive.Content>,
  ComponentPropsWithoutRef<typeof SelectPrimitive.Content>
>(({ className = '', children, position = 'popper', ...props }, ref) => (
  <SelectPrimitive.Portal>
    <SelectPrimitive.Content
      ref={ref}
      className={`
        relative z-50 min-w-[180px] overflow-hidden
        bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg shadow-xl
        animate-fade-in
        data-[state=open]:animate-in data-[state=closed]:animate-out
        data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0
        data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95
        data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2
        data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2
        ${position === 'popper' ? 'data-[side=bottom]:translate-y-1 data-[side=left]:-translate-x-1 data-[side=right]:translate-x-1 data-[side=top]:-translate-y-1' : ''}
        ${className}
      `}
      position={position}
      {...props}
    >
      <SelectPrimitive.Viewport
        className={`p-1 ${position === 'popper' ? 'h-[var(--radix-select-trigger-height)] w-full min-w-[var(--radix-select-trigger-width)]' : ''}`}
      >
        {children}
      </SelectPrimitive.Viewport>
    </SelectPrimitive.Content>
  </SelectPrimitive.Portal>
));
SelectContent.displayName = SelectPrimitive.Content.displayName;

const SelectLabel = forwardRef<
  ElementRef<typeof SelectPrimitive.Label>,
  ComponentPropsWithoutRef<typeof SelectPrimitive.Label>
>(({ className = '', ...props }, ref) => (
  <SelectPrimitive.Label
    ref={ref}
    className={`px-2 py-1.5 text-xs font-medium text-sc-fg-muted ${className}`}
    {...props}
  />
));
SelectLabel.displayName = SelectPrimitive.Label.displayName;

const SelectItem = forwardRef<
  ElementRef<typeof SelectPrimitive.Item>,
  ComponentPropsWithoutRef<typeof SelectPrimitive.Item>
>(({ className = '', children, ...props }, ref) => (
  <SelectPrimitive.Item
    ref={ref}
    className={`
      relative flex items-center gap-2
      px-2 py-1.5 pr-8 w-full
      text-sm text-sc-fg-primary rounded-md
      cursor-pointer select-none outline-none
      transition-colors duration-150
      hover:bg-sc-bg-highlight hover:text-sc-cyan
      focus:bg-sc-bg-highlight focus:text-sc-cyan
      data-[disabled]:pointer-events-none data-[disabled]:opacity-50
      data-[highlighted]:bg-sc-bg-highlight data-[highlighted]:text-sc-cyan
      ${className}
    `}
    {...props}
  >
    <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    <span className="absolute right-2 flex h-4 w-4 items-center justify-center">
      <SelectPrimitive.ItemIndicator>
        <Check className="h-4 w-4 text-sc-cyan" />
      </SelectPrimitive.ItemIndicator>
    </span>
  </SelectPrimitive.Item>
));
SelectItem.displayName = SelectPrimitive.Item.displayName;

const SelectSeparator = forwardRef<
  ElementRef<typeof SelectPrimitive.Separator>,
  ComponentPropsWithoutRef<typeof SelectPrimitive.Separator>
>(({ className = '', ...props }, ref) => (
  <SelectPrimitive.Separator
    ref={ref}
    className={`-mx-1 my-1 h-px bg-sc-fg-subtle/20 ${className}`}
    {...props}
  />
));
SelectSeparator.displayName = SelectPrimitive.Separator.displayName;

export {
  Select,
  SelectGroup,
  SelectValue,
  SelectTrigger,
  SelectContent,
  SelectLabel,
  SelectItem,
  SelectSeparator,
};
