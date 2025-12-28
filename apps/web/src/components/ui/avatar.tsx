'use client';

import * as AvatarPrimitive from '@radix-ui/react-avatar';
import { type ComponentPropsWithoutRef, type ElementRef, forwardRef } from 'react';

type AvatarSize = 'xs' | 'sm' | 'md' | 'lg' | 'xl';

const sizes: Record<AvatarSize, { container: string; text: string }> = {
  xs: { container: 'w-6 h-6', text: 'text-[10px]' },
  sm: { container: 'w-8 h-8', text: 'text-xs' },
  md: { container: 'w-10 h-10', text: 'text-sm' },
  lg: { container: 'w-12 h-12', text: 'text-base' },
  xl: { container: 'w-16 h-16', text: 'text-lg' },
};

interface AvatarProps extends ComponentPropsWithoutRef<typeof AvatarPrimitive.Root> {
  size?: AvatarSize;
}

const Avatar = forwardRef<ElementRef<typeof AvatarPrimitive.Root>, AvatarProps>(
  ({ className = '', size = 'md', ...props }, ref) => (
    <AvatarPrimitive.Root
      ref={ref}
      className={`
        relative flex shrink-0 overflow-hidden rounded-full
        ${sizes[size].container}
        ${className}
      `}
      {...props}
    />
  )
);
Avatar.displayName = AvatarPrimitive.Root.displayName;

const AvatarImage = forwardRef<
  ElementRef<typeof AvatarPrimitive.Image>,
  ComponentPropsWithoutRef<typeof AvatarPrimitive.Image>
>(({ className = '', ...props }, ref) => (
  <AvatarPrimitive.Image
    ref={ref}
    className={`aspect-square h-full w-full object-cover ${className}`}
    {...props}
  />
));
AvatarImage.displayName = AvatarPrimitive.Image.displayName;

interface AvatarFallbackProps extends ComponentPropsWithoutRef<typeof AvatarPrimitive.Fallback> {
  size?: AvatarSize;
}

const AvatarFallback = forwardRef<ElementRef<typeof AvatarPrimitive.Fallback>, AvatarFallbackProps>(
  ({ className = '', size = 'md', ...props }, ref) => (
    <AvatarPrimitive.Fallback
      ref={ref}
      className={`
        flex h-full w-full items-center justify-center rounded-full
        bg-sc-bg-highlight border border-sc-fg-subtle/20
        text-sc-fg-muted font-medium
        ${sizes[size].text}
        ${className}
      `}
      {...props}
    />
  )
);
AvatarFallback.displayName = AvatarPrimitive.Fallback.displayName;

// Status indicator for avatar
type StatusType = 'online' | 'offline' | 'busy' | 'away';

interface AvatarStatusProps {
  status: StatusType;
  size?: AvatarSize;
}

const statusColors: Record<StatusType, string> = {
  online: 'bg-sc-green',
  offline: 'bg-sc-fg-subtle',
  busy: 'bg-sc-red',
  away: 'bg-sc-yellow',
};

const statusSizes: Record<AvatarSize, string> = {
  xs: 'w-1.5 h-1.5',
  sm: 'w-2 h-2',
  md: 'w-2.5 h-2.5',
  lg: 'w-3 h-3',
  xl: 'w-4 h-4',
};

function AvatarStatus({ status, size = 'md' }: AvatarStatusProps) {
  return (
    <span
      className={`
        absolute bottom-0 right-0 rounded-full
        ring-2 ring-sc-bg-base
        ${statusColors[status]}
        ${statusSizes[size]}
      `}
    />
  );
}

// Convenient wrapper for common avatar use case
interface UserAvatarProps {
  src?: string | null;
  name?: string | null;
  size?: AvatarSize;
  status?: StatusType;
  className?: string;
}

function getInitials(name: string): string {
  return name
    .split(' ')
    .map(part => part[0])
    .join('')
    .toUpperCase()
    .slice(0, 2);
}

export function UserAvatar({ src, name, size = 'md', status, className = '' }: UserAvatarProps) {
  return (
    <Avatar size={size} className={className}>
      <AvatarImage src={src || undefined} alt={name || 'User'} />
      <AvatarFallback size={size}>{name ? getInitials(name) : '?'}</AvatarFallback>
      {status && <AvatarStatus status={status} size={size} />}
    </Avatar>
  );
}

// Avatar group for stacked avatars
interface AvatarGroupProps {
  children: React.ReactNode;
  max?: number;
  size?: AvatarSize;
}

export function AvatarGroup({ children, max = 4, size = 'md' }: AvatarGroupProps) {
  const childArray = Array.isArray(children) ? children : [children];
  const visibleAvatars = childArray.slice(0, max);
  const remainingCount = childArray.length - max;

  const overlapClasses: Record<AvatarSize, string> = {
    xs: '-ml-2',
    sm: '-ml-2.5',
    md: '-ml-3',
    lg: '-ml-4',
    xl: '-ml-5',
  };

  return (
    <div className="flex items-center">
      {visibleAvatars.map((child, index) => (
        <div
          key={index}
          className={`
            relative ring-2 ring-sc-bg-base rounded-full
            ${index > 0 ? overlapClasses[size] : ''}
          `}
          style={{ zIndex: visibleAvatars.length - index }}
        >
          {child}
        </div>
      ))}
      {remainingCount > 0 && (
        <div
          className={`
            ${overlapClasses[size]} ${sizes[size].container}
            flex items-center justify-center rounded-full
            bg-sc-bg-highlight border border-sc-fg-subtle/20
            text-sc-fg-muted font-medium ${sizes[size].text}
            ring-2 ring-sc-bg-base
          `}
        >
          +{remainingCount}
        </div>
      )}
    </div>
  );
}

export { Avatar, AvatarImage, AvatarFallback, AvatarStatus };
