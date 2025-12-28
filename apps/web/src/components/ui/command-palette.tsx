'use client';

import { useRouter } from 'next/navigation';
import type { ReactNode } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  BookOpen,
  Boxes,
  FolderKanban,
  KanbanBoard,
  LayoutDashboard,
  List,
  ListTodo,
  Network,
  Search,
} from '@/components/ui/icons';

// Navigation items with Iconoir icons
const COMMAND_NAV: Array<{ name: string; href: string; icon: ReactNode }> = [
  { name: 'Dashboard', href: '/', icon: <LayoutDashboard width={18} height={18} /> },
  { name: 'Projects', href: '/projects', icon: <FolderKanban width={18} height={18} /> },
  { name: 'Tasks', href: '/tasks', icon: <ListTodo width={18} height={18} /> },
  { name: 'Sources', href: '/sources', icon: <BookOpen width={18} height={18} /> },
  { name: 'Graph', href: '/graph', icon: <Network width={18} height={18} /> },
  { name: 'Entities', href: '/entities', icon: <Boxes width={18} height={18} /> },
  { name: 'Search', href: '/search', icon: <Search width={18} height={18} /> },
];

interface CommandItem {
  id: string;
  label: string;
  description?: string;
  icon?: ReactNode;
  shortcut?: string;
  action: () => void;
  category: 'navigation' | 'action' | 'create';
}

interface CommandPaletteProps {
  isOpen: boolean;
  onClose: () => void;
  onCreateTask?: () => void;
  onCreateProject?: () => void;
}

export function CommandPalette({
  isOpen,
  onClose,
  onCreateTask,
  onCreateProject,
}: CommandPaletteProps) {
  const router = useRouter();
  const [query, setQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Build command list
  const commands: CommandItem[] = [
    // Create actions
    ...(onCreateTask
      ? [
          {
            id: 'create-task',
            label: 'Create Task',
            description: 'Add a new task',
            icon: <List width={18} height={18} />,
            shortcut: 'C',
            action: () => {
              onClose();
              onCreateTask();
            },
            category: 'create' as const,
          },
        ]
      : []),
    ...(onCreateProject
      ? [
          {
            id: 'create-project',
            label: 'Create Project',
            description: 'Start a new project',
            icon: <KanbanBoard width={18} height={18} />,
            action: () => {
              onClose();
              onCreateProject();
            },
            category: 'create' as const,
          },
        ]
      : []),
    // Navigation
    ...COMMAND_NAV.map(item => ({
      id: `nav-${item.href}`,
      label: item.name,
      description: `Go to ${item.name}`,
      icon: item.icon,
      action: () => {
        onClose();
        router.push(item.href);
      },
      category: 'navigation' as const,
    })),
    // Actions
    {
      id: 'search',
      label: 'Search Knowledge',
      description: 'Search across all entities',
      icon: <Search width={18} height={18} />,
      shortcut: '/',
      action: () => {
        onClose();
        router.push('/search');
        // Focus search input after navigation
        setTimeout(() => {
          const input = document.getElementById('global-search');
          input?.focus();
        }, 100);
      },
      category: 'action',
    },
  ];

  // Filter commands by query
  const filteredCommands = query
    ? commands.filter(
        cmd =>
          cmd.label.toLowerCase().includes(query.toLowerCase()) ||
          cmd.description?.toLowerCase().includes(query.toLowerCase())
      )
    : commands;

  // Group by category
  const groupedCommands = filteredCommands.reduce(
    (acc, cmd) => {
      if (!acc[cmd.category]) acc[cmd.category] = [];
      acc[cmd.category].push(cmd);
      return acc;
    },
    {} as Record<string, CommandItem[]>
  );

  // Reset selection when query changes
  useEffect(() => {
    setSelectedIndex(0);
  }, []);

  // Focus input when opened
  useEffect(() => {
    if (isOpen) {
      setQuery('');
      setSelectedIndex(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [isOpen]);

  // Keyboard navigation
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          setSelectedIndex(i => Math.min(i + 1, filteredCommands.length - 1));
          break;
        case 'ArrowUp':
          e.preventDefault();
          setSelectedIndex(i => Math.max(i - 1, 0));
          break;
        case 'Enter':
          e.preventDefault();
          filteredCommands[selectedIndex]?.action();
          break;
        case 'Escape':
          e.preventDefault();
          onClose();
          break;
      }
    },
    [filteredCommands, selectedIndex, onClose]
  );

  if (!isOpen) return null;

  const categoryLabels: Record<string, string> = {
    create: 'Create',
    navigation: 'Navigate',
    action: 'Actions',
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[15vh]"
      role="presentation"
      onClick={onClose}
      onKeyDown={e => e.key === 'Escape' && onClose()}
    >
      {/* Backdrop */}
      <div className="absolute inset-0 bg-sc-bg-dark/80 backdrop-blur-sm" aria-hidden="true" />

      {/* Palette */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="relative w-full max-w-lg bg-sc-bg-base border border-sc-fg-subtle/30 rounded-xl shadow-2xl overflow-hidden"
        onClick={e => e.stopPropagation()}
        onKeyDown={e => e.stopPropagation()}
      >
        {/* Search input */}
        <div className="flex items-center gap-3 px-4 py-3 border-b border-sc-fg-subtle/20">
          <span className="text-sc-fg-subtle">⌘</span>
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Type a command or search..."
            className="flex-1 bg-transparent text-sc-fg-primary placeholder:text-sc-fg-subtle outline-none"
          />
          <kbd className="text-xs text-sc-fg-subtle bg-sc-bg-highlight px-1.5 py-0.5 rounded">
            esc
          </kbd>
        </div>

        {/* Commands list */}
        <div className="max-h-80 overflow-y-auto p-2">
          {filteredCommands.length === 0 ? (
            <div className="px-3 py-8 text-center text-sc-fg-subtle">No commands found</div>
          ) : (
            Object.entries(groupedCommands).map(([category, cmds]) => (
              <div key={category} className="mb-2 last:mb-0">
                <div className="px-3 py-1 text-xs font-medium text-sc-fg-subtle uppercase tracking-wider">
                  {categoryLabels[category] ?? category}
                </div>
                {cmds.map(cmd => {
                  const globalIndex = filteredCommands.indexOf(cmd);
                  const isSelected = globalIndex === selectedIndex;

                  return (
                    <button
                      key={cmd.id}
                      type="button"
                      onClick={cmd.action}
                      onMouseEnter={() => setSelectedIndex(globalIndex)}
                      className={`
                        w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left
                        transition-colors duration-75
                        ${
                          isSelected
                            ? 'bg-sc-purple/20 text-sc-purple'
                            : 'text-sc-fg-primary hover:bg-sc-bg-highlight'
                        }
                      `}
                    >
                      <span className="w-6 flex justify-center text-sc-fg-muted">{cmd.icon}</span>
                      <div className="flex-1 min-w-0">
                        <div className="font-medium truncate">{cmd.label}</div>
                        {cmd.description && (
                          <div className="text-xs text-sc-fg-muted truncate">{cmd.description}</div>
                        )}
                      </div>
                      {cmd.shortcut && (
                        <kbd className="text-xs text-sc-fg-subtle bg-sc-bg-elevated px-1.5 py-0.5 rounded border border-sc-fg-subtle/20">
                          {cmd.shortcut}
                        </kbd>
                      )}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t border-sc-fg-subtle/20 flex items-center gap-4 text-xs text-sc-fg-subtle">
          <span className="flex items-center gap-1">
            <kbd className="bg-sc-bg-highlight px-1 rounded">↑↓</kbd> navigate
          </span>
          <span className="flex items-center gap-1">
            <kbd className="bg-sc-bg-highlight px-1 rounded">↵</kbd> select
          </span>
          <span className="flex items-center gap-1">
            <kbd className="bg-sc-bg-highlight px-1 rounded">esc</kbd> close
          </span>
        </div>
      </div>
    </div>
  );
}

// Hook for global keyboard shortcuts
export function useKeyboardShortcuts(options: {
  onCommandPalette: () => void;
  onCreateTask?: () => void;
}) {
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Ignore if in input/textarea
      const target = e.target as HTMLElement;
      if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) {
        return;
      }

      // Cmd/Ctrl+K - Command palette
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        options.onCommandPalette();
        return;
      }

      // C - Create task (when not in input)
      if (e.key === 'c' && !e.metaKey && !e.ctrlKey && options.onCreateTask) {
        e.preventDefault();
        options.onCreateTask();
        return;
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [options]);
}
