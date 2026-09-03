'use client';

import type { ReactNode } from 'react';
import { CaptureMemoryDialog } from '@/components/dashboard';
import { AsyncBoundary } from '@/components/error-boundary';
import { Breadcrumb } from './breadcrumb';
import { BreadcrumbProvider } from './breadcrumb-context';
import { CaptureMemoryProvider, useCaptureMemory } from './capture-memory-context';
import { CommandPaletteProvider } from './command-palette-context';
import { GlobalCommandPalette } from './global-command-palette';
import { Header } from './header';
import { Sidebar } from './sidebar';

function MainShellContent({ children }: { children: ReactNode }) {
  const { isOpen, captureSurface, closeCaptureMemory } = useCaptureMemory();

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
      <GlobalCommandPalette />
    </div>
  );
}

export function MainShell({ children }: { children: ReactNode }) {
  return (
    <BreadcrumbProvider>
      <CaptureMemoryProvider>
        <CommandPaletteProvider>
          <MainShellContent>{children}</MainShellContent>
        </CommandPaletteProvider>
      </CaptureMemoryProvider>
    </BreadcrumbProvider>
  );
}
