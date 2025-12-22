import { ENTITY_COLORS, type EntityType } from '@/lib/constants';
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
