'use client';

import { CommandPalette } from '@/components/ui/command-palette';
import { useCommandPalette } from './command-palette-context';

/**
 * The shell's single omnibox instance. Memory capture stays on the dashboard
 * launchers on purpose (see commit 88605b04), so no capture command is wired.
 */
export function GlobalCommandPalette() {
  const { isOpen, close } = useCommandPalette();

  return <CommandPalette isOpen={isOpen} onClose={close} />;
}
