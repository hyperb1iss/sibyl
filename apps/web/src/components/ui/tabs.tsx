'use client';

import * as TabsPrimitive from '@radix-ui/react-tabs';
import { type ComponentPropsWithoutRef, type ElementRef, forwardRef } from 'react';

// Tabs root
interface TabsProps extends ComponentPropsWithoutRef<typeof TabsPrimitive.Root> {
  variant?: 'underline' | 'pills' | 'enclosed';
}

const Tabs = forwardRef<ElementRef<typeof TabsPrimitive.Root>, TabsProps>(
  ({ className = '', variant = 'underline', ...props }, ref) => (
    <TabsPrimitive.Root ref={ref} data-variant={variant} className={`${className}`} {...props} />
  )
);
Tabs.displayName = TabsPrimitive.Root.displayName;

// Tabs list container
interface TabsListProps extends ComponentPropsWithoutRef<typeof TabsPrimitive.List> {
  fullWidth?: boolean;
}

const TabsList = forwardRef<ElementRef<typeof TabsPrimitive.List>, TabsListProps>(
  ({ className = '', fullWidth = false, ...props }, ref) => (
    <TabsPrimitive.List
      ref={ref}
      className={`
        inline-flex items-center gap-1 p-1

        /* Underline variant */
        [div[data-variant=underline]_&]:gap-0 [div[data-variant=underline]_&]:p-0
        [div[data-variant=underline]_&]:border-b [div[data-variant=underline]_&]:border-sc-fg-subtle/20

        /* Pills variant */
        [div[data-variant=pills]_&]:bg-sc-bg-highlight [div[data-variant=pills]_&]:rounded-lg

        /* Enclosed variant */
        [div[data-variant=enclosed]_&]:bg-sc-bg-highlight [div[data-variant=enclosed]_&]:rounded-t-lg
        [div[data-variant=enclosed]_&]:border-b-0

        ${fullWidth ? 'w-full' : ''}
        ${className}
      `}
      {...props}
    />
  )
);
TabsList.displayName = TabsPrimitive.List.displayName;

// Individual tab trigger
const TabsTrigger = forwardRef<
  ElementRef<typeof TabsPrimitive.Trigger>,
  ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(({ className = '', ...props }, ref) => (
  <TabsPrimitive.Trigger
    ref={ref}
    className={`
      inline-flex items-center justify-center gap-2
      px-4 py-2 text-sm font-medium
      transition-all duration-150
      text-sc-fg-muted

      hover:text-sc-fg-primary

      focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base

      disabled:pointer-events-none disabled:opacity-50

      /* Underline variant */
      [div[data-variant=underline]_&]:relative
      [div[data-variant=underline]_&]:rounded-none
      [div[data-variant=underline]_&]:border-b-2
      [div[data-variant=underline]_&]:border-transparent
      [div[data-variant=underline]_&]:-mb-px
      [div[data-variant=underline]_&]:data-[state=active]:border-sc-purple
      [div[data-variant=underline]_&]:data-[state=active]:text-sc-fg-primary

      /* Pills variant */
      [div[data-variant=pills]_&]:rounded-md
      [div[data-variant=pills]_&]:data-[state=active]:bg-sc-purple
      [div[data-variant=pills]_&]:data-[state=active]:text-white
      [div[data-variant=pills]_&]:data-[state=active]:shadow-sm

      /* Enclosed variant */
      [div[data-variant=enclosed]_&]:rounded-t-lg
      [div[data-variant=enclosed]_&]:border
      [div[data-variant=enclosed]_&]:border-transparent
      [div[data-variant=enclosed]_&]:border-b-0
      [div[data-variant=enclosed]_&]:data-[state=active]:bg-sc-bg-base
      [div[data-variant=enclosed]_&]:data-[state=active]:border-sc-fg-subtle/20
      [div[data-variant=enclosed]_&]:data-[state=active]:text-sc-fg-primary

      ${className}
    `}
    {...props}
  />
));
TabsTrigger.displayName = TabsPrimitive.Trigger.displayName;

// Tab content panel
const TabsContent = forwardRef<
  ElementRef<typeof TabsPrimitive.Content>,
  ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(({ className = '', ...props }, ref) => (
  <TabsPrimitive.Content
    ref={ref}
    className={`
      mt-4
      focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base

      /* Enclosed variant - connected to tabs */
      [div[data-variant=enclosed]_&]:mt-0
      [div[data-variant=enclosed]_&]:p-4
      [div[data-variant=enclosed]_&]:bg-sc-bg-base
      [div[data-variant=enclosed]_&]:border
      [div[data-variant=enclosed]_&]:border-sc-fg-subtle/20
      [div[data-variant=enclosed]_&]:rounded-b-lg
      [div[data-variant=enclosed]_&]:rounded-tr-lg

      ${className}
    `}
    {...props}
  />
));
TabsContent.displayName = TabsPrimitive.Content.displayName;

export { Tabs, TabsList, TabsTrigger, TabsContent };
