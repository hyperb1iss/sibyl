'use client';

import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import {
  type ComponentPropsWithoutRef,
  type ElementRef,
  forwardRef,
  type ReactNode,
  useState,
} from 'react';
import { Flash, InfoCircle, LightBulb } from '@/components/ui/icons';

// Radix Tooltip primitives with SilkCircuit styling
const TooltipProvider = TooltipPrimitive.Provider;
const TooltipRoot = TooltipPrimitive.Root;
const TooltipTrigger = TooltipPrimitive.Trigger;

const TooltipContent = forwardRef<
  ElementRef<typeof TooltipPrimitive.Content>,
  ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(({ className = '', sideOffset = 4, ...props }, ref) => (
  <TooltipPrimitive.Portal>
    <TooltipPrimitive.Content
      ref={ref}
      sideOffset={sideOffset}
      className={`
        z-50 overflow-hidden
        px-3 py-1.5
        text-xs text-sc-fg-primary
        bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg shadow-xl
        animate-in fade-in-0 zoom-in-95
        data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95
        data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2
        data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2
        ${className}
      `}
      {...props}
    />
  </TooltipPrimitive.Portal>
));
TooltipContent.displayName = TooltipPrimitive.Content.displayName;

// Simple Tooltip wrapper for common use cases
interface TooltipProps {
  content: ReactNode;
  children: ReactNode;
  side?: 'top' | 'bottom' | 'left' | 'right';
  delay?: number;
}

export function Tooltip({ content, children, side = 'top', delay = 200 }: TooltipProps) {
  return (
    <TooltipProvider delayDuration={delay}>
      <TooltipRoot>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent side={side}>{content}</TooltipContent>
      </TooltipRoot>
    </TooltipProvider>
  );
}

// EmptyState, ErrorState, and SuccessState moved to empty-state.tsx with the
// rest of the feedback components; re-exported here so existing imports keep working.
export { EmptyState, ErrorState, SuccessState } from './empty-state';
// Export primitives for advanced usage
export { TooltipContent, TooltipProvider, TooltipRoot, TooltipTrigger };

// Info/help tooltip component
interface InfoTooltipProps {
  content: ReactNode;
  size?: 'sm' | 'md';
}

export function InfoTooltip({ content, size = 'sm' }: InfoTooltipProps) {
  const sizeClasses = {
    sm: 'w-3.5 h-3.5 text-[10px]',
    md: 'w-4 h-4 text-xs',
  };

  return (
    <Tooltip content={content} side="top">
      <button
        type="button"
        className={`
          inline-flex items-center justify-center
          ${sizeClasses[size]}
          rounded-full
          bg-sc-bg-highlight
          border border-sc-fg-subtle/30
          text-sc-fg-muted
          hover:text-sc-cyan
          hover:border-sc-cyan/50
          hover:bg-sc-bg-elevated
          transition-all duration-200
          cursor-help
        `}
        aria-label="More information"
      >
        ?
      </button>
    </Tooltip>
  );
}

// Contextual hint component - subtle guidance
interface HintProps {
  children: ReactNode;
  icon?: ReactNode;
  variant?: 'info' | 'tip' | 'warning';
  dismissible?: boolean;
  onDismiss?: () => void;
}

const HINT_VARIANTS = {
  info: {
    icon: <InfoCircle width={20} height={20} className="text-sc-cyan" />,
    bg: 'bg-sc-cyan/10',
    border: 'border-sc-cyan/30',
    text: 'text-sc-cyan',
  },
  tip: {
    icon: <LightBulb width={20} height={20} className="text-sc-purple" />,
    bg: 'bg-sc-purple/10',
    border: 'border-sc-purple/30',
    text: 'text-sc-purple',
  },
  warning: {
    icon: <Flash width={20} height={20} className="text-sc-yellow" />,
    bg: 'bg-sc-yellow/10',
    border: 'border-sc-yellow/30',
    text: 'text-sc-yellow',
  },
};

export function Hint({
  children,
  icon,
  variant = 'tip',
  dismissible = false,
  onDismiss,
}: HintProps) {
  const [visible, setVisible] = useState(true);
  const variantConfig = HINT_VARIANTS[variant];
  const displayIcon = icon ?? variantConfig.icon;

  if (!visible) return null;

  const handleDismiss = () => {
    setVisible(false);
    onDismiss?.();
  };

  return (
    <div
      className={`
        flex items-start gap-3 p-3 rounded-lg border animate-slide-up
        ${variantConfig.bg} ${variantConfig.border}
      `}
    >
      {displayIcon && <span className="flex-shrink-0 animate-glow-pulse">{displayIcon}</span>}
      <div className="flex-1 text-sm text-sc-fg-primary">{children}</div>
      {dismissible && (
        <button
          type="button"
          onClick={handleDismiss}
          className="flex-shrink-0 text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
          aria-label="Dismiss"
        >
          ✕
        </button>
      )}
    </div>
  );
}
