'use client';

import * as AccordionPrimitive from '@radix-ui/react-accordion';
import { type ComponentPropsWithoutRef, type ElementRef, forwardRef } from 'react';
import { NavArrowDown } from '@/components/ui/icons';

// Accordion root - supports single or multiple open items
const Accordion = AccordionPrimitive.Root;

// Accordion item container
const AccordionItem = forwardRef<
  ElementRef<typeof AccordionPrimitive.Item>,
  ComponentPropsWithoutRef<typeof AccordionPrimitive.Item>
>(({ className = '', ...props }, ref) => (
  <AccordionPrimitive.Item
    ref={ref}
    className={`
      border-b border-sc-fg-subtle/20
      last:border-b-0
      ${className}
    `}
    {...props}
  />
));
AccordionItem.displayName = 'AccordionItem';

// Accordion trigger/header
interface AccordionTriggerProps
  extends ComponentPropsWithoutRef<typeof AccordionPrimitive.Trigger> {
  icon?: React.ReactNode;
}

const AccordionTrigger = forwardRef<
  ElementRef<typeof AccordionPrimitive.Trigger>,
  AccordionTriggerProps
>(({ className = '', children, icon, ...props }, ref) => (
  <AccordionPrimitive.Header className="flex">
    <AccordionPrimitive.Trigger
      ref={ref}
      className={`
        flex flex-1 items-center justify-between gap-4
        py-4 text-left text-sm font-medium
        text-sc-fg-primary
        transition-all duration-200
        hover:text-sc-purple

        focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base

        [&[data-state=open]>svg]:rotate-180
        [&[data-state=open]>.accordion-icon]:rotate-180

        ${className}
      `}
      {...props}
    >
      <div className="flex items-center gap-3">
        {icon && <span className="text-sc-purple flex-shrink-0">{icon}</span>}
        <span>{children}</span>
      </div>
      <NavArrowDown className="accordion-icon h-4 w-4 shrink-0 text-sc-fg-muted transition-transform duration-200" />
    </AccordionPrimitive.Trigger>
  </AccordionPrimitive.Header>
));
AccordionTrigger.displayName = AccordionPrimitive.Trigger.displayName;

// Accordion content panel with animation
const AccordionContent = forwardRef<
  ElementRef<typeof AccordionPrimitive.Content>,
  ComponentPropsWithoutRef<typeof AccordionPrimitive.Content>
>(({ className = '', children, ...props }, ref) => (
  <AccordionPrimitive.Content
    ref={ref}
    className="
      overflow-hidden text-sm text-sc-fg-muted
      data-[state=closed]:animate-accordion-up
      data-[state=open]:animate-accordion-down
    "
    {...props}
  >
    <div className={`pb-4 pt-0 ${className}`}>{children}</div>
  </AccordionPrimitive.Content>
));
AccordionContent.displayName = AccordionPrimitive.Content.displayName;

// Card-style accordion variant
interface AccordionCardProps {
  children: React.ReactNode;
  className?: string;
  type?: 'single' | 'multiple';
  defaultValue?: string | string[];
  collapsible?: boolean;
}

function AccordionCard({
  children,
  className = '',
  type = 'single',
  defaultValue,
  collapsible = true,
}: AccordionCardProps) {
  const rootProps =
    type === 'single'
      ? {
          type: 'single' as const,
          defaultValue: defaultValue as string | undefined,
          collapsible,
        }
      : {
          type: 'multiple' as const,
          defaultValue: defaultValue as string[] | undefined,
        };

  return (
    <Accordion
      {...rootProps}
      className={`
        rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-base
        divide-y divide-sc-fg-subtle/20
        ${className}
      `}
    >
      {children}
    </Accordion>
  );
}

// Card-style accordion item
const AccordionCardItem = forwardRef<
  ElementRef<typeof AccordionPrimitive.Item>,
  ComponentPropsWithoutRef<typeof AccordionPrimitive.Item>
>(({ className = '', ...props }, ref) => (
  <AccordionPrimitive.Item
    ref={ref}
    className={`
      first:rounded-t-xl last:rounded-b-xl
      ${className}
    `}
    {...props}
  />
));
AccordionCardItem.displayName = 'AccordionCardItem';

// Card-style trigger with padding
const AccordionCardTrigger = forwardRef<
  ElementRef<typeof AccordionPrimitive.Trigger>,
  AccordionTriggerProps
>(({ className = '', children, icon, ...props }, ref) => (
  <AccordionPrimitive.Header className="flex">
    <AccordionPrimitive.Trigger
      ref={ref}
      className={`
        flex flex-1 items-center justify-between gap-4
        px-6 py-4 text-left text-sm font-medium
        text-sc-fg-primary
        transition-all duration-200
        hover:bg-sc-bg-highlight/50

        focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-inset

        [&[data-state=open]>.accordion-icon]:rotate-180

        ${className}
      `}
      {...props}
    >
      <div className="flex items-center gap-3">
        {icon && <span className="text-sc-purple flex-shrink-0">{icon}</span>}
        <span>{children}</span>
      </div>
      <NavArrowDown className="accordion-icon h-4 w-4 shrink-0 text-sc-fg-muted transition-transform duration-200" />
    </AccordionPrimitive.Trigger>
  </AccordionPrimitive.Header>
));
AccordionCardTrigger.displayName = 'AccordionCardTrigger';

// Card-style content with padding
const AccordionCardContent = forwardRef<
  ElementRef<typeof AccordionPrimitive.Content>,
  ComponentPropsWithoutRef<typeof AccordionPrimitive.Content>
>(({ className = '', children, ...props }, ref) => (
  <AccordionPrimitive.Content
    ref={ref}
    className="
      overflow-hidden text-sm text-sc-fg-muted
      data-[state=closed]:animate-accordion-up
      data-[state=open]:animate-accordion-down
    "
    {...props}
  >
    <div className={`px-6 pb-4 ${className}`}>{children}</div>
  </AccordionPrimitive.Content>
));
AccordionCardContent.displayName = 'AccordionCardContent';

export {
  Accordion,
  AccordionItem,
  AccordionTrigger,
  AccordionContent,
  AccordionCard,
  AccordionCardItem,
  AccordionCardTrigger,
  AccordionCardContent,
};
