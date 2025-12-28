'use client';

import { Toaster } from 'sonner';

import { useTheme } from '@/lib/theme';

export function ThemedToaster() {
  const { theme } = useTheme();

  return (
    <Toaster
      theme={theme === 'dawn' ? 'light' : 'dark'}
      position="bottom-right"
      toastOptions={{
        style: {
          background: 'var(--sc-bg-elevated)',
          border: '1px solid var(--sc-fg-subtle)',
          color: 'var(--sc-fg-primary)',
        },
      }}
    />
  );
}
