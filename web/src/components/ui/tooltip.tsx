'use client';

import { type ReactNode, useState } from 'react';
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

interface TooltipProps {
  content: ReactNode;
  children: ReactNode;
  position?: 'top' | 'bottom' | 'left' | 'right';
  delay?: number;
}

const positions = {
  top: 'bottom-full left-1/2 -translate-x-1/2 mb-2',
  bottom: 'top-full left-1/2 -translate-x-1/2 mt-2',
  left: 'right-full top-1/2 -translate-y-1/2 mr-2',
  right: 'left-full top-1/2 -translate-y-1/2 ml-2',
};

const arrows = {
  top: 'top-full left-1/2 -translate-x-1/2 border-l-transparent border-r-transparent border-b-transparent border-t-sc-bg-elevated',
  bottom:
    'bottom-full left-1/2 -translate-x-1/2 border-l-transparent border-r-transparent border-t-transparent border-b-sc-bg-elevated',
  left: 'left-full top-1/2 -translate-y-1/2 border-t-transparent border-b-transparent border-r-transparent border-l-sc-bg-elevated',
  right:
    'right-full top-1/2 -translate-y-1/2 border-t-transparent border-b-transparent border-l-transparent border-r-sc-bg-elevated',
};

export function Tooltip({ content, children, position = 'top', delay = 200 }: TooltipProps) {
  const [visible, setVisible] = useState(false);
  const [timeoutId, setTimeoutId] = useState<NodeJS.Timeout | null>(null);

  const showTooltip = () => {
    const id = setTimeout(() => setVisible(true), delay);
    setTimeoutId(id);
  };

  const hideTooltip = () => {
    if (timeoutId) clearTimeout(timeoutId);
    setVisible(false);
  };

  return (
    <div
      className="relative inline-flex"
      onMouseEnter={showTooltip}
      onMouseLeave={hideTooltip}
      onFocus={showTooltip}
      onBlur={hideTooltip}
      role="presentation"
    >
      {children}
      {visible && (
        <div
          className={`
            absolute z-50 px-2 py-1 text-xs text-sc-fg-primary
            bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg shadow-lg
            whitespace-nowrap animate-fade-in
            ${positions[position]}
          `}
          role="tooltip"
        >
          {content}
          <span className={`absolute w-0 h-0 border-4 ${arrows[position]}`} />
        </div>
      )}
    </div>
  );
}

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
    <Tooltip content={content} position="top">
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
