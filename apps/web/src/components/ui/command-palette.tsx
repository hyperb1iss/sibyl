'use client';

import * as DialogPrimitive from '@radix-ui/react-dialog';
import { HalfMoon, Sparks } from 'iconoir-react';
import { motion, useReducedMotion } from 'motion/react';
import { useRouter, useSearchParams } from 'next/navigation';
import type { ReactNode } from 'react';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { EntityBadge } from '@/components/ui/badge';
import { DialogOverlay } from '@/components/ui/dialog';
import { EntityIcon } from '@/components/ui/entity-icon';
import { Command, Plus, Search } from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import { THEME_OPTIONS } from '@/components/ui/theme-toggle';
import type { SearchResult } from '@/lib/api/search';
import { NAVIGATION, withProjectsContext } from '@/lib/constants/navigation';
import { useSearch } from '@/lib/hooks/search';
import { useDebouncedValue } from '@/lib/hooks/shared';
import { isEditableTarget } from '@/lib/keyboard';
import { getSearchResultHref } from '@/lib/search-links';
import { useTheme } from '@/lib/theme';

/**
 * The Cmd+K omnibox: commands, navigation, and live search in one list.
 *
 * Commands and navigation filter locally; once the query settles (150ms) the
 * unified search API fills Work / Memory / Docs groups. A final row always
 * hands off to the full /search page for the same query.
 */

type PaletteGroupId = 'commands' | 'navigation' | 'work' | 'memory' | 'docs' | 'search';

interface PaletteItem {
  id: string;
  group: PaletteGroupId;
  label: string;
  description?: string;
  icon: ReactNode;
  /** Right-aligned keyboard hint. */
  hint?: string;
  /** Right-aligned entity type badge for live results. */
  badgeType?: string;
  /** Extra lowercase text that should match the query. */
  keywords?: string;
  run: () => void;
}

interface PaletteGroup {
  id: PaletteGroupId;
  label: string;
  items: PaletteItem[];
}

const GROUP_ORDER: PaletteGroupId[] = [
  'commands',
  'navigation',
  'work',
  'memory',
  'docs',
  'search',
];
const GROUP_LABELS: Record<PaletteGroupId, string> = {
  commands: 'Commands',
  navigation: 'Navigation',
  work: 'Work',
  memory: 'Memory',
  docs: 'Docs',
  search: 'Search',
};

const LIVE_SEARCH_DEBOUNCE_MS = 150;
const LIVE_QUERY_MIN_CHARS = 2;
const LIVE_FETCH_LIMIT = 24;
const LIVE_RESULTS_PER_GROUP = 8;
const WORK_TYPES = new Set(['task', 'epic', 'project', 'milestone']);

function liveGroupFor(result: SearchResult): Extract<PaletteGroupId, 'work' | 'memory' | 'docs'> {
  if (result.result_origin === 'document') return 'docs';
  if (WORK_TYPES.has(result.type)) return 'work';
  return 'memory';
}

function cleanSnippet(content: string | null | undefined): string | undefined {
  if (!content) return undefined;
  const text = content
    .replace(/<\/?mark>/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return text || undefined;
}

function matchesQuery(item: PaletteItem, needle: string): boolean {
  if (!needle) return true;
  const haystack = `${item.label} ${item.description ?? ''} ${item.keywords ?? ''}`.toLowerCase();
  return haystack.includes(needle);
}

const KBD_CLASS =
  'rounded border border-sc-fg-subtle/20 bg-sc-bg-highlight px-1.5 py-0.5 font-mono text-[10px] text-sc-fg-muted';

interface CommandPaletteProps {
  isOpen: boolean;
  onClose: () => void;
  /** Override for "New task"; defaults to routing to /tasks?new=1. */
  onCreateTask?: () => void;
  /** Adds the "Capture memory" command when provided. */
  onCaptureMemory?: () => void;
}

export function CommandPalette({
  isOpen,
  onClose,
  onCreateTask,
  onCaptureMemory,
}: CommandPaletteProps) {
  const reduceMotion = useReducedMotion();

  return (
    <DialogPrimitive.Root
      open={isOpen}
      onOpenChange={open => {
        if (!open) onClose();
      }}
    >
      <DialogPrimitive.Portal>
        <DialogOverlay />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className="fixed left-1/2 top-[15vh] z-50 w-[calc(100vw-2rem)] max-w-[640px] -translate-x-1/2 focus:outline-none"
        >
          <DialogPrimitive.Title className="sr-only">Command palette</DialogPrimitive.Title>
          <motion.div
            initial={reduceMotion ? { opacity: 0 } : { opacity: 0, scale: 0.98, y: -6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            transition={{ duration: 0.18, ease: 'easeOut' }}
            className="overflow-hidden rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-elevated shadow-glow-purple"
          >
            <PaletteBody
              onClose={onClose}
              onCreateTask={onCreateTask}
              onCaptureMemory={onCaptureMemory}
            />
          </motion.div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

// Mounted only while the dialog is open, so query and selection reset for free.
function PaletteBody({
  onClose,
  onCreateTask,
  onCaptureMemory,
}: Omit<CommandPaletteProps, 'isOpen'>) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { preference, toggleTheme } = useTheme();
  const listId = useId();
  const listRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);

  const trimmedQuery = query.trim();
  const needle = trimmedQuery.toLowerCase();
  const debouncedQuery = useDebouncedValue(trimmedQuery, LIVE_SEARCH_DEBOUNCE_MS);
  const liveEnabled = debouncedQuery.length >= LIVE_QUERY_MIN_CHARS;
  const projects = searchParams.get('projects');

  const { data: liveData, isFetching: liveFetching } = useSearch(
    {
      query: debouncedQuery,
      limit: LIVE_FETCH_LIMIT,
      include_documents: true,
      include_graph: true,
      include_raw_memory: true,
    },
    { enabled: liveEnabled }
  );

  const navigate = useCallback(
    (href: string) => {
      onClose();
      router.push(href);
    },
    [onClose, router]
  );

  const groups = useMemo<PaletteGroup[]>(() => {
    const themeLabel =
      THEME_OPTIONS.find(option => option.value === preference)?.label ?? THEME_OPTIONS[0].label;

    const commands: PaletteItem[] = [
      {
        id: 'cmd-new-task',
        group: 'commands',
        label: 'New task',
        description: 'Create a task on the board',
        icon: <Plus width={16} height={16} />,
        keywords: 'create add todo',
        run: () => {
          if (onCreateTask) {
            onClose();
            onCreateTask();
            return;
          }
          navigate(withProjectsContext('/tasks?new=1', projects));
        },
      },
      ...(onCaptureMemory
        ? [
            {
              id: 'cmd-capture-memory',
              group: 'commands' as const,
              label: 'Capture memory',
              description: 'Save a learning, pattern, or failure mode',
              icon: <Sparks width={16} height={16} />,
              keywords: 'remember note learning quick',
              run: () => {
                onClose();
                onCaptureMemory();
              },
            },
          ]
        : []),
      {
        id: 'cmd-toggle-theme',
        group: 'commands',
        label: 'Toggle theme',
        description: `Now ${themeLabel}`,
        icon: <HalfMoon width={16} height={16} />,
        hint: '⌘⇧L',
        keywords: 'dark light neon dawn system appearance',
        run: () => {
          onClose();
          toggleTheme();
        },
      },
    ];

    const navigation: PaletteItem[] = NAVIGATION.map(item => ({
      id: `nav-${item.href}`,
      group: 'navigation',
      label: item.name,
      description: `Go to ${item.name}`,
      icon: <item.icon width={16} height={16} />,
      run: () => navigate(withProjectsContext(item.href, projects)),
    }));

    const live: PaletteItem[] = [];
    if (liveEnabled && liveData) {
      const seen: Partial<Record<PaletteGroupId, number>> = {};
      for (const result of liveData.results) {
        const group = liveGroupFor(result);
        const count = (seen[group] ?? 0) + 1;
        seen[group] = count;
        if (count > LIVE_RESULTS_PER_GROUP) continue;
        const snippet = cleanSnippet(result.content);
        live.push({
          id: `live-${result.id}`,
          group,
          label: result.name,
          // Many work items carry their title as content; skip the echo.
          description: snippet === result.name ? undefined : snippet,
          icon: <EntityIcon type={result.type} size={16} />,
          badgeType: result.type,
          run: () => navigate(getSearchResultHref(result)),
        });
      }
    }

    const searchHref = trimmedQuery ? `/search?q=${encodeURIComponent(trimmedQuery)}` : '/search';
    const searchRow: PaletteItem = {
      id: 'search-everything',
      group: 'search',
      label: trimmedQuery ? `Search everything for "${trimmedQuery}"` : 'Open search',
      description: 'Full results with filters and facets',
      icon: <Search width={16} height={16} />,
      run: () => navigate(withProjectsContext(searchHref, projects)),
    };

    const items = [
      ...[...commands, ...navigation].filter(item => matchesQuery(item, needle)),
      ...live,
      searchRow,
    ];

    return GROUP_ORDER.map(id => ({
      id,
      label: GROUP_LABELS[id],
      items: items.filter(item => item.group === id),
    })).filter(group => group.items.length > 0);
  }, [
    liveData,
    liveEnabled,
    navigate,
    needle,
    onCaptureMemory,
    onClose,
    onCreateTask,
    preference,
    projects,
    toggleTheme,
    trimmedQuery,
  ]);

  const flatItems = useMemo(() => groups.flatMap(group => group.items), [groups]);
  const indexById = useMemo(
    () => new Map(flatItems.map((item, index) => [item.id, index])),
    [flatItems]
  );
  const activeIndex = Math.min(selectedIndex, Math.max(flatItems.length - 1, 0));
  const activeItem = flatItems[activeIndex];
  const optionId = (item: PaletteItem) => `${listId}-option-${item.id}`;
  const liveCount = groups
    .filter(group => group.id === 'work' || group.id === 'memory' || group.id === 'docs')
    .reduce((total, group) => total + group.items.length, 0);

  // Keep the highlighted row visible while arrowing through a long list.
  useEffect(() => {
    const row = listRef.current?.querySelector<HTMLElement>(`[data-index="${activeIndex}"]`);
    row?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        setSelectedIndex(Math.min(activeIndex + 1, flatItems.length - 1));
        break;
      case 'ArrowUp':
        event.preventDefault();
        setSelectedIndex(Math.max(activeIndex - 1, 0));
        break;
      case 'Home':
        event.preventDefault();
        setSelectedIndex(0);
        break;
      case 'End':
        event.preventDefault();
        setSelectedIndex(Math.max(flatItems.length - 1, 0));
        break;
      case 'Enter':
        event.preventDefault();
        activeItem?.run();
        break;
      default:
        break;
    }
  };

  return (
    <>
      {/* Query */}
      <div className="flex items-center gap-3 border-b border-sc-fg-subtle/15 px-4 py-3">
        <Search width={18} height={18} className="shrink-0 text-sc-purple" aria-hidden="true" />
        <input
          type="text"
          role="combobox"
          value={query}
          onChange={event => {
            setQuery(event.target.value);
            setSelectedIndex(0);
          }}
          onKeyDown={handleKeyDown}
          aria-expanded="true"
          aria-controls={listId}
          aria-activedescendant={activeItem ? optionId(activeItem) : undefined}
          aria-autocomplete="list"
          aria-label="Search or run a command"
          placeholder="Search memory, tasks, docs, or run a command..."
          autoComplete="off"
          spellCheck={false}
          className="min-w-0 flex-1 bg-transparent text-[15px] text-sc-fg-primary outline-none placeholder:text-sc-fg-muted/70"
        />
        {liveFetching && <Spinner size="sm" />}
        <kbd className={KBD_CLASS}>esc</kbd>
      </div>

      {/* Results */}
      <div
        ref={listRef}
        id={listId}
        role="listbox"
        aria-label="Palette results"
        className="max-h-[min(60vh,440px)] overflow-y-auto p-2"
      >
        {groups.map(group => (
          <div
            key={group.id}
            role="group"
            aria-labelledby={`${listId}-group-${group.id}`}
            className="mb-1 last:mb-0"
          >
            <div
              id={`${listId}-group-${group.id}`}
              className="px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-sc-fg-subtle"
            >
              {group.label}
            </div>
            {group.items.map(item => {
              const index = indexById.get(item.id) ?? 0;
              return (
                <PaletteRow
                  key={item.id}
                  item={item}
                  index={index}
                  optionId={optionId(item)}
                  selected={index === activeIndex}
                  onHover={setSelectedIndex}
                />
              );
            })}
          </div>
        ))}
        {liveEnabled && liveFetching && liveCount === 0 && (
          <div className="px-3 py-2 text-xs text-sc-fg-muted">
            Searching memory, tasks, and docs...
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="flex items-center gap-4 border-t border-sc-fg-subtle/15 px-4 py-2 text-[11px] text-sc-fg-muted">
        <span className="flex items-center gap-1.5">
          <kbd className={KBD_CLASS}>↑↓</kbd> navigate
        </span>
        <span className="flex items-center gap-1.5">
          <kbd className={KBD_CLASS}>↵</kbd> open
        </span>
        <span className="flex items-center gap-1.5">
          <kbd className={KBD_CLASS}>esc</kbd> close
        </span>
        <span
          className="ml-auto hidden items-center gap-1 text-sc-fg-subtle sm:flex"
          aria-hidden="true"
        >
          <Command width={10} height={10} />K
        </span>
      </div>
    </>
  );
}

interface PaletteRowProps {
  item: PaletteItem;
  index: number;
  optionId: string;
  selected: boolean;
  onHover: (index: number) => void;
}

function PaletteRow({ item, index, optionId, selected, onHover }: PaletteRowProps) {
  return (
    <div
      id={optionId}
      role="option"
      aria-selected={selected}
      tabIndex={-1}
      data-index={index}
      onClick={item.run}
      onMouseMove={() => onHover(index)}
      className={`relative flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 transition-colors duration-150 ${
        selected ? 'bg-sc-purple/15 text-sc-fg-primary' : 'text-sc-fg-primary'
      }`}
    >
      {selected && (
        <span
          aria-hidden="true"
          className="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-full bg-sc-purple shadow-[0_0_8px_color-mix(in_oklch,var(--sc-purple)_60%,transparent)]"
        />
      )}
      <span
        aria-hidden="true"
        className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border transition-colors duration-150 ${
          selected
            ? 'border-sc-purple/40 bg-sc-purple/10 text-sc-purple'
            : 'border-sc-fg-subtle/15 bg-sc-bg-highlight text-sc-fg-muted'
        }`}
      >
        {item.icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{item.label}</span>
        {item.description && (
          <span className="block truncate text-xs text-sc-fg-muted">{item.description}</span>
        )}
      </span>
      {item.badgeType ? (
        <EntityBadge type={item.badgeType} />
      ) : item.hint ? (
        <kbd className={KBD_CLASS}>{item.hint}</kbd>
      ) : null}
    </div>
  );
}

/**
 * Page-level single-key shortcuts. Cmd+K belongs to CommandPaletteProvider;
 * this only binds "c" for quick task creation outside text fields.
 */
export function useKeyboardShortcuts(options: { onCreateTask?: () => void }) {
  const { onCreateTask } = options;

  useEffect(() => {
    if (!onCreateTask) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (isEditableTarget(event.target)) return;
      if (event.key === 'c' && !event.metaKey && !event.ctrlKey && !event.altKey) {
        event.preventDefault();
        onCreateTask();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onCreateTask]);
}
