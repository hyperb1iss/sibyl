import type { EntityType } from '@/lib/constants';
import { ENTITY_STYLES } from '@/lib/constants';

type BadgeSize = 'sm' | 'md';

const sizes: Record<BadgeSize, string> = {
  sm: 'px-2 py-0.5 text-xs',
  md: 'px-3 py-1 text-sm',
};

// Entity type badge with SilkCircuit colors
interface EntityBadgeProps {
  type: string;
  size?: BadgeSize;
  className?: string;
}

export function EntityBadge({ type, size = 'sm', className = '' }: EntityBadgeProps) {
  const style = ENTITY_STYLES[type as EntityType] ?? ENTITY_STYLES.knowledge_source;

  return (
    <span
      className={`
        inline-flex items-center rounded font-medium capitalize border
        ${sizes[size]}
        ${style.badge}
        ${className}
      `}
    >
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
