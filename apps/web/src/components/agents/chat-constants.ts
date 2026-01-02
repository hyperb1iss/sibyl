/**
 * Constants and helpers for the agent chat system.
 */

import type { IconComponent } from '@/components/ui/icons';
import {
  Check,
  Code,
  EditPencil,
  Folder,
  Globe,
  List,
  Page,
  Search,
  Settings,
  User,
  Xmark,
} from '@/components/ui/icons';

// =============================================================================
// Tool Icons
// =============================================================================

/** Map tool icon names from backend to icon components */
export const TOOL_ICONS: Record<string, IconComponent> = {
  Page,
  Code,
  EditPencil,
  Search,
  Folder,
  Globe,
  User,
  List,
  Settings,
  Check,
  Xmark,
};

/** Get icon component for a tool, with fallback to Code */
export function getToolIcon(iconName?: string): IconComponent {
  return iconName ? (TOOL_ICONS[iconName] ?? Code) : Code;
}

// =============================================================================
// Tool Status Templates (Tier 2)
// =============================================================================

/** Playful status templates per tool - {file}, {pattern}, {agent} get substituted */
export const TOOL_STATUS_TEMPLATES: Record<string, string[]> = {
  Read: [
    'Absorbing {file}',
    'Decoding {file}',
    'Studying {file}',
    'Ingesting {file}',
    'Parsing {file}',
  ],
  Edit: ['Sculpting {file}', 'Refining {file}', 'Tweaking {file}', 'Polishing {file}'],
  Write: ['Manifesting {file}', 'Conjuring {file}', 'Crafting {file}', 'Birthing {file}'],
  Grep: [
    'Hunting for {pattern}',
    'Seeking {pattern}',
    'Tracking {pattern}',
    'Sniffing out {pattern}',
  ],
  Glob: ['Scouting {pattern}', 'Mapping {pattern}', 'Surveying {pattern}'],
  Bash: [
    'Whispering to the shell',
    'Invoking the terminal',
    'Casting shell magic',
    'Running incantations',
  ],
  Task: ['Summoning {agent}', 'Dispatching {agent}', 'Rallying {agent}', 'Awakening {agent}'],
  WebSearch: ['Scouring the interwebs', 'Consulting the oracle', 'Querying the web'],
  WebFetch: ['Fetching from the void', 'Retrieving distant knowledge', 'Pulling from the ether'],
  LSP: ['Consulting the language server', 'Asking the code oracle', 'Querying symbols'],
};

/** Get a playful tool status with variable substitution */
export function getToolStatus(toolName: string, input?: Record<string, unknown>): string | null {
  const templates = TOOL_STATUS_TEMPLATES[toolName];
  if (!templates?.length) return null;

  const template = templates[Math.floor(Math.random() * templates.length)];

  // Extract substitution values from input
  const filePath = input?.file_path as string | undefined;
  const file = filePath ? filePath.split('/').pop() : undefined;
  const pattern = (input?.pattern as string | undefined) ?? (input?.query as string | undefined);
  const agent = input?.subagent_type as string | undefined;

  return template
    .replace('{file}', file ?? 'file')
    .replace('{pattern}', pattern ? `"${pattern.slice(0, 20)}"` : 'matches')
    .replace('{agent}', agent ?? 'agent');
}

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
  ],
  playful: [
    'Consulting the cosmic wiki',
    'Asking the rubber duck',
    'Summoning the muse',
    'Channeling the void',
    'Brewing some magic',
    'Spinning up neurons',
    'Wrangling electrons',
  ],
  mystical: [
    'Reading the tea leaves',
    'Divining the path forward',
    'Peering into the matrix',
    'Tapping the akashic records',
    'Communing with the codebase',
  ],
  cheeky: [
    'Hold my coffee',
    'One sec, almost there',
    'Trust the process',
    'Working some magic here',
    'Doing the thing',
  ],
} as const;

/** All thinking phrases flattened */
export const ALL_THINKING_PHRASES = [
  ...THINKING_PHRASES.focused,
  ...THINKING_PHRASES.playful,
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
