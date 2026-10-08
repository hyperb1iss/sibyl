'use client';

import { useState } from 'react';

const SIZES = {
  xs: 'h-5 w-5 text-[9px]',
  sm: 'h-8 w-8 text-xs',
  md: 'h-11 w-11 text-sm',
} as const;

// A stable tint per person, so the same teammate reads the same everywhere.
const TONES = [
  'from-sc-purple/30 to-sc-magenta/15 text-sc-purple border-sc-purple/30',
  'from-sc-cyan/25 to-sc-purple/15 text-sc-cyan border-sc-cyan/30',
  'from-sc-coral/30 to-sc-purple/15 text-sc-coral border-sc-coral/30',
  'from-sc-green/25 to-sc-cyan/15 text-sc-green border-sc-green/30',
  'from-sc-yellow/25 to-sc-coral/15 text-sc-yellow border-sc-yellow/30',
] as const;

export function initialsFor(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return '?';
  const first = words[0].charAt(0);
  const last = words.length > 1 ? words[words.length - 1].charAt(0) : '';
  return `${first}${last}`.toUpperCase();
}

function toneFor(seed: string): string {
  let hash = 0;
  for (let index = 0; index < seed.length; index++) {
    hash = (hash * 31 + seed.charCodeAt(index)) | 0;
  }
  return TONES[Math.abs(hash) % TONES.length];
}

interface PersonAvatarProps {
  name: string;
  /** Seeds the fallback tint; the user id keeps it stable across renames. */
  seed?: string;
  avatarUrl?: string | null;
  size?: keyof typeof SIZES;
  /** Neutral and dimmed, for someone with nothing in the window. */
  muted?: boolean;
  className?: string;
}

/** Profile photo, or initials on a per-person tint when there is none. */
export function PersonAvatar({
  name,
  seed,
  avatarUrl,
  size = 'sm',
  muted = false,
  className = '',
}: PersonAvatarProps) {
  const [failed, setFailed] = useState(false);
  const base = `${SIZES[size]} shrink-0 rounded-full border ${className}`;

  if (avatarUrl && !failed) {
    return (
      <img
        src={avatarUrl}
        alt=""
        aria-hidden="true"
        onError={() => setFailed(true)}
        className={`${base} border-sc-fg-subtle/20 object-cover ${muted ? 'opacity-60 grayscale' : ''}`}
      />
    );
  }

  return (
    <span
      aria-hidden="true"
      className={`${base} inline-flex items-center justify-center font-semibold ${
        muted
          ? 'border-sc-fg-subtle/25 bg-sc-bg-highlight text-sc-fg-muted'
          : `bg-gradient-to-br ${toneFor(seed ?? name)}`
      }`}
    >
      {initialsFor(name)}
    </span>
  );
}
