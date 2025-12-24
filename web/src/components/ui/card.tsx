import type { HTMLAttributes, ReactNode } from 'react';
import { AlertTriangle, Flash, InfoCircle, Sparkles } from '@/components/ui/icons';

type CardVariant = 'default' | 'elevated' | 'interactive' | 'bordered';

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  variant?: CardVariant;
  glow?: boolean;
  gradientBorder?: boolean;
  children: ReactNode;
}

const variants: Record<CardVariant, string> = {
  default: 'bg-sc-bg-base border border-sc-fg-subtle/20',
  elevated: 'bg-sc-bg-elevated border border-sc-fg-subtle/10 shadow-xl shadow-black/20',
  interactive:
    'bg-sc-bg-base border border-sc-fg-subtle/20 hover:border-sc-purple/30 transition-all duration-200 cursor-pointer hover:shadow-lg hover:shadow-sc-purple/5 active:scale-[0.99]',
  bordered: 'bg-transparent border-2 border-sc-fg-subtle/30',
};

export function Card({
  variant = 'default',
  glow = false,
  gradientBorder = false,
  children,
  className = '',
  ...props
}: CardProps) {
  return (
    <div
      className={`
        rounded-xl p-6
        ${variants[variant]}
        ${glow ? 'animate-pulse-glow' : ''}
        ${gradientBorder ? 'gradient-border' : ''}
        ${className}
      `}
      {...props}
    >
      {children}
    </div>
  );
}

interface CardHeaderProps {
  title: string;
  description?: string;
  action?: ReactNode;
  icon?: ReactNode;
}

export function CardHeader({ title, description, action, icon }: CardHeaderProps) {
  return (
    <div className="flex items-start justify-between gap-4 mb-4">
      <div className="flex items-start gap-3">
        {icon && <div className="flex-shrink-0 text-sc-purple text-xl">{icon}</div>}
        <div>
          <h3 className="text-lg font-semibold text-sc-fg-primary">{title}</h3>
          {description && <p className="text-sm text-sc-fg-muted mt-0.5">{description}</p>}
        </div>
      </div>
      {action && <div className="flex-shrink-0">{action}</div>}
    </div>
  );
}

interface StatCardProps {
  label: string;
  value: string | number;
  icon?: ReactNode;
  trend?: 'up' | 'down' | 'neutral';
  sublabel?: string;
  loading?: boolean;
}

export function StatCard({ label, value, icon, trend, sublabel, loading }: StatCardProps) {
  const trendColors = {
    up: 'text-sc-green',
    down: 'text-sc-red',
    neutral: 'text-sc-fg-subtle',
  };

  return (
    <Card variant="default" className="relative overflow-hidden group hover-spark">
      {/* Subtle gradient accent on hover */}
      <div className="absolute inset-0 bg-gradient-to-br from-sc-purple/0 to-sc-purple/5 opacity-0 group-hover:opacity-100 transition-opacity duration-300" />

      <div className="relative">
        <div className="flex items-center justify-between mb-4">
          <h4 className="text-sm font-medium text-sc-fg-muted">{label}</h4>
          {icon && (
            <span className="text-sc-purple group-hover:scale-110 transition-transform duration-200">
              {icon}
            </span>
          )}
        </div>
        <div className="text-2xl font-bold text-sc-fg-primary">
          {loading ? (
            <div className="h-8 w-16 bg-sc-bg-highlight rounded animate-shimmer" />
          ) : (
            <span className="animate-fade-in">{value}</span>
          )}
        </div>
        {sublabel && (
          <p className={`text-sm mt-1 ${trend ? trendColors[trend] : 'text-sc-fg-subtle'}`}>
            {sublabel}
          </p>
        )}
      </div>
    </Card>
  );
}

// Feature card with icon and description
interface FeatureCardProps {
  icon: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
  highlight?: boolean;
}

export function FeatureCard({
  icon,
  title,
  description,
  action,
  highlight = false,
}: FeatureCardProps) {
  return (
    <Card variant="interactive" gradientBorder={highlight} className="group">
      <div className="flex flex-col items-start gap-4">
        <div className="text-3xl group-hover:scale-110 group-hover:rotate-3 transition-all duration-300">
          {icon}
        </div>
        <div className="flex-1">
          <h3 className="text-lg font-semibold text-sc-fg-primary mb-2 group-hover:text-sc-purple transition-colors">
            {title}
          </h3>
          <p className="text-sm text-sc-fg-muted">{description}</p>
        </div>
        {action && <div className="mt-2 animate-slide-up">{action}</div>}
      </div>
    </Card>
  );
}

// Metric card with progress indicator
interface MetricCardProps {
  label: string;
  current: number;
  total?: number;
  unit?: string;
  color?: 'purple' | 'cyan' | 'coral' | 'green' | 'yellow';
  icon?: ReactNode;
}

const metricColors = {
  purple: {
    bg: 'bg-sc-purple/20',
    border: 'border-sc-purple/30',
    text: 'text-sc-purple',
    bar: 'bg-sc-purple',
  },
  cyan: {
    bg: 'bg-sc-cyan/20',
    border: 'border-sc-cyan/30',
    text: 'text-sc-cyan',
    bar: 'bg-sc-cyan',
  },
  coral: {
    bg: 'bg-sc-coral/20',
    border: 'border-sc-coral/30',
    text: 'text-sc-coral',
    bar: 'bg-sc-coral',
  },
  green: {
    bg: 'bg-sc-green/20',
    border: 'border-sc-green/30',
    text: 'text-sc-green',
    bar: 'bg-sc-green',
  },
  yellow: {
    bg: 'bg-sc-yellow/20',
    border: 'border-sc-yellow/30',
    text: 'text-sc-yellow',
    bar: 'bg-sc-yellow',
  },
};

export function MetricCard({
  label,
  current,
  total,
  unit = '',
  color = 'purple',
  icon,
}: MetricCardProps) {
  const colorConfig = metricColors[color];
  const percentage = total ? (current / total) * 100 : undefined;

  return (
    <div
      className={`
        rounded-xl p-4 border
        ${colorConfig.bg} ${colorConfig.border}
        hover:shadow-lg transition-all duration-200
        group
      `}
    >
      <div className="flex items-center justify-between mb-3">
        <span className="text-sm font-medium text-sc-fg-muted">{label}</span>
        {icon && (
          <span
            className={`${colorConfig.text} group-hover:scale-110 transition-transform duration-200`}
          >
            {icon}
          </span>
        )}
      </div>
      <div className={`text-2xl font-bold ${colorConfig.text} mb-2 animate-fade-in`}>
        {current}
        {unit}
        {total && (
          <span className="text-sm text-sc-fg-subtle ml-1">
            / {total}
            {unit}
          </span>
        )}
      </div>
      {percentage !== undefined && (
        <div className="w-full h-1.5 bg-sc-bg-highlight rounded-full overflow-hidden">
          <div
            className={`h-full ${colorConfig.bar} transition-all duration-500 ease-out rounded-full`}
            style={{ width: `${Math.min(percentage, 100)}%` }}
          />
        </div>
      )}
    </div>
  );
}

// Notification card with dismiss
interface NotificationCardProps {
  title: string;
  message: string;
  type?: 'info' | 'success' | 'warning' | 'error';
  action?: ReactNode;
  onDismiss?: () => void;
}

const notificationStyles = {
  info: {
    icon: <InfoCircle width={20} height={20} className="text-sc-cyan" />,
    bg: 'bg-sc-cyan/10',
    border: 'border-sc-cyan/30',
    text: 'text-sc-cyan',
  },
  success: {
    icon: <Sparkles width={20} height={20} className="text-sc-green" />,
    bg: 'bg-sc-green/10',
    border: 'border-sc-green/30',
    text: 'text-sc-green',
  },
  warning: {
    icon: <Flash width={20} height={20} className="text-sc-yellow" />,
    bg: 'bg-sc-yellow/10',
    border: 'border-sc-yellow/30',
    text: 'text-sc-yellow',
  },
  error: {
    icon: <AlertTriangle width={20} height={20} className="text-sc-red" />,
    bg: 'bg-sc-red/10',
    border: 'border-sc-red/30',
    text: 'text-sc-red',
  },
};

export function NotificationCard({
  title,
  message,
  type = 'info',
  action,
  onDismiss,
}: NotificationCardProps) {
  const style = notificationStyles[type];

  return (
    <div
      className={`
        rounded-xl p-4 border animate-slide-up
        ${style.bg} ${style.border}
      `}
    >
      <div className="flex items-start gap-3">
        <span className="text-2xl flex-shrink-0 animate-bounce-in">{style.icon}</span>
        <div className="flex-1 min-w-0">
          <h4 className={`font-semibold ${style.text} mb-1`}>{title}</h4>
          <p className="text-sm text-sc-fg-muted">{message}</p>
          {action && <div className="mt-3">{action}</div>}
        </div>
        {onDismiss && (
          <button
            type="button"
            onClick={onDismiss}
            className="flex-shrink-0 text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
            aria-label="Dismiss notification"
          >
            ✕
          </button>
        )}
      </div>
    </div>
  );
}
