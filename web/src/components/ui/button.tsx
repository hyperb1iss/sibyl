import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { Spinner } from './spinner';

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
type ButtonSize = 'sm' | 'md' | 'lg';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  icon?: ReactNode;
  children: ReactNode;
  spark?: boolean; // Add spark hover effect
}

const variants: Record<ButtonVariant, string> = {
  primary:
    'bg-sc-purple text-white hover:bg-sc-purple/80 active:scale-[0.98] shadow-lg shadow-sc-purple/20 hover:shadow-xl hover:shadow-sc-purple/30',
  secondary:
    'bg-sc-bg-highlight border border-sc-fg-subtle/20 text-sc-fg-primary hover:border-sc-purple/50 hover:text-sc-purple hover:shadow-lg hover:shadow-sc-purple/10',
  ghost: 'bg-transparent text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight',
  danger:
    'bg-sc-red/20 text-sc-red border border-sc-red/30 hover:bg-sc-red/30 hover:border-sc-red/50 hover:shadow-lg hover:shadow-sc-red/20',
};

const sizes: Record<ButtonSize, string> = {
  sm: 'px-3 py-1.5 text-sm rounded-lg gap-1.5',
  md: 'px-4 py-2 text-sm rounded-lg gap-2',
  lg: 'px-6 py-3 text-base rounded-xl gap-2.5',
};

export function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  icon,
  type = 'button',
  disabled,
  children,
  className = '',
  spark = false,
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={`
        inline-flex items-center justify-center font-medium
        transition-all duration-200 ease-out
        disabled:opacity-50 disabled:cursor-not-allowed disabled:pointer-events-none
        focus-visible:outline focus-visible:outline-2 focus-visible:outline-sc-cyan focus-visible:outline-offset-2
        ${variants[variant]}
        ${sizes[size]}
        ${spark && !disabled && !loading ? 'hover-spark' : ''}
        ${className}
      `}
      {...props}
    >
      {loading ? (
        <>
          <Spinner size={size === 'lg' ? 'md' : 'sm'} color="current" />
          <span className="ml-1">{children}</span>
        </>
      ) : (
        <>
          {icon && <span className="flex-shrink-0">{icon}</span>}
          {children}
        </>
      )}
    </button>
  );
}

// Specialized button variants for common patterns
interface ColorButtonProps extends Omit<ButtonProps, 'variant'> {
  color: 'purple' | 'cyan' | 'coral' | 'yellow' | 'green' | 'red';
}

const colorStyles: Record<ColorButtonProps['color'], string> = {
  purple:
    'bg-sc-purple/20 text-sc-purple hover:bg-sc-purple/30 border border-sc-purple/30 hover:border-sc-purple/50 hover:shadow-lg hover:shadow-sc-purple/20',
  cyan: 'bg-sc-cyan/20 text-sc-cyan hover:bg-sc-cyan/30 border border-sc-cyan/30 hover:border-sc-cyan/50 hover:shadow-lg hover:shadow-sc-cyan/20',
  coral:
    'bg-sc-coral/20 text-sc-coral hover:bg-sc-coral/30 border border-sc-coral/30 hover:border-sc-coral/50 hover:shadow-lg hover:shadow-sc-coral/20',
  yellow:
    'bg-sc-yellow/20 text-sc-yellow hover:bg-sc-yellow/30 border border-sc-yellow/30 hover:border-sc-yellow/50 hover:shadow-lg hover:shadow-sc-yellow/20',
  green:
    'bg-sc-green/20 text-sc-green hover:bg-sc-green/30 border border-sc-green/30 hover:border-sc-green/50 hover:shadow-lg hover:shadow-sc-green/20',
  red: 'bg-sc-red/20 text-sc-red hover:bg-sc-red/30 border border-sc-red/30 hover:border-sc-red/50 hover:shadow-lg hover:shadow-sc-red/20',
};

export function ColorButton({
  color,
  size = 'md',
  loading = false,
  icon,
  type = 'button',
  disabled,
  children,
  className = '',
  spark = false,
  ...props
}: ColorButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={`
        inline-flex items-center justify-center font-medium
        transition-all duration-200 ease-out
        disabled:opacity-50 disabled:cursor-not-allowed
        active:scale-[0.98]
        focus-visible:outline focus-visible:outline-2 focus-visible:outline-sc-cyan focus-visible:outline-offset-2
        ${colorStyles[color]}
        ${sizes[size]}
        ${spark && !disabled && !loading ? 'hover-spark' : ''}
        ${className}
      `}
      {...props}
    >
      {loading ? (
        <>
          <Spinner size={size === 'lg' ? 'md' : 'sm'} color="current" />
          <span className="ml-1">{children}</span>
        </>
      ) : (
        <>
          {icon && <span className="flex-shrink-0">{icon}</span>}
          {children}
        </>
      )}
    </button>
  );
}

// Icon button for compact actions
interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  icon: ReactNode;
  label: string; // For accessibility
  size?: ButtonSize;
  variant?: 'default' | 'ghost';
}

export function IconButton({
  icon,
  label,
  size = 'md',
  variant = 'default',
  type = 'button',
  className = '',
  ...props
}: IconButtonProps) {
  const sizeClasses = {
    sm: 'w-7 h-7 text-sm',
    md: 'w-9 h-9 text-base',
    lg: 'w-11 h-11 text-lg',
  };

  const variantClasses = {
    default:
      'bg-sc-bg-highlight border border-sc-fg-subtle/20 hover:border-sc-purple/50 hover:text-sc-purple hover:shadow-lg hover:shadow-sc-purple/10',
    ghost: 'bg-transparent hover:bg-sc-bg-highlight',
  };

  return (
    <button
      type={type}
      className={`
        inline-flex items-center justify-center rounded-lg
        text-sc-fg-muted
        transition-all duration-200 ease-out
        active:scale-95
        disabled:opacity-50 disabled:cursor-not-allowed
        focus-visible:outline focus-visible:outline-2 focus-visible:outline-sc-cyan focus-visible:outline-offset-2
        ${sizeClasses[size]}
        ${variantClasses[variant]}
        ${className}
      `}
      aria-label={label}
      {...props}
    >
      {icon}
    </button>
  );
}

// Gradient button for special CTAs
interface GradientButtonProps extends Omit<ButtonProps, 'variant'> {
  gradient?: 'purple-cyan' | 'purple-coral' | 'cyan-coral';
}

const gradients = {
  'purple-cyan':
    'bg-gradient-to-r from-sc-purple to-sc-cyan hover:from-sc-purple/90 hover:to-sc-cyan/90',
  'purple-coral':
    'bg-gradient-to-r from-sc-purple to-sc-coral hover:from-sc-purple/90 hover:to-sc-coral/90',
  'cyan-coral':
    'bg-gradient-to-r from-sc-cyan to-sc-coral hover:from-sc-cyan/90 hover:to-sc-coral/90',
};

export function GradientButton({
  gradient = 'purple-cyan',
  size = 'md',
  loading = false,
  icon,
  type = 'button',
  disabled,
  children,
  className = '',
  spark: _spark,
  ...props
}: GradientButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={`
        inline-flex items-center justify-center font-medium
        text-white shadow-lg
        transition-all duration-200 ease-out
        active:scale-[0.98]
        disabled:opacity-50 disabled:cursor-not-allowed disabled:pointer-events-none
        focus-visible:outline focus-visible:outline-2 focus-visible:outline-sc-cyan focus-visible:outline-offset-2
        hover:shadow-xl hover-spark
        ${gradients[gradient]}
        ${sizes[size]}
        ${className}
      `}
      {...props}
    >
      {loading ? (
        <>
          <Spinner size={size === 'lg' ? 'md' : 'sm'} color="white" />
          <span className="ml-1">{children}</span>
        </>
      ) : (
        <>
          {icon && <span className="flex-shrink-0">{icon}</span>}
          {children}
        </>
      )}
    </button>
  );
}
