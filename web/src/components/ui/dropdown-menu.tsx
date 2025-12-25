'use client';

import * as DropdownMenuPrimitive from '@radix-ui/react-dropdown-menu';
import { type ComponentPropsWithoutRef, type ElementRef, forwardRef } from 'react';
import { Check, Circle, NavArrowRight } from '@/components/ui/icons';

const DropdownMenu = DropdownMenuPrimitive.Root;
const DropdownMenuTrigger = DropdownMenuPrimitive.Trigger;
const DropdownMenuGroup = DropdownMenuPrimitive.Group;
const DropdownMenuPortal = DropdownMenuPrimitive.Portal;
const DropdownMenuSub = DropdownMenuPrimitive.Sub;
const DropdownMenuRadioGroup = DropdownMenuPrimitive.RadioGroup;

const DropdownMenuSubTrigger = forwardRef<
  ElementRef<typeof DropdownMenuPrimitive.SubTrigger>,
  ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.SubTrigger> & {
    inset?: boolean;
  }
>(({ className = '', inset, children, ...props }, ref) => (
  <DropdownMenuPrimitive.SubTrigger
    ref={ref}
    className={`
      flex items-center gap-2
      px-2 py-1.5 rounded-md
      text-sm text-sc-fg-primary
      cursor-pointer select-none outline-none
      transition-colors duration-150
      hover:bg-sc-bg-highlight hover:text-sc-cyan
      focus:bg-sc-bg-highlight focus:text-sc-cyan
      data-[state=open]:bg-sc-bg-highlight
      ${inset ? 'pl-8' : ''}
      ${className}
    `}
    {...props}
  >
    {children}
    <NavArrowRight className="ml-auto h-4 w-4" />
  </DropdownMenuPrimitive.SubTrigger>
));
DropdownMenuSubTrigger.displayName = DropdownMenuPrimitive.SubTrigger.displayName;

const DropdownMenuSubContent = forwardRef<
  ElementRef<typeof DropdownMenuPrimitive.SubContent>,
  ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.SubContent>
>(({ className = '', ...props }, ref) => (
  <DropdownMenuPrimitive.SubContent
    ref={ref}
    className={`
      z-50 min-w-[8rem] overflow-hidden
      bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg shadow-xl p-1
      data-[state=open]:animate-in data-[state=closed]:animate-out
      data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0
      data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95
      data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2
      data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2
      ${className}
    `}
    {...props}
  />
));
DropdownMenuSubContent.displayName = DropdownMenuPrimitive.SubContent.displayName;

const DropdownMenuContent = forwardRef<
  ElementRef<typeof DropdownMenuPrimitive.Content>,
  ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Content>
>(({ className = '', sideOffset = 4, ...props }, ref) => (
  <DropdownMenuPrimitive.Portal>
    <DropdownMenuPrimitive.Content
      ref={ref}
      sideOffset={sideOffset}
      className={`
        z-50 min-w-[8rem] overflow-hidden
        bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg shadow-xl p-1
        data-[state=open]:animate-in data-[state=closed]:animate-out
        data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0
        data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95
        data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2
        data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2
        ${className}
      `}
      {...props}
    />
  </DropdownMenuPrimitive.Portal>
));
DropdownMenuContent.displayName = DropdownMenuPrimitive.Content.displayName;

const DropdownMenuItem = forwardRef<
  ElementRef<typeof DropdownMenuPrimitive.Item>,
  ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Item> & {
    inset?: boolean;
    destructive?: boolean;
  }
>(({ className = '', inset, destructive, ...props }, ref) => (
  <DropdownMenuPrimitive.Item
    ref={ref}
    className={`
      relative flex items-center gap-2
      px-2 py-1.5 rounded-md
      text-sm cursor-pointer select-none outline-none
      transition-colors duration-150
      ${
        destructive
          ? 'text-sc-red hover:bg-sc-red/10 focus:bg-sc-red/10'
          : 'text-sc-fg-primary hover:bg-sc-bg-highlight hover:text-sc-cyan focus:bg-sc-bg-highlight focus:text-sc-cyan'
      }
      data-[disabled]:pointer-events-none data-[disabled]:opacity-50
      ${inset ? 'pl-8' : ''}
      ${className}
    `}
    {...props}
  />
));
DropdownMenuItem.displayName = DropdownMenuPrimitive.Item.displayName;

const DropdownMenuCheckboxItem = forwardRef<
  ElementRef<typeof DropdownMenuPrimitive.CheckboxItem>,
  ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.CheckboxItem>
>(({ className = '', children, checked, ...props }, ref) => (
  <DropdownMenuPrimitive.CheckboxItem
    ref={ref}
    className={`
      relative flex items-center gap-2
      pl-8 pr-2 py-1.5 rounded-md
      text-sm text-sc-fg-primary
      cursor-pointer select-none outline-none
      transition-colors duration-150
      hover:bg-sc-bg-highlight hover:text-sc-cyan
      focus:bg-sc-bg-highlight focus:text-sc-cyan
      data-[disabled]:pointer-events-none data-[disabled]:opacity-50
      ${className}
    `}
    checked={checked}
    {...props}
  >
    <span className="absolute left-2 flex h-4 w-4 items-center justify-center">
      <DropdownMenuPrimitive.ItemIndicator>
        <Check className="h-4 w-4 text-sc-cyan" />
      </DropdownMenuPrimitive.ItemIndicator>
    </span>
    {children}
  </DropdownMenuPrimitive.CheckboxItem>
));
DropdownMenuCheckboxItem.displayName = DropdownMenuPrimitive.CheckboxItem.displayName;

const DropdownMenuRadioItem = forwardRef<
  ElementRef<typeof DropdownMenuPrimitive.RadioItem>,
  ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.RadioItem>
>(({ className = '', children, ...props }, ref) => (
  <DropdownMenuPrimitive.RadioItem
    ref={ref}
    className={`
      relative flex items-center gap-2
      pl-8 pr-2 py-1.5 rounded-md
      text-sm text-sc-fg-primary
      cursor-pointer select-none outline-none
      transition-colors duration-150
      hover:bg-sc-bg-highlight hover:text-sc-cyan
      focus:bg-sc-bg-highlight focus:text-sc-cyan
      data-[disabled]:pointer-events-none data-[disabled]:opacity-50
      ${className}
    `}
    {...props}
  >
    <span className="absolute left-2 flex h-4 w-4 items-center justify-center">
      <DropdownMenuPrimitive.ItemIndicator>
        <Circle className="h-2 w-2 fill-sc-cyan text-sc-cyan" />
      </DropdownMenuPrimitive.ItemIndicator>
    </span>
    {children}
  </DropdownMenuPrimitive.RadioItem>
));
DropdownMenuRadioItem.displayName = DropdownMenuPrimitive.RadioItem.displayName;

const DropdownMenuLabel = forwardRef<
  ElementRef<typeof DropdownMenuPrimitive.Label>,
  ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Label> & {
    inset?: boolean;
  }
>(({ className = '', inset, ...props }, ref) => (
  <DropdownMenuPrimitive.Label
    ref={ref}
    className={`
      px-2 py-1.5
      text-xs font-medium text-sc-fg-muted
      ${inset ? 'pl-8' : ''}
      ${className}
    `}
    {...props}
  />
));
DropdownMenuLabel.displayName = DropdownMenuPrimitive.Label.displayName;

const DropdownMenuSeparator = forwardRef<
  ElementRef<typeof DropdownMenuPrimitive.Separator>,
  ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Separator>
>(({ className = '', ...props }, ref) => (
  <DropdownMenuPrimitive.Separator
    ref={ref}
    className={`-mx-1 my-1 h-px bg-sc-fg-subtle/20 ${className}`}
    {...props}
  />
));
DropdownMenuSeparator.displayName = DropdownMenuPrimitive.Separator.displayName;

const DropdownMenuShortcut = ({
  className = '',
  ...props
}: React.HTMLAttributes<HTMLSpanElement>) => (
  <span className={`ml-auto text-xs tracking-widest text-sc-fg-subtle ${className}`} {...props} />
);
DropdownMenuShortcut.displayName = 'DropdownMenuShortcut';

export {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuCheckboxItem,
  DropdownMenuRadioItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuGroup,
  DropdownMenuPortal,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuRadioGroup,
};
