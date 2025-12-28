'use client';

import { Computer, HalfMoon, SunLight } from 'iconoir-react';
import { useEffect } from 'react';

import { type ThemePreference, useTheme } from '@/lib/theme';

const THEME_OPTIONS: { value: ThemePreference; icon: typeof HalfMoon; label: string }[] = [
  { value: 'neon', icon: HalfMoon, label: 'Neon (Dark)' },
  { value: 'dawn', icon: SunLight, label: 'Dawn (Light)' },
  { value: 'system', icon: Computer, label: 'System' },
];

export function ThemeToggle() {
  const { preference, toggleTheme } = useTheme();

  // Keyboard shortcut: Cmd/Ctrl + Shift + L
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === 'l') {
        e.preventDefault();
        toggleTheme();
      }
    }
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [toggleTheme]);

  const current = THEME_OPTIONS.find(o => o.value === preference) ?? THEME_OPTIONS[0];
  const Icon = current.icon;

  return (
    <button
      type="button"
      onClick={toggleTheme}
      className="flex items-center gap-2 px-3 py-2 rounded-lg text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight transition-colors"
      title={`Theme: ${current.label} (⌘⇧L to cycle)`}
    >
      <Icon className="w-4 h-4" />
      <span className="text-sm">{current.label}</span>
    </button>
  );
}

export function ThemeToggleCompact() {
  const { preference, toggleTheme } = useTheme();

  const current = THEME_OPTIONS.find(o => o.value === preference) ?? THEME_OPTIONS[0];
  const Icon = current.icon;

  return (
    <button
      type="button"
      onClick={toggleTheme}
      className="p-2 rounded-lg text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight transition-colors"
      title={`Theme: ${current.label} (⌘⇧L)`}
    >
      <Icon className="w-5 h-5" />
    </button>
  );
}
