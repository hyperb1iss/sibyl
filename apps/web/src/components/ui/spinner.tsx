'use client';

import { useEffect, useState } from 'react';

type SpinnerSize = 'sm' | 'md' | 'lg' | 'xl';

interface SpinnerProps {
  size?: SpinnerSize;
  color?: 'purple' | 'cyan' | 'white' | 'current';
  variant?: 'default' | 'orbital' | 'gradient';
  className?: string;
}

const sizes: Record<SpinnerSize, string> = {
  sm: 'w-4 h-4 border-2',
  md: 'w-6 h-6 border-2',
  lg: 'w-8 h-8 border-2',
  xl: 'w-12 h-12 border-3',
};

const colors: Record<NonNullable<SpinnerProps['color']>, string> = {
  purple: 'border-sc-purple border-t-transparent',
  cyan: 'border-sc-cyan border-t-transparent',
  white: 'border-white border-t-transparent',
  current: 'border-current border-t-transparent',
};

export function Spinner({
  size = 'md',
  color = 'purple',
  variant = 'default',
  className = '',
}: SpinnerProps) {
  if (variant === 'orbital') {
    return <OrbitalSpinner size={size} />;
  }

  if (variant === 'gradient') {
    return <GradientSpinner size={size} />;
  }

  return (
    <div
      className={`
        rounded-full animate-spin
        ${sizes[size]}
        ${colors[color]}
        ${className}
      `}
      role="status"
      aria-label="Loading"
    />
  );
}

// Orbital spinner with multiple rotating dots
function OrbitalSpinner({ size = 'md' }: { size: SpinnerSize }) {
  const sizeMap = {
    sm: { container: 'w-4 h-4', dot: 'w-1 h-1' },
    md: { container: 'w-6 h-6', dot: 'w-1.5 h-1.5' },
    lg: { container: 'w-8 h-8', dot: 'w-2 h-2' },
    xl: { container: 'w-12 h-12', dot: 'w-3 h-3' },
  };

  const { container, dot } = sizeMap[size];

  return (
    <div className={`relative ${container}`} role="status" aria-label="Loading">
      <div className="absolute inset-0 animate-orbital">
        <div
          className={`absolute top-0 left-1/2 -translate-x-1/2 ${dot} rounded-full bg-sc-purple`}
        />
      </div>
      <div className="absolute inset-0 animate-orbital" style={{ animationDelay: '0.2s' }}>
        <div
          className={`absolute top-0 left-1/2 -translate-x-1/2 ${dot} rounded-full bg-sc-cyan`}
        />
      </div>
      <div className="absolute inset-0 animate-orbital" style={{ animationDelay: '0.4s' }}>
        <div
          className={`absolute top-0 left-1/2 -translate-x-1/2 ${dot} rounded-full bg-sc-coral`}
        />
      </div>
    </div>
  );
}

// Gradient spinner with animated border
function GradientSpinner({ size = 'md' }: { size: SpinnerSize }) {
  const sizeClasses = {
    sm: 'w-4 h-4 border-2',
    md: 'w-6 h-6 border-2',
    lg: 'w-8 h-8 border-2',
    xl: 'w-12 h-12 border-3',
  };

  return (
    <div
      className={`
        rounded-full animate-spin
        ${sizeClasses[size]}
        border-transparent
        bg-gradient-to-r from-sc-purple via-sc-cyan to-sc-coral
        bg-clip-border
      `}
      style={{
        backgroundClip: 'padding-box, border-box',
        backgroundOrigin: 'padding-box, border-box',
        borderImage: 'linear-gradient(135deg, #e135ff, #80ffea, #ff6ac1) 1',
      }}
      role="status"
      aria-label="Loading"
    />
  );
}

// Centered loading state for full sections with cycling messages
interface LoadingStateProps {
  size?: SpinnerSize;
  message?: string;
  variant?: 'default' | 'orbital' | 'gradient';
  playful?: boolean;
}

const PLAYFUL_MESSAGES = [
  'Summoning digital spirits...',
  'Consulting the knowledge graph...',
  'Wrangling electrons...',
  'Teaching neurons new tricks...',
  'Spinning up the quantum bits...',
  'Brewing some fresh insights...',
  'Untangling the web of wisdom...',
  'Charging the flux capacitor...',
  'Aligning the stars...',
  'Decoding the matrix...',
];

export function LoadingState({
  size = 'lg',
  message,
  variant = 'orbital',
  playful = false,
}: LoadingStateProps) {
  const [currentMessage, setCurrentMessage] = useState(
    message || (playful ? PLAYFUL_MESSAGES[0] : undefined)
  );
  const [messageIndex, setMessageIndex] = useState(0);

  useEffect(() => {
    if (!playful || message) return;

    const interval = setInterval(() => {
      setMessageIndex(prev => (prev + 1) % PLAYFUL_MESSAGES.length);
    }, 2500);

    return () => clearInterval(interval);
  }, [playful, message]);

  useEffect(() => {
    if (playful && !message) {
      setCurrentMessage(PLAYFUL_MESSAGES[messageIndex]);
    }
  }, [messageIndex, playful, message]);

  return (
    <div className="flex flex-col items-center justify-center py-12 animate-fade-in">
      <Spinner size={size} variant={variant} />
      {currentMessage && (
        <p className="mt-4 text-sc-fg-muted text-sm animate-fade-in">{currentMessage}</p>
      )}
    </div>
  );
}

// Skeleton loaders for content placeholders with shimmer effect
interface SkeletonProps {
  className?: string;
  shimmer?: boolean;
}

export function Skeleton({ className = '', shimmer = true }: SkeletonProps) {
  return (
    <div
      className={`
        bg-sc-bg-highlight rounded
        ${shimmer ? 'animate-shimmer' : 'animate-pulse'}
        ${className}
      `}
    />
  );
}

export function SkeletonCard() {
  return (
    <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-6 space-y-4 animate-fade-in">
      <div className="flex items-center justify-between">
        <Skeleton className="h-5 w-24" />
        <Skeleton className="h-5 w-5 rounded-full" />
      </div>
      <Skeleton className="h-8 w-32" />
      <Skeleton className="h-4 w-20" />
    </div>
  );
}

export function SkeletonList({ count = 3 }: { count?: number }) {
  return (
    <div className="space-y-4">
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-4 animate-fade-in"
          style={{ animationDelay: `${i * 0.1}s` }}
        >
          <div className="flex items-start gap-4">
            <Skeleton className="h-6 w-16 rounded" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-5 w-48" />
              <Skeleton className="h-4 w-full" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// Progress indicator with electric effect
interface ProgressSpinnerProps {
  progress: number; // 0-100
  size?: SpinnerSize;
  showPercent?: boolean;
}

export function ProgressSpinner({
  progress,
  size = 'lg',
  showPercent = true,
}: ProgressSpinnerProps) {
  const sizeMap = {
    sm: { container: 'w-8 h-8', text: 'text-[8px]', stroke: '3' },
    md: { container: 'w-12 h-12', text: 'text-[10px]', stroke: '4' },
    lg: { container: 'w-16 h-16', text: 'text-xs', stroke: '4' },
    xl: { container: 'w-24 h-24', text: 'text-sm', stroke: '5' },
  };

  const { container, text, stroke } = sizeMap[size];
  const radius = 20;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (progress / 100) * circumference;

  return (
    <div className={`relative ${container}`}>
      <svg
        className="transform -rotate-90 w-full h-full"
        role="img"
        aria-label={`Loading: ${progress}%`}
      >
        <circle
          cx="50%"
          cy="50%"
          r={radius}
          stroke="currentColor"
          strokeWidth={stroke}
          fill="none"
          className="text-sc-bg-highlight"
        />
        <circle
          cx="50%"
          cy="50%"
          r={radius}
          stroke="currentColor"
          strokeWidth={stroke}
          fill="none"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          className="text-sc-cyan transition-all duration-300 drop-shadow-[0_0_8px_rgba(128,255,234,0.5)]"
        />
      </svg>
      {showPercent && (
        <div
          className={`absolute inset-0 flex items-center justify-center ${text} font-bold text-sc-fg-primary`}
        >
          {Math.round(progress)}%
        </div>
      )}
    </div>
  );
}
