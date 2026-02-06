/**
 * Sibyl console greeting
 *
 * ✦ SIBYL — collective intelligence
 */

const VERSION = '0.1.0';

// SilkCircuit palette
const STYLES = {
  star: 'color: #e135ff; font-weight: bold; font-size: 14px;',
  name: 'color: #80ffea; font-weight: bold; font-size: 14px;',
  dash: 'color: #555566; font-size: 14px;',
  tagline: 'color: #ff6ac1; font-style: italic; font-size: 14px;',
  version: 'color: #e135ff; font-size: 12px; opacity: 0.7;',
};

export function printConsoleGreeting(): void {
  if (typeof window === 'undefined') return;

  console.log(
    `%c✦ %cSIBYL%c — %ccollective intelligence  %cv${VERSION}`,
    STYLES.star,
    STYLES.name,
    STYLES.dash,
    STYLES.tagline,
    STYLES.version
  );
}
