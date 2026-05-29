'use client';

import { AnimatePresence, motion } from 'motion/react';
import { Check, RefreshDouble, Xmark } from '@/components/ui/icons';
import { getEntityStyles, getRelationshipConfig } from '@/lib/constants';
import { EntityIcon } from './entity-icon';

type BadgeSize = 'sm' | 'md' | 'lg';

const sizes: Record<BadgeSize, { classes: string; iconSize: number }> = {
  sm: { classes: 'px-2 py-0.5 text-xs gap-1', iconSize: 12 },
  md: { classes: 'px-2.5 py-1 text-sm gap-1.5', iconSize: 14 },
  lg: { classes: 'px-3 py-1.5 text-sm gap-2', iconSize: 16 },
};

// Entity type badge with SilkCircuit colors
interface EntityBadgeProps {
  type: string;
  size?: BadgeSize;
  showIcon?: boolean;
  className?: string;
}

export function EntityBadge({
  type,
  size = 'sm',
  showIcon = false,
  className = '',
}: EntityBadgeProps) {
  const sizeConfig = sizes[size];
  const styles = getEntityStyles(type);

  return (
    <span
      className={`inline-flex items-center rounded font-medium capitalize border ${sizeConfig.classes} ${styles.badge} ${className}`}
    >
      {showIcon && <EntityIcon type={type} size={sizeConfig.iconSize} className="opacity-80" />}
      {type.replace(/_/g, ' ')}
    </span>
  );
}

// Relationship type badge with SilkCircuit colors
interface RelationshipBadgeProps {
  type: string;
  direction?: 'outgoing' | 'incoming';
  size?: 'xs';
  className?: string;
}

const relationshipBadgeSizes = {
  xs: 'px-1.5 py-0.5 text-[10px]',
} as const;

export function RelationshipBadge({
  type,
  direction,
  size = 'xs',
  className = '',
}: RelationshipBadgeProps) {
  const config = getRelationshipConfig(type);
  const sizeClasses = relationshipBadgeSizes[size];

  return (
    <span
      className={`inline-flex items-center gap-1 rounded font-medium ${sizeClasses} ${className}`}
      style={{
        backgroundColor: `${config.color}15`,
        color: config.color,
      }}
      title={`${direction === 'incoming' ? '← ' : '→ '}${config.label}`}
    >
      {direction === 'incoming' && <span className="opacity-60">←</span>}
      {config.label}
      {direction === 'outgoing' && <span className="opacity-60">→</span>}
    </span>
  );
}

// Status indicator badges
type StatusType = 'healthy' | 'unhealthy' | 'warning' | 'idle' | 'running' | 'unknown';
type StatusValue = StatusType | boolean;

interface StatusBadgeProps {
  status: StatusValue;
  label?: string;
  pulse?: boolean;
  variant?: 'dot' | 'chip';
}

const statusStyles: Record<
  StatusType,
  { bg: string; text: string; dot: string; border: string; icon: React.ReactNode }
> = {
  healthy: {
    bg: 'bg-sc-green/20',
    text: 'text-sc-green',
    dot: 'bg-sc-green',
    border: 'border-sc-green/20',
    icon: <Check width={14} height={14} />,
  },
  unhealthy: {
    bg: 'bg-sc-red/20',
    text: 'text-sc-red',
    dot: 'bg-sc-red',
    border: 'border-sc-red/20',
    icon: <Xmark width={14} height={14} />,
  },
  warning: {
    bg: 'bg-sc-yellow/20',
    text: 'text-sc-yellow',
    dot: 'bg-sc-yellow',
    border: 'border-sc-yellow/20',
    icon: <RefreshDouble width={14} height={14} />,
  },
  idle: {
    bg: 'bg-sc-green/20',
    text: 'text-sc-green',
    dot: 'bg-sc-green',
    border: 'border-sc-green/20',
    icon: <RefreshDouble width={14} height={14} />,
  },
  running: {
    bg: 'bg-sc-yellow/20',
    text: 'text-sc-yellow',
    dot: 'bg-sc-yellow',
    border: 'border-sc-yellow/20',
    icon: <RefreshDouble width={14} height={14} />,
  },
  unknown: {
    bg: 'bg-sc-fg-subtle/10',
    text: 'text-sc-fg-muted',
    dot: 'bg-sc-fg-subtle',
    border: 'border-sc-fg-subtle/20',
    icon: <RefreshDouble width={14} height={14} />,
  },
};

function resolveStatus(status: StatusValue): StatusType {
  if (status === true || status === 'healthy') {
    return 'healthy';
  }

  if (status === false || status === 'unhealthy') {
    return 'unhealthy';
  }

  if (status === 'warning' || status === 'idle' || status === 'running' || status === 'unknown') {
    return status;
  }

  return 'unknown';
}

function formatStatusLabel(status: StatusType): string {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

export function StatusBadge({ status, label, pulse = false, variant = 'dot' }: StatusBadgeProps) {
  const resolvedStatus = resolveStatus(status);
  const style = statusStyles[resolvedStatus];
  const displayLabel = label ?? formatStatusLabel(resolvedStatus);

  if (variant === 'chip') {
    return (
      <span
        className={`
          inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs font-medium
          ${style.bg} ${style.text} ${style.border}
        `}
      >
        <span className={pulse ? 'animate-pulse' : ''}>{style.icon}</span>
        <span>{displayLabel}</span>
      </span>
    );
  }

  return (
    <span
      className={`
        inline-flex items-center gap-2 px-3 py-1 rounded-full text-sm font-medium
        ${style.bg} ${style.text}
      `}
    >
      <span
        className={`
          w-2 h-2 rounded-full ${style.dot}
          ${pulse ? 'animate-pulse' : ''}
        `}
      />
      {displayLabel}
    </span>
  );
}

// Removable badge with dismiss button
type BadgeColor = 'purple' | 'cyan' | 'coral' | 'yellow' | 'green' | 'red' | 'gray';

interface RemovableBadgeProps {
  children: React.ReactNode;
  onRemove: () => void;
  color?: BadgeColor;
  size?: BadgeSize;
  disabled?: boolean;
}

const badgeColors: Record<BadgeColor, { bg: string; text: string; border: string; hover: string }> =
  {
    purple: {
      bg: 'bg-sc-purple/20',
      text: 'text-sc-purple',
      border: 'border-sc-purple/30',
      hover: 'hover:bg-sc-purple/30',
    },
    cyan: {
      bg: 'bg-sc-cyan/20',
      text: 'text-sc-cyan',
      border: 'border-sc-cyan/30',
      hover: 'hover:bg-sc-cyan/30',
    },
    coral: {
      bg: 'bg-sc-coral/20',
      text: 'text-sc-coral',
      border: 'border-sc-coral/30',
      hover: 'hover:bg-sc-coral/30',
    },
    yellow: {
      bg: 'bg-sc-yellow/20',
      text: 'text-sc-yellow',
      border: 'border-sc-yellow/30',
      hover: 'hover:bg-sc-yellow/30',
    },
    green: {
      bg: 'bg-sc-green/20',
      text: 'text-sc-green',
      border: 'border-sc-green/30',
      hover: 'hover:bg-sc-green/30',
    },
    red: {
      bg: 'bg-sc-red/20',
      text: 'text-sc-red',
      border: 'border-sc-red/30',
      hover: 'hover:bg-sc-red/30',
    },
    gray: {
      bg: 'bg-sc-fg-subtle/10',
      text: 'text-sc-fg-muted',
      border: 'border-sc-fg-subtle/20',
      hover: 'hover:bg-sc-fg-subtle/20',
    },
  };

export function RemovableBadge({
  children,
  onRemove,
  color = 'gray',
  size = 'md',
  disabled = false,
}: RemovableBadgeProps) {
  const colorConfig = badgeColors[color];
  const sizeConfig = sizes[size];

  return (
    <motion.span
      initial={{ opacity: 0, scale: 0.8 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.8 }}
      transition={{ duration: 0.15 }}
      className={`
        inline-flex items-center rounded-full font-medium border
        ${sizeConfig.classes}
        ${colorConfig.bg} ${colorConfig.text} ${colorConfig.border}
        ${disabled ? 'opacity-50' : ''}
      `}
    >
      <span className="truncate max-w-[200px]">{children}</span>
      <button
        type="button"
        onClick={onRemove}
        disabled={disabled}
        className={`
          -mr-1 ml-1 p-0.5 rounded-full
          transition-colors duration-150
          ${colorConfig.hover}
          focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan
          disabled:cursor-not-allowed disabled:opacity-50
        `}
        aria-label="Remove"
      >
        <Xmark className="w-3 h-3" />
      </button>
    </motion.span>
  );
}

// Wrapper component for animated badge lists
interface BadgeListProps {
  children: React.ReactNode;
  className?: string;
}

export function BadgeList({ children, className = '' }: BadgeListProps) {
  return (
    <div className={`flex flex-wrap gap-2 ${className}`}>
      <AnimatePresence mode="popLayout">{children}</AnimatePresence>
    </div>
  );
}
