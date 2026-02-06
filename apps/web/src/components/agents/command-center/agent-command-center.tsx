'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useMemo } from 'react';
import { Group, type Layout, Panel } from 'react-resizable-panels';
import { AgentChatPanel } from '@/components/agents/agent-chat-panel';
import type { Agent } from '@/lib/api';
import { useAgent, useMediaQuery } from '@/lib/hooks';
import { readStorage, writeStorage } from '@/lib/storage';
import { AgentNavigator } from './agent-navigator';
import { DashboardView } from './dashboard-view';
import { InspectorPanel } from './inspector-panel';
import { ResizeHandle } from './resize-handle';

// =============================================================================
// Types
// =============================================================================

interface AgentCommandCenterProps {
  agents: Agent[];
  projects: Array<{ id: string; name: string }>;
  projectFilter?: string;
  isLoading: boolean;
  error: Error | null;
}

// Layout persistence helpers
function loadLayout(key: string, fallback: Layout): Layout {
  return readStorage<Layout>(key) ?? fallback;
}

function saveLayout(key: string) {
  return (layout: Layout) => writeStorage(key, layout);
}

const DEFAULT_DESKTOP_LAYOUT: Layout = { nav: 18, center: 58, inspector: 24 };
const DEFAULT_TABLET_LAYOUT: Layout = { nav: 25, center: 75 };

// =============================================================================
// Agent Command Center
// =============================================================================

export function AgentCommandCenter({ agents, projects, projectFilter }: AgentCommandCenterProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const isDesktop = useMediaQuery('(min-width: 1024px)');
  const isTablet = useMediaQuery('(min-width: 768px)');

  // Persisted layouts — read once on mount, save on change
  const desktopLayout = useMemo(
    () => loadLayout('agents:desktop-layout', DEFAULT_DESKTOP_LAYOUT),
    []
  );
  const tabletLayout = useMemo(() => loadLayout('agents:tablet-layout', DEFAULT_TABLET_LAYOUT), []);
  const saveDesktopLayout = useMemo(() => saveLayout('agents:desktop-layout'), []);
  const saveTabletLayout = useMemo(() => saveLayout('agents:tablet-layout'), []);

  // Selected agent from URL
  const selectedAgentId = searchParams.get('id');
  const { data: selectedAgent } = useAgent(selectedAgentId ?? '');

  // Navigate to agent (update URL search param)
  const handleSelectAgent = useCallback(
    (id: string | null) => {
      const params = new URLSearchParams(searchParams);
      if (id) {
        params.set('id', id);
      } else {
        params.delete('id');
      }
      const query = params.toString();
      router.push(query ? `/agents?${query}` : '/agents', { scroll: false });
    },
    [router, searchParams]
  );

  // Handle spawned agent — select it immediately
  const handleSpawned = useCallback(
    (id: string) => {
      handleSelectAgent(id);
    },
    [handleSelectAgent]
  );

  // Sorted agent list for keyboard nav
  const sortedAgentIds = useMemo(() => {
    const twoMinutesAgo = Date.now() - 2 * 60 * 1000;
    return [...agents]
      .sort((a, b) => {
        const aActive = a.last_heartbeat && new Date(a.last_heartbeat).getTime() >= twoMinutesAgo;
        const bActive = b.last_heartbeat && new Date(b.last_heartbeat).getTime() >= twoMinutesAgo;
        if (aActive && !bActive) return -1;
        if (!aActive && bActive) return 1;
        const aTime = a.last_heartbeat ? new Date(a.last_heartbeat).getTime() : 0;
        const bTime = b.last_heartbeat ? new Date(b.last_heartbeat).getTime() : 0;
        return bTime - aTime;
      })
      .map(a => a.id);
  }, [agents]);

  // Keyboard shortcuts
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      const target = e.target as HTMLElement;
      if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) {
        return;
      }

      if (e.key === 'Escape' && selectedAgentId) {
        e.preventDefault();
        handleSelectAgent(null);
        return;
      }

      const isCtrlOrCmd = e.ctrlKey || e.metaKey;

      if (isCtrlOrCmd && e.key === ']') {
        e.preventDefault();
        if (sortedAgentIds.length === 0) return;
        if (!selectedAgentId) {
          handleSelectAgent(sortedAgentIds[0]);
        } else {
          const idx = sortedAgentIds.indexOf(selectedAgentId);
          const next = sortedAgentIds[(idx + 1) % sortedAgentIds.length];
          handleSelectAgent(next);
        }
        return;
      }

      if (isCtrlOrCmd && e.key === '[') {
        e.preventDefault();
        if (sortedAgentIds.length === 0) return;
        if (!selectedAgentId) {
          handleSelectAgent(sortedAgentIds[sortedAgentIds.length - 1]);
        } else {
          const idx = sortedAgentIds.indexOf(selectedAgentId);
          const prev = sortedAgentIds[(idx - 1 + sortedAgentIds.length) % sortedAgentIds.length];
          handleSelectAgent(prev);
        }
        return;
      }
    }

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectedAgentId, sortedAgentIds, handleSelectAgent]);

  // Desktop: 3-pane layout
  if (isDesktop) {
    return (
      <div className="h-full animate-fade-in">
        <Group
          orientation="horizontal"
          id="agent-command-center"
          defaultLayout={desktopLayout}
          onLayoutChanged={saveDesktopLayout}
        >
          {/* Navigator */}
          <Panel id="nav" defaultSize={18} minSize={12} collapsible collapsedSize={0}>
            <AgentNavigator
              agents={agents}
              projects={projects}
              selectedAgentId={selectedAgentId}
              onSelectAgent={handleSelectAgent}
              onSpawned={handleSpawned}
            />
          </Panel>

          <ResizeHandle />

          {/* Center pane */}
          <Panel id="center" defaultSize={58} minSize={30}>
            {selectedAgentId && selectedAgent ? (
              <div className="h-full overflow-hidden">
                <AgentChatPanel agent={selectedAgent} />
              </div>
            ) : (
              <DashboardView
                agents={agents}
                projectFilter={projectFilter}
                onSelectAgent={id => handleSelectAgent(id)}
              />
            )}
          </Panel>

          <ResizeHandle />

          {/* Inspector */}
          <Panel id="inspector" defaultSize={24} minSize={15} collapsible collapsedSize={0}>
            <InspectorPanel
              agent={selectedAgent ?? null}
              agents={agents}
              projectFilter={projectFilter}
              onSelectAgent={id => handleSelectAgent(id)}
            />
          </Panel>
        </Group>
      </div>
    );
  }

  // Tablet: 2-pane (nav + center), no inspector
  if (isTablet) {
    return (
      <div className="h-full animate-fade-in">
        <Group
          orientation="horizontal"
          id="agent-command-center-tablet"
          defaultLayout={tabletLayout}
          onLayoutChanged={saveTabletLayout}
        >
          <Panel id="nav" defaultSize={25} minSize={15} collapsible collapsedSize={0}>
            <AgentNavigator
              agents={agents}
              projects={projects}
              selectedAgentId={selectedAgentId}
              onSelectAgent={handleSelectAgent}
              onSpawned={handleSpawned}
            />
          </Panel>

          <ResizeHandle />

          <Panel id="center" defaultSize={75} minSize={40}>
            {selectedAgentId && selectedAgent ? (
              <div className="h-full overflow-hidden">
                <AgentChatPanel agent={selectedAgent} />
              </div>
            ) : (
              <DashboardView
                agents={agents}
                projectFilter={projectFilter}
                onSelectAgent={id => handleSelectAgent(id)}
              />
            )}
          </Panel>
        </Group>
      </div>
    );
  }

  // Mobile: single pane
  return (
    <div className="h-full animate-fade-in">
      {selectedAgentId && selectedAgent ? (
        <div className="h-full flex flex-col overflow-hidden">
          <button
            type="button"
            onClick={() => handleSelectAgent(null)}
            className="shrink-0 flex items-center gap-1.5 px-3 py-2 text-xs text-sc-purple hover:text-sc-purple/80 transition-colors"
          >
            ← Back to agents
          </button>
          <div className="flex-1 min-h-0 overflow-hidden">
            <AgentChatPanel agent={selectedAgent} />
          </div>
        </div>
      ) : (
        <div className="h-full overflow-y-auto">
          <AgentNavigator
            agents={agents}
            projects={projects}
            selectedAgentId={selectedAgentId}
            onSelectAgent={handleSelectAgent}
            onSpawned={handleSpawned}
          />
        </div>
      )}
    </div>
  );
}
