'use client';

import { AnimatePresence, motion } from 'motion/react';
import { Xmark } from '@/components/ui/icons';
import { ENTITY_COLORS, type EntityType, getRelationshipConfig } from '@/lib/constants';
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
  const color = ENTITY_COLORS[type as EntityType] ?? '#8b85a0';
  const sizeConfig = sizes[size];

  return (
    <span
      className={`inline-flex items-center rounded font-medium capitalize border ${sizeConfig.classes} ${className}`}
      style={{
        backgroundColor: `${color}20`,
        color: color,
        borderColor: `${color}40`,
      }}
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
  size?: 'xs' | 'sm';
  className?: string;
}

export function RelationshipBadge({
  type,
  direction,
  size = 'xs',
  className = '',
}: RelationshipBadgeProps) {
  const config = getRelationshipConfig(type);
  const sizeClasses = size === 'xs' ? 'px-1.5 py-0.5 text-[10px]' : 'px-2 py-0.5 text-xs';

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
type StatusType = 'healthy' | 'unhealthy' | 'warning' | 'idle' | 'running';

interface StatusBadgeProps {
  status: StatusType;
  label?: string;
  pulse?: boolean;
}

const statusStyles: Record<StatusType, { bg: string; text: string; dot: string }> = {
  healthy: { bg: 'bg-sc-green/20', text: 'text-sc-green', dot: 'bg-sc-green' },
  unhealthy: { bg: 'bg-sc-red/20', text: 'text-sc-red', dot: 'bg-sc-red' },
  warning: { bg: 'bg-sc-yellow/20', text: 'text-sc-yellow', dot: 'bg-sc-yellow' },
  idle: { bg: 'bg-sc-green/20', text: 'text-sc-green', dot: 'bg-sc-green' },
  running: { bg: 'bg-sc-yellow/20', text: 'text-sc-yellow', dot: 'bg-sc-yellow' },
};

export function StatusBadge({ status, label, pulse = false }: StatusBadgeProps) {
  const style = statusStyles[status];
  const displayLabel = label ?? status.charAt(0).toUpperCase() + status.slice(1);

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
