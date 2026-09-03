'use client';

import { CommandPalette } from '@/components/ui/command-palette';
import { useCaptureMemory } from './capture-memory-context';
import { useCommandPalette } from './command-palette-context';

/** The shell's single omnibox instance, wired to the palette and capture contexts. */
export function GlobalCommandPalette() {
  const { isOpen, close } = useCommandPalette();
  const { openCaptureMemory } = useCaptureMemory();

  return (
    <CommandPalette
      isOpen={isOpen}
      onClose={close}
      onCaptureMemory={() => openCaptureMemory('palette')}
    />
  );
}
