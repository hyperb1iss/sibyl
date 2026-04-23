/**
 * Sibyl logger - unified logging for Next.js.
 *
 * Matches Python output format: service | timestamp | level | message key=value...
 *
 * Usage:
 *   import { log } from '@/lib/logger';
 *   log.info('Request handled', { path: '/api/users', status: 200 });
 */

// Sibyl color palette (ANSI 24-bit)
const COLORS = {
  purple: '\x1b[38;2;225;53;255m',
  cyan: '\x1b[38;2;128;255;234m',
  coral: '\x1b[38;2;255;106;193m',
  yellow: '\x1b[38;2;241;250;140m',
  green: '\x1b[38;2;80;250;123m',
  red: '\x1b[38;2;255;99;99m',
  dim: '\x1b[38;2;85;85;102m',
  reset: '\x1b[0m',
} as const;

const LEVEL_COLORS: Record<string, string> = {
  trace: COLORS.dim,
  debug: COLORS.dim,
  info: COLORS.cyan,
  warn: COLORS.yellow,
  error: COLORS.red,
};

const SERVICE_NAME = 'web';
const SERVICE_WIDTH = 7;

type LogLevel = 'trace' | 'debug' | 'info' | 'warn' | 'error';
type LogData = Record<string, unknown>;

/**
 * Check if colors should be enabled (TTY or FORCE_COLOR)
 */
function shouldUseColors(): boolean {
  if (typeof process === 'undefined') return false;
  const forceColor = process.env.FORCE_COLOR;
  if (forceColor && forceColor !== '0' && forceColor !== 'false') return true;
  return process.stdout?.isTTY ?? false;
}

/**
 * Get current timestamp in HH:MM:SS format
 */
function getTimestamp(): string {
  return new Date().toTimeString().slice(0, 8);
}

/**
 * Format key-value pairs for log output
 */
function formatKV(data: LogData | undefined, colorsEnabled: boolean): string {
  if (!data || Object.keys(data).length === 0) return '';

  const pairs = Object.entries(data)
    .filter(([key]) => !key.startsWith('_'))
    .map(([key, value]) => {
      if (colorsEnabled) {
        if (typeof value === 'number') {
          return `${key}=${COLORS.coral}${value}${COLORS.reset}`;
        }
        if (typeof value === 'boolean') {
          const color = value ? COLORS.green : COLORS.red;
          return `${key}=${color}${value}${COLORS.reset}`;
        }
      }
      return `${key}=${value}`;
    });

  return pairs.join(' ');
}

/**
 * Format a log message in Sibyl style
 */
function formatLog(level: LogLevel, message: string, data?: LogData): string {
  const colors = shouldUseColors();
  const timestamp = getTimestamp();
  const kv = formatKV(data, colors);

  if (colors) {
    const service = `${COLORS.cyan}${SERVICE_NAME.padEnd(SERVICE_WIDTH)}${COLORS.reset}`;
    const ts = `${COLORS.dim}${timestamp}${COLORS.reset}`;
    const lvlColor = LEVEL_COLORS[level] || COLORS.cyan;
    const lvl = `${lvlColor}${level.padEnd(5)}${COLORS.reset}`;
    const kvStr = kv ? ` ${COLORS.dim}${kv}${COLORS.reset}` : '';
    return `${service} | ${ts} | ${lvl} | ${message}${kvStr}`;
  }

  const service = SERVICE_NAME.padEnd(SERVICE_WIDTH);
  const lvl = level.padEnd(5);
  const kvStr = kv ? ` ${kv}` : '';
  return `${service} | ${timestamp} | ${lvl} | ${message}${kvStr}`;
}

/**
 * Log to appropriate output based on level
 */
function writeLog(level: LogLevel, message: string, data?: LogData): void {
  const formatted = formatLog(level, message, data);

  if (level === 'error') {
    console.error(formatted);
  } else if (level === 'warn') {
    console.warn(formatted);
  } else {
    console.log(formatted);
  }
}

/**
 * Sibyl logger instance
 *
 * @example
 * log.info('Request handled', { path: '/api/users', status: 200 });
 * log.error('Database connection failed', { host: 'localhost', port: 5432 });
 */
export const log = {
  trace: (message: string, data?: LogData) => writeLog('trace', message, data),
  debug: (message: string, data?: LogData) => writeLog('debug', message, data),
  info: (message: string, data?: LogData) => writeLog('info', message, data),
  warn: (message: string, data?: LogData) => writeLog('warn', message, data),
  error: (message: string, data?: LogData) => writeLog('error', message, data),
};

/**
 * Re-export colors for use in other modules
 */
export { COLORS };
