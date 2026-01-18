/**
 * Constants and helpers for the agent chat system.
 *
 * Tool-specific constants (icons, status templates) are in tool-registry.ts
 */

// =============================================================================
// Thinking Phrases (Tier 1)
// =============================================================================

/** Clever waiting phrases grouped by mood */
export const THINKING_PHRASES = {
  focused: [
    'Reasoning through this',
    'Mapping the terrain',
    'Tracing the threads',
    'Connecting the pieces',
    'Following the breadcrumbs',
    'Synthesizing thoughts',
  ],
  playful: [
    'Consulting the cosmic wiki',
    'Asking the rubber duck',
    'Summoning the muse',
    'Channeling the void',
    'Brewing some magic',
    'Spinning up neurons',
    'Wrangling electrons',
    'Flibbertigibbeting',
    'Cogitating intensely',
    'Percolating ideas',
  ],
  claude: [
    'Clauding away',
    'Doing that Claude thing',
    'Being helpful',
    'Contemplating elegantly',
    'Weaving thoughts together',
    'Crafting a response',
  ],
  mystical: [
    'Reading the tea leaves',
    'Divining the path forward',
    'Peering into the matrix',
    'Tapping the akashic records',
    'Communing with the codebase',
    'Consulting the oracle',
  ],
  cheeky: [
    'Hold my coffee',
    'One sec, almost there',
    'Trust the process',
    'Working some magic here',
    'Doing the thing',
    'Just a moment',
    'Pondering ponderously',
  ],
} as const;

/** All thinking phrases flattened */
export const ALL_THINKING_PHRASES = [
  ...THINKING_PHRASES.focused,
  ...THINKING_PHRASES.playful,
  ...THINKING_PHRASES.claude,
  ...THINKING_PHRASES.mystical,
  ...THINKING_PHRASES.cheeky,
];

/** Pick a random thinking phrase */
export function pickRandomPhrase(): string {
  return ALL_THINKING_PHRASES[Math.floor(Math.random() * ALL_THINKING_PHRASES.length)];
}

// =============================================================================
// Utilities
// =============================================================================

/** Format milliseconds to human-readable duration */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}

/**
 * Strip ANSI escape codes from a string.
 * Handles color codes, cursor movement, and other terminal sequences.
 */
export function stripAnsi(str: string): string {
  // Matches ANSI escape sequences:
  // - ESC (0x1B) followed by [ and parameters ending in a letter
  // - CSI (0x9B) followed by parameters
  // Uses string-based regex to avoid biome control character warnings
  // biome-ignore lint/complexity/useRegexLiterals: can't use literal due to noControlCharactersInRegex
  const ansiPattern = new RegExp(
    '[\\x1b\\x9b][[()#;?]*(?:[0-9]{1,4}(?:;[0-9]{0,4})*)?[0-9A-ORZcf-nqry=><]',
    'g'
  );
  return str.replace(ansiPattern, '');
}
