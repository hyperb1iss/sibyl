'use client';

import type { ReactNode } from 'react';
import { useEffect, useState } from 'react';
import { CaptureMemoryDialog } from '@/components/dashboard';
import { AsyncBoundary } from '@/components/error-boundary';
import { CommandPalette } from '@/components/ui/command-palette';
import { Breadcrumb } from './breadcrumb';
import { BreadcrumbProvider } from './breadcrumb-context';
import { CaptureMemoryProvider, useCaptureMemory } from './capture-memory-context';
import { Header } from './header';
import { Sidebar } from './sidebar';

function MainShellContent({ children }: { children: ReactNode }) {
  const { isOpen, captureSurface, closeCaptureMemory } = useCaptureMemory();
  const [isCommandPaletteOpen, setIsCommandPaletteOpen] = useState(false);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
      ) {
        return;
      }

      if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setIsCommandPaletteOpen(true);
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);

  return (
    <div className="flex h-dvh overflow-hidden">
      <Sidebar />
      <div className="flex-1 flex flex-col overflow-hidden min-w-0">
        <Header />
        <main
          className="flex-1 overflow-auto bg-sc-bg-dark p-3 sm:p-4 md:p-6"
          style={{ scrollbarGutter: 'stable' }}
        >
          <div className="mb-4">
            <Breadcrumb />
          </div>
          <AsyncBoundary level="page">{children}</AsyncBoundary>
        </main>
      </div>

      <CaptureMemoryDialog
        isOpen={isOpen}
        onClose={closeCaptureMemory}
        captureSurface={captureSurface}
      />
      <CommandPalette
        isOpen={isCommandPaletteOpen}
        onClose={() => setIsCommandPaletteOpen(false)}
      />
    </div>
  );
}

export function MainShell({ children }: { children: ReactNode }) {
  return (
    <BreadcrumbProvider>
      <CaptureMemoryProvider>
        <MainShellContent>{children}</MainShellContent>
      </CaptureMemoryProvider>
    </BreadcrumbProvider>
  );
}
