'use client';

import { useEffect, useRef, useState } from 'react';
import { Card } from '@/components/ui/card';
import {
  Check,
  ChevronDown,
  Filter,
  Focus,
  Layers,
  Maximize2,
  Minimize2,
  MinusCircle,
  PlusCircle,
  RotateCcw,
  Search,
  X,
} from '@/components/ui/icons';
import { ENTITY_TYPES, getEntityColor } from '@/lib/constants/entities';
import type { ZoomLevelName } from './semantic-zoom';

const ENTITY_TYPE_LABELS: Record<string, string> = {
  task: 'Tasks',
  project: 'Projects',
  epic: 'Epics',
  pattern: 'Patterns',
  procedure: 'Procedures',
  episode: 'Episodes',
  topic: 'Topics',
  note: 'Notes',
  concept: 'Concepts',
  rule: 'Rules',
  template: 'Templates',
  guide: 'Guides',
  tool: 'Tools',
  language: 'Languages',
  source: 'Sources',
  document: 'Documents',
  file: 'Files',
  function: 'Functions',
  error_pattern: 'Errors',
  milestone: 'Milestones',
  team: 'Teams',
};

export function GraphToolbar({
  zoomLevel,
  onJumpToLevel,
  selectedClusterLabel,
  onClearCluster,
  onZoomIn,
  onZoomOut,
  onFitView,
  onReset,
  isFullscreen,
  onToggleFullscreen,
  searchTerm,
  onSearchChange,
  selectedTypes,
  onTypesChange,
  matchCount,
  nodeCount,
  edgeCount,
  includeShared,
  onIncludeSharedChange,
  sharedLabel,
  sharedAvailable,
  focusProjects,
  onFocusProjectsChange,
  focusedProjectCount,
  focusAvailable,
}: {
  zoomLevel: ZoomLevelName;
  onJumpToLevel: (level: 'domains' | 'entities') => void;
  selectedClusterLabel?: string | null;
  onClearCluster?: () => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFitView: () => void;
  onReset: () => void;
  isFullscreen: boolean;
  onToggleFullscreen: () => void;
  searchTerm: string;
  onSearchChange: (term: string) => void;
  selectedTypes: string[];
  onTypesChange: (types: string[]) => void;
  matchCount: number;
  nodeCount: number;
  edgeCount: number;
  includeShared?: boolean;
  onIncludeSharedChange?: (next: boolean) => void;
  sharedLabel?: string;
  sharedAvailable?: boolean;
  focusProjects?: boolean;
  onFocusProjectsChange?: (next: boolean) => void;
  focusedProjectCount?: number;
  focusAvailable?: boolean;
}) {
  const [typeDropdownOpen, setTypeDropdownOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const canToggleFocus = Boolean(focusAvailable && onFocusProjectsChange);
  const focusActive = Boolean(focusProjects);
  const canToggleShared = Boolean(focusActive && sharedAvailable && onIncludeSharedChange);
  const sharedActive = Boolean(includeShared);

  // Close dropdown on outside click
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setTypeDropdownOpen(false);
      }
    }
    if (typeDropdownOpen) {
      document.addEventListener('mousedown', handleClickOutside);
      return () => document.removeEventListener('mousedown', handleClickOutside);
    }
  }, [typeDropdownOpen]);

  const toggleType = (type: string) => {
    if (selectedTypes.includes(type)) {
      onTypesChange(selectedTypes.filter(t => t !== type));
    } else {
      onTypesChange([...selectedTypes, type]);
    }
  };

  const clearTypes = () => onTypesChange([]);

  const primaryTypes = [
    'task',
    'project',
    'epic',
    'pattern',
    'procedure',
    'episode',
    'topic',
    'note',
    'concept',
  ];
  const secondaryTypes = ENTITY_TYPES.filter(t => !primaryTypes.includes(t));

  return (
    <>
      {/* Mobile compact toolbar */}
      <div className="absolute top-2 left-2 right-2 z-10 flex items-center gap-2 md:hidden">
        <div className="flex-1 flex items-center justify-center gap-3 text-xs bg-sc-bg-base/90 rounded-lg px-3 py-2 border border-sc-fg-subtle/20">
          <span>
            <span className="text-sc-purple font-medium">{nodeCount}</span>
            <span className="text-sc-fg-subtle ml-1">nodes</span>
          </span>
          <span>
            <span className="text-sc-cyan font-medium">{edgeCount}</span>
            <span className="text-sc-fg-subtle ml-1">edges</span>
          </span>
        </div>
        {canToggleShared && (
          <button
            type="button"
            onClick={() => onIncludeSharedChange?.(!sharedActive)}
            aria-pressed={sharedActive}
            title={sharedActive ? 'Hide shared knowledge' : 'Include shared knowledge'}
            className={`p-2.5 rounded-lg border transition-colors ${
              sharedActive
                ? 'bg-sc-cyan/15 text-sc-cyan border-sc-cyan/40'
                : 'bg-sc-bg-base/90 text-sc-fg-subtle border-sc-fg-subtle/20 hover:text-sc-fg-primary'
            }`}
          >
            <Layers width={18} height={18} />
          </button>
        )}
        {canToggleFocus && (
          <button
            type="button"
            onClick={() => onFocusProjectsChange?.(!focusActive)}
            aria-pressed={focusActive}
            title={focusActive ? 'Show all projects in graph' : 'Focus graph to selected projects'}
            className={`p-2.5 rounded-lg border transition-colors ${
              focusActive
                ? 'bg-sc-purple/15 text-sc-purple border-sc-purple/40'
                : 'bg-sc-bg-base/90 text-sc-fg-subtle border-sc-fg-subtle/20 hover:text-sc-fg-primary'
            }`}
          >
            <Focus width={18} height={18} />
          </button>
        )}
        <button
          type="button"
          onClick={onToggleFullscreen}
          className="p-2.5 rounded-lg bg-sc-bg-base/90 text-sc-fg-subtle hover:text-sc-fg-primary border border-sc-fg-subtle/20 transition-colors"
        >
          {isFullscreen ? (
            <Minimize2 width={18} height={18} />
          ) : (
            <Maximize2 width={18} height={18} />
          )}
        </button>
      </div>

      {/* Desktop unified toolbar */}
      <div className="absolute top-4 left-4 z-10 hidden md:block">
        <Card className="!p-1.5 flex items-center gap-2">
          {/* Resolution toggle: aggregate overview vs. node detail */}
          <div className="flex items-center gap-1 rounded-lg bg-sc-bg-highlight/40 p-0.5">
            <button
              type="button"
              onClick={() => onJumpToLevel('domains')}
              aria-pressed={zoomLevel === 'domains'}
              className={`px-2.5 py-1 text-xs font-medium rounded-lg transition-colors ${
                zoomLevel === 'domains'
                  ? 'bg-sc-purple/20 text-sc-purple'
                  : 'text-sc-fg-muted hover:text-sc-fg-primary'
              } focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base`}
              title="Zoom out to the domain map"
            >
              Domains
            </button>
            <span
              className={`px-2 py-1 text-xs font-medium rounded-lg ${
                zoomLevel === 'mixed' ? 'bg-sc-cyan/15 text-sc-cyan' : 'text-sc-fg-subtle'
              }`}
              title="Zoom in on a domain to open it; the rest stay summarized"
            >
              Mixed
            </span>
            <button
              type="button"
              onClick={() => onJumpToLevel('entities')}
              aria-pressed={zoomLevel === 'entities'}
              className={`px-2.5 py-1 text-xs font-medium rounded-lg transition-colors ${
                zoomLevel === 'entities'
                  ? 'bg-sc-purple/20 text-sc-purple'
                  : 'text-sc-fg-muted hover:text-sc-fg-primary'
              } focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base`}
              title="Zoom in until every domain in view is open"
            >
              Entities
            </button>
          </div>

          {selectedClusterLabel && (
            <button
              type="button"
              onClick={onClearCluster}
              className="flex items-center gap-1 max-w-[12rem] px-2 py-1 text-xs rounded-lg bg-sc-cyan/10 text-sc-cyan hover:bg-sc-cyan/20 transition-colors"
              title="Back to all clusters"
            >
              <X width={12} height={12} className="flex-shrink-0" />
              <span className="truncate">{selectedClusterLabel}</span>
            </button>
          )}

          <div className="w-px h-5 bg-sc-fg-subtle/20" />

          {/* Zoom controls */}
          <div className="flex items-center gap-0.5">
            <button
              type="button"
              onClick={onZoomIn}
              className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
              title="Zoom in"
            >
              <PlusCircle width={16} height={16} />
            </button>
            <button
              type="button"
              onClick={onZoomOut}
              className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
              title="Zoom out"
            >
              <MinusCircle width={16} height={16} />
            </button>
            <button
              type="button"
              onClick={onFitView}
              className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
              title="Fit to view"
            >
              <Focus width={16} height={16} />
            </button>
            <button
              type="button"
              onClick={onReset}
              className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
              title="Reset view"
            >
              <RotateCcw width={16} height={16} />
            </button>
            <button
              type="button"
              onClick={onToggleFullscreen}
              className="p-1.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
              title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
            >
              {isFullscreen ? (
                <Minimize2 width={16} height={16} />
              ) : (
                <Maximize2 width={16} height={16} />
              )}
            </button>
          </div>

          {/* Divider */}
          <div className="w-px h-5 bg-sc-fg-subtle/20" />

          {/* Search input */}
          <div className="relative">
            <Search
              width={14}
              height={14}
              className="absolute left-2 top-1/2 -translate-y-1/2 text-sc-fg-subtle"
            />
            <input
              type="text"
              placeholder="Search nodes..."
              value={searchTerm}
              onChange={e => onSearchChange(e.target.value)}
              className="pl-7 pr-7 py-1 w-44 text-xs bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg focus-visible:outline-none focus-visible:border-sc-cyan focus-visible:ring-2 focus-visible:ring-sc-cyan/20 text-sc-fg-primary placeholder:text-sc-fg-subtle"
            />
            {searchTerm && (
              <button
                type="button"
                onClick={() => onSearchChange('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-sc-fg-subtle hover:text-sc-fg-primary"
              >
                <X width={12} height={12} />
              </button>
            )}
          </div>

          {/* Search result count */}
          {searchTerm && (
            <span className="text-xs text-sc-fg-muted whitespace-nowrap">
              {matchCount}/{nodeCount}
            </span>
          )}

          {/* Divider */}
          <div className="w-px h-5 bg-sc-fg-subtle/20" />

          {/* Entity type filter dropdown */}
          <div ref={dropdownRef} className="relative">
            <button
              type="button"
              onClick={() => setTypeDropdownOpen(!typeDropdownOpen)}
              className={`flex items-center gap-1.5 px-2 py-1 text-xs rounded-lg transition-colors ${
                selectedTypes.length > 0
                  ? 'bg-sc-purple/10 text-sc-purple'
                  : 'text-sc-fg-muted hover:text-sc-fg-primary'
              }`}
            >
              <Filter width={14} height={14} />
              <span>Types</span>
              {selectedTypes.length > 0 && (
                <span className="px-1 rounded bg-sc-purple/20 text-[10px]">
                  {selectedTypes.length}
                </span>
              )}
              <ChevronDown
                width={12}
                height={12}
                className={`transition-transform ${typeDropdownOpen ? 'rotate-180' : ''}`}
              />
            </button>

            {typeDropdownOpen && (
              <div className="absolute top-full left-0 mt-1 w-56 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl shadow-lg overflow-hidden z-50 animate-fade-in">
                {selectedTypes.length > 0 && (
                  <>
                    <button
                      type="button"
                      onClick={clearTypes}
                      className="w-full flex items-center justify-center gap-2 px-3 py-2 text-xs text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-elevated transition-colors"
                    >
                      <X width={12} height={12} />
                      Clear filter
                    </button>
                    <div className="border-t border-sc-fg-subtle/10" />
                  </>
                )}
                <div className="max-h-64 overflow-y-auto p-2 space-y-0.5">
                  {primaryTypes.map(type => {
                    const isSelected = selectedTypes.includes(type);
                    const color = getEntityColor(type);
                    return (
                      <button
                        key={type}
                        type="button"
                        onClick={() => toggleType(type)}
                        className={`w-full flex items-center gap-2 px-2 py-1.5 rounded text-xs transition-colors ${
                          isSelected
                            ? 'bg-sc-purple/10 text-sc-fg-primary'
                            : 'text-sc-fg-muted hover:bg-sc-bg-elevated hover:text-sc-fg-primary'
                        }`}
                      >
                        <div
                          className={`w-3.5 h-3.5 rounded border flex items-center justify-center flex-shrink-0 transition-colors ${
                            isSelected ? 'bg-sc-purple border-sc-purple' : 'border-sc-fg-subtle/40'
                          }`}
                        >
                          {isSelected && <Check width={10} height={10} className="text-white" />}
                        </div>
                        <div
                          className="w-2.5 h-2.5 rounded-full flex-shrink-0"
                          style={{ backgroundColor: color }}
                        />
                        <span className="flex-1 text-left">{ENTITY_TYPE_LABELS[type] || type}</span>
                      </button>
                    );
                  })}
                  <div className="border-t border-sc-fg-subtle/10 my-1" />
                  {secondaryTypes.map(type => {
                    const isSelected = selectedTypes.includes(type);
                    const color = getEntityColor(type);
                    return (
                      <button
                        key={type}
                        type="button"
                        onClick={() => toggleType(type)}
                        className={`w-full flex items-center gap-2 px-2 py-1.5 rounded text-xs transition-colors ${
                          isSelected
                            ? 'bg-sc-purple/10 text-sc-fg-primary'
                            : 'text-sc-fg-muted hover:bg-sc-bg-elevated hover:text-sc-fg-primary'
                        }`}
                      >
                        <div
                          className={`w-3.5 h-3.5 rounded border flex items-center justify-center flex-shrink-0 transition-colors ${
                            isSelected ? 'bg-sc-purple border-sc-purple' : 'border-sc-fg-subtle/40'
                          }`}
                        >
                          {isSelected && <Check width={10} height={10} className="text-white" />}
                        </div>
                        <div
                          className="w-2.5 h-2.5 rounded-full flex-shrink-0"
                          style={{ backgroundColor: color }}
                        />
                        <span className="flex-1 text-left">{ENTITY_TYPE_LABELS[type] || type}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>

          {canToggleFocus && (
            <>
              <div className="w-px h-5 bg-sc-fg-subtle/20" />
              <button
                type="button"
                onClick={() => onFocusProjectsChange?.(!focusActive)}
                aria-pressed={focusActive}
                title={
                  focusActive ? 'Show all projects in graph' : 'Focus graph to selected projects'
                }
                className={`flex items-center gap-1.5 px-2 py-1 text-xs rounded-lg transition-colors ${
                  focusActive
                    ? 'bg-sc-purple/15 text-sc-purple'
                    : 'text-sc-fg-muted hover:text-sc-fg-primary'
                }`}
              >
                <Focus width={14} height={14} />
                <span>
                  {focusActive
                    ? `Focused (${focusedProjectCount || 0})`
                    : `Focus (${focusedProjectCount || 0})`}
                </span>
              </button>
            </>
          )}

          {canToggleShared && (
            <>
              <div className="w-px h-5 bg-sc-fg-subtle/20" />
              <button
                type="button"
                onClick={() => onIncludeSharedChange?.(!sharedActive)}
                aria-pressed={sharedActive}
                title={sharedActive ? 'Hide shared knowledge' : 'Include shared knowledge'}
                className={`flex items-center gap-1.5 px-2 py-1 text-xs rounded-lg transition-colors ${
                  sharedActive
                    ? 'bg-sc-cyan/15 text-sc-cyan'
                    : 'text-sc-fg-muted hover:text-sc-fg-primary'
                }`}
              >
                <Layers width={14} height={14} />
                <span>{sharedLabel || 'Shared'}</span>
              </button>
            </>
          )}
        </Card>
      </div>

      {/* Mobile zoom controls (bottom) */}
      <div className="absolute bottom-4 right-4 z-10 flex md:hidden">
        <Card className="!p-1 flex items-center gap-1">
          <button
            type="button"
            onClick={onZoomOut}
            className="p-2.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
          >
            <MinusCircle width={20} height={20} />
          </button>
          <button
            type="button"
            onClick={onFitView}
            className="p-2.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
          >
            <Focus width={20} height={20} />
          </button>
          <button
            type="button"
            onClick={onZoomIn}
            className="p-2.5 rounded hover:bg-sc-bg-highlight text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
          >
            <PlusCircle width={20} height={20} />
          </button>
        </Card>
      </div>
    </>
  );
}
