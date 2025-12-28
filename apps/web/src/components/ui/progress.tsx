interface ProgressProps {
  value: number; // 0-100 or 0-1
  max?: number;
  size?: 'sm' | 'md' | 'lg';
  color?: 'purple' | 'cyan' | 'green' | 'yellow' | 'red';
  showLabel?: boolean;
  animate?: boolean;
  className?: string;
}

const sizes: Record<NonNullable<ProgressProps['size']>, string> = {
  sm: 'h-1',
  md: 'h-2',
  lg: 'h-3',
};

const colors: Record<NonNullable<ProgressProps['color']>, string> = {
  purple: 'bg-sc-purple',
  cyan: 'bg-sc-cyan',
  green: 'bg-sc-green',
  yellow: 'bg-sc-yellow',
  red: 'bg-sc-red',
};

export function Progress({
  value,
  max = 100,
  size = 'md',
  color = 'purple',
  showLabel = false,
  animate = true,
  className = '',
}: ProgressProps) {
  // Handle both 0-1 and 0-100 ranges
  const normalizedValue = value > 1 ? value : value * 100;
  const percentage = Math.min(Math.max(normalizedValue, 0), max);
  const percentDisplay = Math.round((percentage / max) * 100);

  return (
    <div className={className}>
      {showLabel && (
        <div className="flex justify-between text-sm text-sc-fg-muted mb-2">
          <span>Progress</span>
          <span>{percentDisplay}%</span>
        </div>
      )}
      <div className={`bg-sc-bg-highlight rounded-full overflow-hidden ${sizes[size]}`}>
        <div
          className={`
            h-full rounded-full
            ${colors[color]}
            ${animate ? 'transition-all duration-500 ease-out' : ''}
          `}
          style={{ width: `${percentDisplay}%` }}
          role="progressbar"
          aria-valuenow={percentage}
          aria-valuemin={0}
          aria-valuemax={max}
        />
      </div>
    </div>
  );
}

// Score bar (horizontal mini progress for search results, etc.)
interface ScoreBarProps {
  score: number; // 0-1
  size?: 'sm' | 'md';
  className?: string;
}

export function ScoreBar({ score, size = 'sm', className = '' }: ScoreBarProps) {
  const percentage = Math.round(score * 100);
  const width = size === 'sm' ? 'w-12' : 'w-16';
  const height = size === 'sm' ? 'h-1' : 'h-1.5';

  return (
    <div
      className={`${width} ${height} bg-sc-bg-highlight rounded-full overflow-hidden ${className}`}
    >
      <div
        className="h-full bg-sc-purple rounded-full transition-all"
        style={{ width: `${percentage}%` }}
      />
    </div>
  );
}

// Circular progress (for more dramatic loading states)
interface CircularProgressProps {
  value: number;
  size?: number;
  strokeWidth?: number;
  color?: 'purple' | 'cyan' | 'green';
}

export function CircularProgress({
  value,
  size = 48,
  strokeWidth = 4,
  color = 'purple',
}: CircularProgressProps) {
  const radius = (size - strokeWidth) / 2;
  const circumference = radius * 2 * Math.PI;
  const offset = circumference - (value / 100) * circumference;

  const colorClass = colors[color];

  return (
    <svg
      width={size}
      height={size}
      className="transform -rotate-90"
      role="img"
      aria-label={`Progress: ${value}%`}
    >
      {/* Background circle */}
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke="currentColor"
        strokeWidth={strokeWidth}
        className="text-sc-bg-highlight"
      />
      {/* Progress circle */}
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke="currentColor"
        strokeWidth={strokeWidth}
        strokeDasharray={circumference}
        strokeDashoffset={offset}
        strokeLinecap="round"
        className={`${colorClass} transition-all duration-500 ease-out`}
        style={{ color: `var(--sc-${color})` }}
      />
    </svg>
  );
}
