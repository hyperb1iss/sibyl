'use client';

import type { HTMLAttributes, ReactNode } from 'react';

type CardVariant =
  | 'default'
  | 'elevated'
  | 'interactive'
  | 'bordered'
  | 'error'
  | 'warning'
  | 'success';

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  variant?: CardVariant;
  glow?: boolean;
  gradientBorder?: boolean;
  children: ReactNode;
}

const variants: Record<CardVariant, string> = {
  default: 'bg-sc-bg-base border border-sc-fg-subtle/30 shadow-card',
  elevated: 'bg-sc-bg-elevated border border-sc-fg-subtle/20 shadow-card-elevated',
  interactive:
    'bg-sc-bg-base border border-sc-fg-subtle/30 shadow-card hover:border-sc-purple/40 hover:shadow-card-hover transition-all duration-200 cursor-pointer active:scale-[0.99]',
  bordered: 'bg-transparent border-2 border-sc-fg-subtle/40 ring-card',
  error: 'bg-sc-red/5 border border-sc-red/40 shadow-glow-red',
  warning: 'bg-sc-yellow/5 border border-sc-yellow/40 shadow-glow-yellow',
  success: 'bg-sc-green/5 border border-sc-green/40 shadow-glow-green',
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
