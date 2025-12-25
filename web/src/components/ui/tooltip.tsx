'use client';

import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import {
  type ComponentPropsWithoutRef,
  type ElementRef,
  forwardRef,
  type ReactNode,
  useState,
} from 'react';
import {
  AlertTriangle,
  BarChart3,
  Cube,
  Flash,
  InfoCircle,
  Search,
  Sparkles,
  WifiOff,
} from '@/components/ui/icons';

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

// Export primitives for advanced usage
export { TooltipProvider, TooltipRoot, TooltipTrigger, TooltipContent };

// Empty state component for when there's no data - with personality
interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
  variant?: 'default' | 'search' | 'data' | 'create';
}

const EMPTY_STATE_DEFAULTS = {
  search: {
    icon: <Search width={48} height={48} className="text-sc-cyan" />,
    floatingClass: 'animate-float',
  },
  data: {
    icon: <BarChart3 width={48} height={48} className="text-sc-purple" />,
    floatingClass: 'animate-wiggle',
  },
  create: {
    icon: <Sparkles width={48} height={48} className="text-sc-yellow" />,
    floatingClass: 'animate-bounce-in',
  },
  default: {
    icon: <Cube width={48} height={48} className="text-sc-coral" />,
    floatingClass: 'animate-float',
  },
};

export function EmptyState({
  icon,
  title,
  description,
  action,
  variant = 'default',
}: EmptyStateProps) {
  const defaults = EMPTY_STATE_DEFAULTS[variant];
  const displayIcon = icon ?? defaults.icon;

  return (
    <div className="text-center py-16 animate-fade-in">
      {displayIcon && (
        <div className={`text-6xl mb-4 opacity-80 ${defaults.floatingClass}`}>{displayIcon}</div>
      )}
      <p className="text-sc-fg-muted text-lg font-medium">{title}</p>
      {description && (
        <p className="text-sc-fg-subtle text-sm mt-2 max-w-md mx-auto">{description}</p>
      )}
      {action && <div className="mt-6 animate-slide-up">{action}</div>}
    </div>
  );
}

// Error state component - friendly and helpful
interface ErrorStateProps {
  title?: string;
  message: string;
  action?: ReactNode;
  variant?: 'error' | 'warning' | 'offline';
}

const ERROR_VARIANTS = {
  error: {
    icon: <AlertTriangle width={32} height={32} className="text-sc-red" />,
    title: 'Oops, something went sideways',
    color: 'text-sc-red',
    iconClass: 'animate-wiggle',
  },
  warning: {
    icon: <Flash width={32} height={32} className="text-sc-yellow" />,
    title: 'Heads up',
    color: 'text-sc-yellow',
    iconClass: 'animate-pulse',
  },
  offline: {
    icon: <WifiOff width={32} height={32} className="text-sc-coral" />,
    title: 'Connection lost',
    color: 'text-sc-coral',
    iconClass: 'animate-float',
  },
};

export function ErrorState({ title, message, action, variant = 'error' }: ErrorStateProps) {
  const variantConfig = ERROR_VARIANTS[variant];
  const displayTitle = title ?? variantConfig.title;

  return (
    <div className="text-center py-12 animate-fade-in">
      <div className={`text-4xl mb-4 ${variantConfig.iconClass}`}>{variantConfig.icon}</div>
      <p className={`text-lg font-medium ${variantConfig.color}`}>{displayTitle}</p>
      <p className="text-sc-fg-muted text-sm mt-1 max-w-md mx-auto">{message}</p>
      {action && <div className="mt-4 animate-slide-up">{action}</div>}
    </div>
  );
}

// Success celebration component
interface SuccessStateProps {
  title: string;
  message?: string;
  action?: ReactNode;
  celebratory?: boolean;
}

export function SuccessState({ title, message, action, celebratory = true }: SuccessStateProps) {
  return (
    <div className="text-center py-12 animate-bounce-in">
      <div className={`mb-4 flex justify-center ${celebratory ? 'success-sparkle' : ''}`}>
        <Sparkles width={48} height={48} className="text-sc-green" />
      </div>
      <p className="text-sc-green text-xl font-semibold gradient-text">{title}</p>
      {message && <p className="text-sc-fg-muted text-sm mt-2 max-w-md mx-auto">{message}</p>}
      {action && <div className="mt-6 animate-slide-up">{action}</div>}
    </div>
  );
}

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
    icon: <Sparkles width={20} height={20} className="text-sc-purple" />,
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
