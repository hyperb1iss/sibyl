import { ENTITY_COLORS, type EntityType } from '@/lib/constants';
import { EntityIcon } from './entity-icon';

interface ToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  size?: 'sm' | 'md';
  label?: string;
  description?: string;
}

const sizes: Record<
  NonNullable<ToggleProps['size']>,
  { track: string; thumb: string; translate: string }
> = {
  sm: { track: 'w-8 h-4', thumb: 'w-3 h-3', translate: 'translate-x-4' },
  md: { track: 'w-11 h-6', thumb: 'w-4 h-4', translate: 'translate-x-5' },
};

export function Toggle({
  checked,
  onChange,
  disabled = false,
  size = 'md',
  label,
  description,
}: ToggleProps) {
  const sizeStyles = sizes[size];

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`
          relative inline-flex flex-shrink-0 rounded-full transition-colors duration-200 ease-in-out
          focus:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-dark
          ${sizeStyles.track}
          ${checked ? 'bg-sc-purple' : 'bg-sc-bg-highlight'}
          ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
        `}
      >
        <span
          className={`
            absolute top-0.5 left-0.5 rounded-full bg-white shadow transform transition-transform duration-200 ease-in-out
            ${sizeStyles.thumb}
            ${checked ? sizeStyles.translate : 'translate-x-0'}
          `}
        />
      </button>
      {(label || description) && (
        <div>
          {label && (
            <span
              className={`text-sm font-medium ${disabled ? 'text-sc-fg-subtle' : 'text-sc-fg-primary'}`}
            >
              {label}
            </span>
          )}
          {description && <p className="text-xs text-sc-fg-subtle">{description}</p>}
        </div>
      )}
    </div>
  );
}

// Filter chip/toggle for multi-select scenarios
interface FilterChipProps {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  disabled?: boolean;
}

export function FilterChip({ active, onClick, children, disabled }: FilterChipProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`
        px-3 py-1.5 rounded-lg text-sm font-medium capitalize transition-all duration-150
        border
        ${
          active
            ? 'bg-sc-purple/20 text-sc-purple border-sc-purple/30 shadow-sm shadow-sc-purple/10'
            : 'bg-sc-bg-highlight text-sc-fg-muted border-sc-fg-subtle/10 hover:border-sc-fg-subtle/30 hover:text-sc-fg-primary'
        }
        ${disabled ? 'opacity-50 cursor-not-allowed' : ''}
      `}
    >
      {children}
    </button>
  );
}

// Tag styles matching TaskCard for visual consistency
const TAG_STYLES: Record<string, { bg: string; text: string; border: string; activeBg: string }> = {
  // Domain tags
  frontend: {
    bg: 'bg-sc-cyan/10',
    text: 'text-sc-cyan',
    border: 'border-sc-cyan/20',
    activeBg: 'bg-sc-cyan/25',
  },
  backend: {
    bg: 'bg-sc-purple/10',
    text: 'text-sc-purple',
    border: 'border-sc-purple/20',
    activeBg: 'bg-sc-purple/25',
  },
  database: {
    bg: 'bg-sc-coral/10',
    text: 'text-sc-coral',
    border: 'border-sc-coral/20',
    activeBg: 'bg-sc-coral/25',
  },
  devops: {
    bg: 'bg-sc-yellow/10',
    text: 'text-sc-yellow',
    border: 'border-sc-yellow/20',
    activeBg: 'bg-sc-yellow/25',
  },
  testing: {
    bg: 'bg-sc-green/10',
    text: 'text-sc-green',
    border: 'border-sc-green/20',
    activeBg: 'bg-sc-green/25',
  },
  security: {
    bg: 'bg-sc-red/10',
    text: 'text-sc-red',
    border: 'border-sc-red/20',
    activeBg: 'bg-sc-red/25',
  },
  performance: {
    bg: 'bg-sc-coral/10',
    text: 'text-sc-coral',
    border: 'border-sc-coral/20',
    activeBg: 'bg-sc-coral/25',
  },
  docs: {
    bg: 'bg-sc-fg-subtle/10',
    text: 'text-sc-fg-muted',
    border: 'border-sc-fg-subtle/20',
    activeBg: 'bg-sc-fg-subtle/25',
  },
  // Type tags
  feature: {
    bg: 'bg-sc-green/10',
    text: 'text-sc-green',
    border: 'border-sc-green/20',
    activeBg: 'bg-sc-green/25',
  },
  bug: {
    bg: 'bg-sc-red/10',
    text: 'text-sc-red',
    border: 'border-sc-red/20',
    activeBg: 'bg-sc-red/25',
  },
  refactor: {
    bg: 'bg-sc-purple/10',
    text: 'text-sc-purple',
    border: 'border-sc-purple/20',
    activeBg: 'bg-sc-purple/25',
  },
  chore: {
    bg: 'bg-sc-fg-subtle/10',
    text: 'text-sc-fg-muted',
    border: 'border-sc-fg-subtle/20',
    activeBg: 'bg-sc-fg-subtle/25',
  },
  research: {
    bg: 'bg-sc-cyan/10',
    text: 'text-sc-cyan',
    border: 'border-sc-cyan/20',
    activeBg: 'bg-sc-cyan/25',
  },
};

const DEFAULT_TAG_STYLE = {
  bg: 'bg-sc-bg-elevated',
  text: 'text-sc-fg-muted',
  border: 'border-sc-fg-subtle/20',
  activeBg: 'bg-sc-bg-highlight',
};

function getTagStyle(tag: string) {
  return TAG_STYLES[tag.toLowerCase()] || DEFAULT_TAG_STYLE;
}

// Tag chip for filtering by tag - styled to match task card tags
interface TagChipProps {
  tag: string;
  active: boolean;
  onClick: () => void;
  disabled?: boolean;
}

export function TagChip({ tag, active, onClick, disabled }: TagChipProps) {
  const style = getTagStyle(tag);

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`
        px-2.5 py-1 rounded-full text-xs font-medium transition-all duration-150
        border
        ${active ? style.activeBg : style.bg}
        ${style.text}
        ${active ? `${style.border} ring-1 ring-current/30` : style.border}
        ${active ? 'shadow-sm' : ''}
        hover:scale-105
        ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
      `}
    >
      {tag}
    </button>
  );
}

// Entity type filter chip with SilkCircuit colors
interface EntityTypeChipProps {
  entityType: string;
  active: boolean;
  onClick: () => void;
  count?: number;
  disabled?: boolean;
}

export function EntityTypeChip({
  entityType,
  active,
  onClick,
  count,
  disabled,
}: EntityTypeChipProps) {
  const color = ENTITY_COLORS[entityType as EntityType] ?? '#8b85a0';

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`
        group relative px-3 py-1.5 rounded-lg text-sm font-medium capitalize
        transition-all duration-200 border overflow-hidden
        ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
      `}
      style={{
        background: active
          ? `linear-gradient(135deg, ${color}30 0%, ${color}15 100%)`
          : `linear-gradient(135deg, ${color}12 0%, transparent 100%)`,
        borderColor: active ? `${color}50` : `${color}25`,
        color: active ? color : undefined,
        boxShadow: active ? `0 0 16px ${color}30, 0 0 4px ${color}20` : `0 0 0 1px ${color}08`,
      }}
      onMouseEnter={e => {
        if (!active) {
          e.currentTarget.style.background = `linear-gradient(135deg, ${color}20 0%, ${color}08 100%)`;
          e.currentTarget.style.borderColor = `${color}40`;
          e.currentTarget.style.boxShadow = `0 0 12px ${color}20`;
        }
      }}
      onMouseLeave={e => {
        if (!active) {
          e.currentTarget.style.background = `linear-gradient(135deg, ${color}12 0%, transparent 100%)`;
          e.currentTarget.style.borderColor = `${color}25`;
          e.currentTarget.style.boxShadow = `0 0 0 1px ${color}08`;
        }
      }}
    >
      <span className="relative flex items-center gap-1.5">
        <span className="transition-transform duration-200 group-hover:scale-110" style={{ color }}>
          <EntityIcon type={entityType} size={14} />
        </span>
        <span style={{ color: active ? color : 'var(--sc-fg-muted)' }}>
          {entityType.replace(/_/g, ' ')}
        </span>
        {count !== undefined && count > 0 && (
          <span
            className="ml-0.5 text-xs px-1.5 py-0.5 rounded-full font-semibold"
            style={{
              backgroundColor: active ? `${color}30` : `${color}15`,
              color: active ? color : `${color}cc`,
            }}
          >
            {count}
          </span>
        )}
      </span>
    </button>
  );
}
