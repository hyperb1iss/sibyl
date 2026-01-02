'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { IconComponent } from '@/components/ui/icons';
import {
  Check,
  ChevronDown,
  Code,
  EditPencil,
  Folder,
  Globe,
  List,
  Page,
  Pause,
  Play,
  Search,
  Send,
  Settings,
  Square,
  User,
  Xmark,
} from '@/components/ui/icons';
import { Markdown } from '@/components/ui/markdown';
import { Spinner } from '@/components/ui/spinner';
import type { Agent, AgentMessage as ApiMessage } from '@/lib/api';
import {
  AGENT_STATUS_CONFIG,
  AGENT_TYPE_CONFIG,
  type AgentStatusType,
  type AgentTypeValue,
} from '@/lib/constants';
import {
  useAgentMessages,
  useAgentSubscription,
  usePauseAgent,
  useResumeAgent,
  useSendAgentMessage,
  useTerminateAgent,
} from '@/lib/hooks';
import { ToolContentRenderer } from './tool-renderers';

// =============================================================================
// Types & Constants
// =============================================================================

// Map icon names from backend to components
const TOOL_ICONS: Record<string, IconComponent> = {
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

// Playful status templates per tool - {file}, {pattern}, {cmd} get substituted
const TOOL_STATUS_TEMPLATES: Record<string, string[]> = {
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

// Helper to get a playful tool status with substitution
function getToolStatus(toolName: string, input?: Record<string, unknown>): string | null {
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

// Clever waiting phrases - grouped by mood, picked once per waiting session
const THINKING_PHRASES = {
  // Focused but friendly
  focused: [
    'Reasoning through this',
    'Mapping the terrain',
    'Tracing the threads',
    'Connecting the pieces',
    'Following the breadcrumbs',
  ],
  // Playful and fun
  playful: [
    'Consulting the cosmic wiki',
    'Asking the rubber duck',
    'Summoning the muse',
    'Channeling the void',
    'Brewing some magic',
    'Spinning up neurons',
    'Wrangling electrons',
  ],
  // Mystical vibes
  mystical: [
    'Reading the tea leaves',
    'Divining the path forward',
    'Peering into the matrix',
    'Tapping the akashic records',
    'Communing with the codebase',
  ],
  // Cheeky dev humor
  cheeky: [
    'Hold my coffee',
    'One sec, almost there',
    'Trust the process',
    'Working some magic here',
    'Doing the thing',
  ],
};

const ALL_THINKING_PHRASES = [
  ...THINKING_PHRASES.focused,
  ...THINKING_PHRASES.playful,
  ...THINKING_PHRASES.mystical,
  ...THINKING_PHRASES.cheeky,
];

function ThinkingIndicator() {
  // Pick a random phrase once when component mounts - no cycling
  const [phrase] = useState(
    () => ALL_THINKING_PHRASES[Math.floor(Math.random() * ALL_THINKING_PHRASES.length)]
  );
  const [dotCount, setDotCount] = useState(1);

  // Animate dots
  useEffect(() => {
    const dotInterval = setInterval(() => {
      setDotCount(prev => (prev % 3) + 1);
    }, 400);
    return () => clearInterval(dotInterval);
  }, []);

  const dots = '.'.repeat(dotCount).padEnd(3, '\u00A0');

  return (
    <div className="flex items-center gap-3 px-4 py-3 rounded-lg bg-gradient-to-r from-sc-purple/10 via-sc-cyan/5 to-sc-purple/10 border border-sc-purple/30 animate-slide-up relative overflow-hidden">
      {/* Subtle shimmer overlay */}
      <div className="absolute inset-0 bg-gradient-to-r from-transparent via-sc-purple/5 to-transparent animate-shimmer-slow pointer-events-none" />

      {/* Subtle glow */}
      <div className="relative">
        <div className="absolute inset-0 bg-sc-purple/15 rounded-full blur-sm" />
        <Spinner size="sm" className="text-sc-purple relative z-10" />
      </div>

      <span className="text-sm text-sc-fg-muted font-medium">
        <span className="text-sc-purple">{phrase}</span>
        <span className="text-sc-cyan font-mono">{dots}</span>
      </span>
    </div>
  );
}

// Empty chat state with personality
function EmptyChatState({ agentName }: { agentName: string }) {
  return (
    <div className="flex flex-col items-center justify-center h-full py-12 px-4 animate-fade-in">
      <div className="relative mb-6">
        {/* Floating glow behind icon */}
        <div className="absolute inset-0 bg-gradient-to-br from-sc-purple/20 to-sc-cyan/20 rounded-full blur-xl animate-glow-pulse" />
        <div className="relative bg-gradient-to-br from-sc-purple/10 to-sc-cyan/10 p-6 rounded-2xl border border-sc-purple/20">
          <div className="text-4xl animate-float">
            <svg
              width="48"
              height="48"
              viewBox="0 0 24 24"
              fill="none"
              className="text-sc-purple"
              aria-hidden="true"
            >
              <path
                d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"
                fill="currentColor"
                opacity="0.2"
              />
              <circle cx="12" cy="12" r="3" fill="currentColor" className="animate-pulse" />
              <path
                d="M12 2v2m0 16v2M2 12h2m16 0h2"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
            </svg>
          </div>
        </div>
      </div>

      <h3 className="text-lg font-semibold text-sc-fg-primary mb-2">{agentName} is ready</h3>
      <p className="text-sm text-sc-fg-muted text-center max-w-xs">
        This agent is standing by. Watch the magic unfold as it works through its task.
      </p>
    </div>
  );
}

interface ChatMessage {
  id: string;
  role: 'agent' | 'user' | 'system';
  content: string;
  timestamp: Date;
  type?: 'text' | 'tool_call' | 'tool_result' | 'error';
  metadata?: Record<string, unknown>;
}

// Subagent data for rendering
interface SubagentData {
  taskCall: ChatMessage;
  taskResult?: ChatMessage;
  nestedCalls: ChatMessage[];
  pollingCalls?: ChatMessage[]; // TaskOutput calls polling this background agent
  lastPollStatus?: 'running' | 'completed' | 'failed';
}

// Grouped message types for rendering
type MessageGroup =
  | { kind: 'message'; message: ChatMessage; pairedResult?: ChatMessage }
  | {
      kind: 'subagent';
      taskCall: ChatMessage;
      taskResult?: ChatMessage;
      nestedCalls: ChatMessage[];
      resultsByToolId: Map<string, ChatMessage>;
      pollingCalls?: ChatMessage[]; // TaskOutput calls for background agents
    }
  | {
      kind: 'parallel_subagents';
      subagents: SubagentData[];
      resultsByToolId: Map<string, ChatMessage>;
    };

// =============================================================================
// Sub-components
// =============================================================================

function AgentHeader({ agent }: { agent: Agent }) {
  const pauseAgent = usePauseAgent();
  const resumeAgent = useResumeAgent();
  const terminateAgent = useTerminateAgent();

  const statusConfig =
    AGENT_STATUS_CONFIG[agent.status as AgentStatusType] ?? AGENT_STATUS_CONFIG.working;
  const typeConfig =
    AGENT_TYPE_CONFIG[agent.agent_type as AgentTypeValue] ?? AGENT_TYPE_CONFIG.general;

  const isActive = ['initializing', 'working', 'resuming'].includes(agent.status);
  const isPaused = agent.status === 'paused';
  const isTerminal = ['completed', 'failed', 'terminated'].includes(agent.status);

  return (
    <div className="shrink-0 flex items-center justify-between px-3 py-2 border-b border-sc-fg-subtle/20 bg-sc-bg-elevated">
      {/* Left: Icon + Name + Status */}
      <div className="flex items-center gap-2 min-w-0">
        <span
          className="text-sm transition-transform duration-200 hover:scale-110"
          style={{ color: typeConfig.color }}
        >
          {typeConfig.icon}
        </span>
        <span className="text-sm font-medium text-sc-fg-primary truncate">{agent.name}</span>
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded transition-all duration-200 ${statusConfig.bgClass} ${statusConfig.textClass} ${
            isActive ? 'animate-pulse-glow' : ''
          }`}
        >
          {statusConfig.icon} {statusConfig.label}
        </span>
      </div>

      {/* Right: Controls with micro-interactions */}
      <div className="flex items-center gap-1 shrink-0">
        {isActive && (
          <button
            type="button"
            onClick={() => pauseAgent.mutate({ id: agent.id })}
            className="p-1.5 text-sc-yellow hover:bg-sc-yellow/10 rounded transition-all duration-200 hover:scale-110 active:scale-95 disabled:opacity-50 disabled:pointer-events-none"
            disabled={pauseAgent.isPending}
            title="Pause"
          >
            <Pause width={14} height={14} />
          </button>
        )}
        {isPaused && (
          <button
            type="button"
            onClick={() => resumeAgent.mutate(agent.id)}
            className="p-1.5 text-sc-green hover:bg-sc-green/10 rounded transition-all duration-200 hover:scale-110 active:scale-95 disabled:opacity-50 disabled:pointer-events-none"
            disabled={resumeAgent.isPending}
            title="Resume"
          >
            <Play width={14} height={14} />
          </button>
        )}
        {!isTerminal && (
          <button
            type="button"
            onClick={() => terminateAgent.mutate({ id: agent.id })}
            className="p-1.5 text-sc-red hover:bg-sc-red/10 rounded transition-all duration-200 hover:scale-110 active:scale-95 disabled:opacity-50 disabled:pointer-events-none"
            disabled={terminateAgent.isPending}
            title="Stop"
          >
            <Square width={14} height={14} />
          </button>
        )}
      </div>
    </div>
  );
}

interface ToolMessageProps {
  message: ChatMessage;
  result?: ChatMessage; // Paired result for tool calls
  isNew?: boolean; // For entrance animation
}

function ToolMessage({ message, result, isNew = false }: ToolMessageProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);

  const iconName = message.metadata?.icon as string | undefined;
  const toolName = message.metadata?.tool_name as string | undefined;
  const Icon = iconName ? TOOL_ICONS[iconName] : Code;
  const input = message.metadata?.input as Record<string, unknown> | undefined;

  // Memoize playful status so it doesn't change on re-render
  const playfulStatus = useMemo(
    () => (toolName ? getToolStatus(toolName, input) : null),
    [toolName, input]
  );

  // For results, check error status
  const resultError = result?.metadata?.is_error as boolean | undefined;
  const hasResult = !!result;

  // Get result preview (short)
  const getResultPreview = () => {
    if (!result) return null;
    const content = result.content;
    const firstLine = content.split('\n')[0] || '';
    // For file counts, errors, etc - show brief
    if (firstLine.length < 60) return firstLine;
    return `${firstLine.slice(0, 50)}...`;
  };

  const resultPreview = getResultPreview();
  const hasExpandableContent = message.content.length > 100 || (result?.content?.length ?? 0) > 100;

  // Status indicator color
  const statusClass = !hasResult
    ? 'text-sc-purple' // Pending
    : resultError
      ? 'text-sc-red' // Error
      : 'text-sc-green'; // Success

  return (
    <div
      className={`rounded-md font-mono text-xs overflow-hidden transition-all duration-300 ${isNew ? 'animate-slide-up' : ''} ${
        isExpanded
          ? 'bg-sc-bg-elevated ring-1 ring-sc-purple/30 shadow-lg shadow-sc-purple/5'
          : 'bg-sc-bg-elevated/50 hover:bg-sc-bg-elevated/80'
      }`}
    >
      {/* Header row with tool name and preview */}
      <button
        type="button"
        onClick={() => hasExpandableContent && setIsExpanded(!isExpanded)}
        className={`w-full flex items-center gap-1.5 px-2 py-1.5 text-left transition-all duration-200 group ${
          hasExpandableContent ? 'cursor-pointer hover:bg-sc-fg-subtle/5' : ''
        }`}
      >
        <Icon
          width={12}
          height={12}
          className={`${statusClass} shrink-0 transition-all duration-200 ${isExpanded ? 'scale-110' : ''} ${!hasResult ? 'animate-pulse' : ''}`}
        />
        <span className={`${statusClass} font-medium shrink-0 transition-colors duration-200`}>
          {toolName || 'Tool'}
        </span>
        <span className="text-sc-fg-muted truncate flex-1 min-w-0 group-hover:text-sc-fg-primary transition-colors duration-200">
          {/* Show playful status when pending, raw content when complete */}
          {!hasResult && playfulStatus ? (
            <span className="italic">{playfulStatus}</span>
          ) : (
            message.content
          )}
        </span>
        {/* Result indicator with entrance animation */}
        {hasResult && (
          <span className={`text-[10px] shrink-0 ${statusClass} animate-fade-in`}>
            {resultError ? '' : ''} {resultPreview?.slice(0, 20)}
            {resultPreview && resultPreview.length > 20 ? '...' : ''}
          </span>
        )}
        <span className="text-[10px] text-sc-fg-subtle shrink-0 tabular-nums ml-1">
          {message.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </span>
        {hasExpandableContent && (
          <ChevronDown
            width={12}
            height={12}
            className={`text-sc-fg-subtle shrink-0 transition-transform duration-300 ${isExpanded ? 'rotate-180' : ''}`}
          />
        )}
      </button>

      {/* Animated expandable content */}
      <div
        ref={contentRef}
        className={`overflow-hidden transition-all duration-300 ease-out ${
          isExpanded ? 'max-h-[500px] opacity-100' : 'max-h-0 opacity-0'
        }`}
      >
        <div className="border-t border-sc-fg-subtle/10 p-2">
          {/* Use smart renderer for code tools, fallback to simple display */}
          {toolName && ['Read', 'Edit', 'Write', 'Bash', 'Grep', 'Glob'].includes(toolName) ? (
            <ToolContentRenderer
              toolName={toolName}
              input={message.metadata?.input as Record<string, unknown> | undefined}
              result={(result?.metadata?.full_content as string | undefined) || result?.content}
              isError={resultError}
            />
          ) : result ? (
            <pre
              className={`whitespace-pre-wrap break-words text-[11px] leading-relaxed p-2 rounded ${
                resultError ? 'bg-sc-red/5 text-sc-red' : 'bg-sc-bg-dark text-sc-fg-primary'
              }`}
            >
              {(result.metadata?.full_content as string | undefined) || result.content}
            </pre>
          ) : (
            <pre className="whitespace-pre-wrap break-words text-[11px] leading-relaxed p-2 rounded bg-sc-bg-dark text-sc-fg-primary">
              {message.content}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}

interface SubagentBlockProps {
  taskCall: ChatMessage;
  taskResult?: ChatMessage;
  nestedCalls: ChatMessage[];
  resultsByToolId: Map<string, ChatMessage>;
  pollingCalls?: ChatMessage[]; // TaskOutput calls for background agents
}

function SubagentBlock({
  taskCall,
  taskResult,
  nestedCalls,
  resultsByToolId,
  pollingCalls = [],
}: SubagentBlockProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);

  // Extract metadata from task call
  const description = taskCall.content || 'Subagent task';
  const shortDescription = description.length > 50 ? `${description.slice(0, 47)}...` : description;
  const agentType = taskCall.metadata?.subagent_type as string | undefined;
  const isBackground = taskCall.metadata?.run_in_background as boolean | undefined;

  // Get latest poll status for background agents
  const latestPollResult =
    pollingCalls.length > 0
      ? resultsByToolId.get(pollingCalls[pollingCalls.length - 1].metadata?.tool_id as string)
      : undefined;
  const pollStatus = latestPollResult?.metadata?.status as
    | 'running'
    | 'completed'
    | 'failed'
    | undefined;
  const pollCount = pollingCalls.length;

  // Count tool calls
  const toolCount = nestedCalls.length;

  // Check status
  const hasResult = !!taskResult;
  const isWorking = !hasResult;
  const isError = taskResult?.metadata?.is_error as boolean | undefined;

  // Extract result metadata (cost, duration, etc.)
  const resultMeta = taskResult?.metadata as
    | { duration_ms?: number; total_cost_usd?: number }
    | undefined;
  const durationMs = resultMeta?.duration_ms;
  const costUsd = resultMeta?.total_cost_usd;

  // Get 2 most recent tool calls for live preview (only when working)
  const recentCalls = isWorking ? nestedCalls.slice(-2) : [];

  // Auto-scroll preview when new calls come in
  // biome-ignore lint/correctness/useExhaustiveDependencies: trigger on nested call count
  useEffect(() => {
    if (previewRef.current && isWorking) {
      previewRef.current.scrollTop = previewRef.current.scrollHeight;
    }
  }, [nestedCalls.length, isWorking]);

  // Format duration
  const formatDuration = (ms: number) => {
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    return `${(ms / 60000).toFixed(1)}m`;
  };

  return (
    <div
      className={`rounded-lg border overflow-hidden transition-all duration-300 animate-slide-up ${
        isWorking
          ? 'border-sc-purple/40 bg-gradient-to-r from-sc-purple/10 via-sc-cyan/5 to-sc-purple/10 shadow-lg shadow-sc-purple/10'
          : isError
            ? 'border-sc-red/30 bg-sc-red/5'
            : 'border-sc-green/20 bg-sc-bg-elevated/30'
      }`}
    >
      {/* Header */}
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-sc-fg-subtle/5 transition-all duration-200 group"
      >
        {/* Icon - use poll status for background agents */}
        {isBackground && pollStatus ? (
          pollStatus === 'running' ? (
            <Spinner size="sm" className="text-sc-cyan shrink-0" />
          ) : pollStatus === 'failed' ? (
            <Xmark width={14} height={14} className="text-sc-red shrink-0" />
          ) : (
            <div className="animate-bounce-in">
              <Check width={14} height={14} className="text-sc-green shrink-0" />
            </div>
          )
        ) : isWorking ? (
          <Spinner size="sm" className="text-sc-purple shrink-0" />
        ) : isError ? (
          <Xmark width={14} height={14} className="text-sc-red shrink-0 animate-wiggle" />
        ) : (
          <div className="animate-bounce-in">
            <Check width={14} height={14} className="text-sc-green shrink-0" />
          </div>
        )}

        {/* Agent type badge */}
        {agentType && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-sc-purple/20 text-sc-purple font-medium shrink-0 transition-transform duration-200 group-hover:scale-105">
            {agentType}
          </span>
        )}

        {/* Background indicator with poll count */}
        {isBackground && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-sc-cyan/20 text-sc-cyan shrink-0 flex items-center gap-1">
            bg
            {pollCount > 0 && <span className="text-sc-cyan/70">({pollCount}x)</span>}
          </span>
        )}

        <span className="text-xs text-sc-fg-muted truncate flex-1 min-w-0 group-hover:text-sc-fg-primary transition-colors duration-200">
          {shortDescription}
        </span>

        {/* Tool count with activity indicator */}
        {toolCount > 0 && (
          <span
            className={`text-[10px] text-sc-fg-subtle shrink-0 tabular-nums transition-colors duration-200 ${isWorking ? 'text-sc-purple' : ''}`}
          >
            {toolCount} tool{toolCount !== 1 ? 's' : ''}
          </span>
        )}

        {/* Status - use poll status for background agents */}
        {isBackground && pollStatus ? (
          pollStatus === 'running' ? (
            <span className="text-[10px] text-sc-yellow font-medium animate-pulse">running</span>
          ) : pollStatus === 'failed' ? (
            <span className="text-[10px] text-sc-red font-medium">failed</span>
          ) : (
            <span className="text-[10px] text-sc-green font-medium animate-fade-in">done</span>
          )
        ) : isWorking ? (
          <span className="text-[10px] text-sc-yellow font-medium animate-pulse">working</span>
        ) : isError ? (
          <span className="text-[10px] text-sc-red font-medium">failed</span>
        ) : (
          <span className="text-[10px] text-sc-green font-medium animate-fade-in">done</span>
        )}

        {/* Duration (when complete) */}
        {durationMs && (
          <span className="text-[10px] text-sc-fg-subtle shrink-0 tabular-nums animate-fade-in">
            {formatDuration(durationMs)}
          </span>
        )}

        {/* Cost (when available) */}
        {costUsd && costUsd > 0 && (
          <span className="text-[10px] text-sc-coral shrink-0 tabular-nums animate-fade-in">
            ${costUsd.toFixed(4)}
          </span>
        )}

        {/* Timestamp */}
        <span className="text-[10px] text-sc-fg-subtle shrink-0 tabular-nums">
          {taskCall.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </span>

        <ChevronDown
          width={14}
          height={14}
          className={`text-sc-fg-subtle shrink-0 transition-transform duration-300 ${isExpanded ? 'rotate-180' : ''}`}
        />
      </button>

      {/* Live preview (only when working and not expanded) */}
      {!isExpanded && isWorking && recentCalls.length > 0 && (
        <div
          ref={previewRef}
          className="px-3 pb-2 space-y-0.5 border-t border-sc-purple/10 max-h-20 overflow-y-auto"
        >
          {recentCalls.map(call => {
            const pairedResult = resultsByToolId.get(call.metadata?.tool_id as string);
            const iconName = call.metadata?.icon as string | undefined;
            const toolName = call.metadata?.tool_name as string | undefined;
            const Icon = iconName ? TOOL_ICONS[iconName] : Code;
            const hasCallResult = !!pairedResult;
            const callError = pairedResult?.metadata?.is_error as boolean | undefined;
            const statusClass = !hasCallResult
              ? 'text-sc-purple'
              : callError
                ? 'text-sc-red'
                : 'text-sc-green';

            return (
              <div
                key={call.id}
                className="flex items-center gap-1.5 text-[10px] font-mono py-0.5 animate-slide-up"
              >
                {!hasCallResult ? (
                  <Spinner size="sm" className="text-sc-purple" />
                ) : (
                  <Icon width={10} height={10} className={statusClass} />
                )}
                <span className={statusClass}>{toolName}</span>
                <span className="text-sc-fg-subtle truncate">{call.content.slice(0, 40)}</span>
              </div>
            );
          })}
        </div>
      )}

      {/* Expanded content with animation */}
      <div
        ref={contentRef}
        className={`overflow-hidden transition-all duration-300 ease-in-out ${
          isExpanded ? 'max-h-[600px] opacity-100' : 'max-h-0 opacity-0'
        }`}
      >
        <div className="border-t border-sc-fg-subtle/10 bg-sc-bg-base/50 p-2 space-y-1 max-h-[500px] overflow-y-auto">
          {/* Show all nested tool calls */}
          {nestedCalls.length > 0 ? (
            nestedCalls.map(call => {
              const pairedResult = resultsByToolId.get(call.metadata?.tool_id as string);
              return <ToolMessage key={call.id} message={call} result={pairedResult} />;
            })
          ) : (
            <div className="text-xs text-sc-fg-subtle italic py-4 text-center flex flex-col items-center gap-2">
              <div className="w-8 h-8 rounded-full bg-sc-fg-subtle/10 flex items-center justify-center">
                <Code width={16} height={16} className="text-sc-fg-subtle/50" />
              </div>
              No tool calls recorded
            </div>
          )}

          {/* Show final result summary if present */}
          {taskResult && (
            <div
              className={`mt-2 p-3 rounded-lg text-xs animate-fade-in ${
                isError
                  ? 'bg-sc-red/10 border border-sc-red/20 text-sc-red'
                  : 'bg-sc-green/10 border border-sc-green/20 text-sc-green'
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="font-medium flex items-center gap-1.5">
                  {isError ? (
                    <>
                      <Xmark width={12} height={12} /> Failed
                    </>
                  ) : (
                    <>
                      <Check width={12} height={12} /> Completed
                    </>
                  )}
                </span>
                <div className="flex items-center gap-2 text-[10px] text-sc-fg-subtle">
                  {durationMs && <span>{formatDuration(durationMs)}</span>}
                  {costUsd && costUsd > 0 && <span>${costUsd.toFixed(4)}</span>}
                </div>
              </div>
              {taskResult.content && taskResult.content.length < 300 && (
                <p className="text-sc-fg-muted mt-2 text-[11px] leading-relaxed">
                  {taskResult.content}
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// Parallel subagents block - when Claude spawns multiple agents at once
interface ParallelAgentsBlockProps {
  subagents: SubagentData[];
  resultsByToolId: Map<string, ChatMessage>;
}

function ParallelAgentsBlock({ subagents, resultsByToolId }: ParallelAgentsBlockProps) {
  const workingCount = subagents.filter(s => !s.taskResult).length;
  const completedCount = subagents.length - workingCount;
  const allDone = workingCount === 0;

  return (
    <div
      className={`rounded-lg border overflow-hidden animate-slide-up transition-all duration-300 ${
        allDone
          ? 'border-sc-green/20 bg-gradient-to-r from-sc-green/5 to-sc-cyan/5'
          : 'border-sc-cyan/30 bg-gradient-to-r from-sc-cyan/5 via-sc-purple/5 to-sc-cyan/5'
      }`}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-sc-cyan/10">
        {!allDone ? (
          <Spinner size="sm" className="text-sc-cyan shrink-0" />
        ) : (
          <div className="animate-bounce-in">
            <Check width={14} height={14} className="text-sc-green shrink-0" />
          </div>
        )}
        <span className={`text-xs font-medium ${allDone ? 'text-sc-green' : 'text-sc-cyan'}`}>
          Parallel Agents ({subagents.length})
        </span>
        <span className="text-[10px] text-sc-fg-subtle flex-1">
          {allDone ? (
            <span className="text-sc-green animate-fade-in">All complete</span>
          ) : (
            <>
              <span className="text-sc-yellow animate-pulse">{workingCount} working</span>
              {completedCount > 0 && (
                <span className="text-sc-green ml-2 animate-fade-in">{completedCount} done</span>
              )}
            </>
          )}
        </span>
      </div>

      {/* Individual agents */}
      <div className="p-2 space-y-1.5">
        {subagents.map(subagent => (
          <SubagentBlock
            key={subagent.taskCall.id}
            taskCall={subagent.taskCall}
            taskResult={subagent.taskResult}
            nestedCalls={subagent.nestedCalls}
            resultsByToolId={resultsByToolId}
          />
        ))}
      </div>
    </div>
  );
}

interface ChatMessageComponentProps {
  message: ChatMessage;
  pairedResult?: ChatMessage; // For tool_call messages, the matching result
  isNew?: boolean;
}

function ChatMessageComponent({ message, pairedResult, isNew = false }: ChatMessageComponentProps) {
  const isAgent = message.role === 'agent';
  const isSystem = message.role === 'system';
  const isToolCall = message.type === 'tool_call';

  // Tool calls render with their paired result
  if (isToolCall) {
    return <ToolMessage message={message} result={pairedResult} isNew={isNew} />;
  }

  // System messages - compact with subtle animation
  if (isSystem) {
    return (
      <div className={`text-center py-1 ${isNew ? 'animate-fade-in' : ''}`}>
        <span className="text-xs text-sc-fg-subtle">{message.content}</span>
      </div>
    );
  }

  // Agent text messages - render as beautiful markdown with entrance animation
  if (isAgent) {
    return (
      <div
        className={`rounded-lg bg-gradient-to-br from-sc-purple/5 via-sc-bg-elevated to-sc-cyan/5 border border-sc-purple/20 p-4 my-2 shadow-lg shadow-sc-purple/5 ${isNew ? 'animate-slide-up' : ''}`}
      >
        <Markdown content={message.content} className="text-sm" />
        <p className="text-[10px] text-sc-fg-subtle mt-3 tabular-nums border-t border-sc-fg-subtle/10 pt-2">
          {message.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </p>
      </div>
    );
  }

  // User text messages - simple bubble with personality
  return (
    <div className={`flex gap-2 flex-row-reverse ${isNew ? 'animate-slide-up' : ''}`}>
      <div className="max-w-[85%] rounded-lg px-3 py-2 bg-gradient-to-br from-sc-cyan/15 to-sc-cyan/5 border border-sc-cyan/30 shadow-lg shadow-sc-cyan/10">
        <p className="text-sm text-sc-fg-primary whitespace-pre-wrap leading-relaxed">
          {message.content}
        </p>
        <p className="text-[10px] text-sc-fg-muted mt-1 tabular-nums">
          {message.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </p>
      </div>
    </div>
  );
}

// Group messages to collapse subagent work using parent_tool_use_id
function groupMessages(
  messages: ChatMessage[],
  resultsByToolId: Map<string, ChatMessage>
): MessageGroup[] {
  // First pass: identify all Task tool calls and collect their nested messages
  const taskToolIds = new Set<string>();
  const nestedByParent = new Map<string, ChatMessage[]>();
  const taskCalls: ChatMessage[] = [];
  const backgroundTaskIds = new Set<string>(); // Tasks with run_in_background: true
  const pollingByTaskId = new Map<string, ChatMessage[]>(); // TaskOutput calls per task

  for (const msg of messages) {
    // Track Task tool calls
    if (msg.type === 'tool_call' && msg.metadata?.tool_name === 'Task') {
      const toolId = msg.metadata?.tool_id as string;
      if (toolId) {
        taskToolIds.add(toolId);
        nestedByParent.set(toolId, []);
        taskCalls.push(msg);

        // Track background tasks
        if (msg.metadata?.run_in_background) {
          backgroundTaskIds.add(toolId);
          pollingByTaskId.set(toolId, []);
        }
      }
    }

    // Track TaskOutput calls (polling for background agents)
    if (msg.type === 'tool_call' && msg.metadata?.tool_name === 'TaskOutput') {
      const taskId = msg.metadata?.task_id as string | undefined;
      if (taskId && backgroundTaskIds.has(taskId)) {
        const polling = pollingByTaskId.get(taskId);
        if (polling) {
          polling.push(msg);
        }
      }
    }

    // Group messages by parent_tool_use_id
    const parentId = msg.metadata?.parent_tool_use_id as string | undefined;
    if (parentId && taskToolIds.has(parentId)) {
      const nested = nestedByParent.get(parentId);
      if (nested && msg.type !== 'tool_result') {
        nested.push(msg);
      }
    }
  }

  // Detect parallel agents (Task calls within 2 seconds of each other)
  const PARALLEL_THRESHOLD_MS = 2000;
  const parallelGroups: ChatMessage[][] = [];
  const processedTaskIds = new Set<string>();

  for (let i = 0; i < taskCalls.length; i++) {
    const task = taskCalls[i];
    const taskId = task.metadata?.tool_id as string;
    if (processedTaskIds.has(taskId)) continue;

    // Find all tasks within the time window
    const parallelTasks = [task];
    processedTaskIds.add(taskId);

    for (let j = i + 1; j < taskCalls.length; j++) {
      const otherTask = taskCalls[j];
      const otherId = otherTask.metadata?.tool_id as string;
      if (processedTaskIds.has(otherId)) continue;

      const timeDiff = Math.abs(task.timestamp.getTime() - otherTask.timestamp.getTime());
      if (timeDiff <= PARALLEL_THRESHOLD_MS) {
        parallelTasks.push(otherTask);
        processedTaskIds.add(otherId);
      }
    }

    parallelGroups.push(parallelTasks);
  }

  // Build map of task ID to its parallel group
  const taskToParallelGroup = new Map<string, ChatMessage[]>();
  for (const group of parallelGroups) {
    for (const task of group) {
      taskToParallelGroup.set(task.metadata?.tool_id as string, group);
    }
  }

  // Track which parallel groups we've already rendered
  const renderedParallelGroups = new Set<ChatMessage[]>();

  // Second pass: build groups, skipping messages that belong to subagents
  const groups: MessageGroup[] = [];

  for (const msg of messages) {
    // Skip tool_results (they're paired with their calls)
    if (msg.type === 'tool_result') {
      continue;
    }

    // Skip messages that belong to a subagent (they're rendered inside SubagentBlock)
    const parentId = msg.metadata?.parent_tool_use_id as string | undefined;
    if (parentId && taskToolIds.has(parentId)) {
      continue;
    }

    // Check if this is a Task tool call (subagent spawn)
    if (msg.type === 'tool_call' && msg.metadata?.tool_name === 'Task') {
      const taskToolId = msg.metadata?.tool_id as string | undefined;
      if (!taskToolId) continue;

      const parallelGroup = taskToParallelGroup.get(taskToolId);

      // If this is part of a parallel group with multiple agents
      if (parallelGroup && parallelGroup.length > 1) {
        // Only render once per parallel group
        if (renderedParallelGroups.has(parallelGroup)) continue;
        renderedParallelGroups.add(parallelGroup);

        const subagents: SubagentData[] = parallelGroup.map(task => {
          const id = task.metadata?.tool_id as string;
          const polling = pollingByTaskId.get(id) ?? [];
          const lastPollResult =
            polling.length > 0
              ? resultsByToolId.get(polling[polling.length - 1].metadata?.tool_id as string)
              : undefined;
          const lastPollStatus = lastPollResult?.metadata?.status as
            | 'running'
            | 'completed'
            | 'failed'
            | undefined;
          return {
            taskCall: task,
            taskResult: resultsByToolId.get(id),
            nestedCalls: nestedByParent.get(id) ?? [],
            pollingCalls: polling,
            lastPollStatus,
          };
        });

        groups.push({
          kind: 'parallel_subagents',
          subagents,
          resultsByToolId,
        });
      } else {
        // Single subagent
        const taskResult = resultsByToolId.get(taskToolId);
        const nestedCalls = nestedByParent.get(taskToolId) ?? [];
        const polling = pollingByTaskId.get(taskToolId) ?? [];

        groups.push({
          kind: 'subagent',
          taskCall: msg,
          taskResult,
          nestedCalls,
          resultsByToolId,
          pollingCalls: polling,
        });
      }
    } else {
      // Regular message
      const pairedResult =
        msg.type === 'tool_call' ? resultsByToolId.get(msg.metadata?.tool_id as string) : undefined;
      groups.push({ kind: 'message', message: msg, pairedResult });
    }
  }

  return groups;
}

function ChatPanel({
  messages,
  onSendMessage,
  isAgentWorking,
  agentName,
  agentStatus,
}: {
  messages: ChatMessage[];
  onSendMessage: (content: string) => void;
  isAgentWorking: boolean;
  agentName: string;
  agentStatus: string;
}) {
  const [inputValue, setInputValue] = useState('');
  const [isFocused, setIsFocused] = useState(false);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const prevMessageCount = useRef(messages.length);
  const isAtBottomRef = useRef(true); // Track if user is scrolled to bottom

  // Track new messages for entrance animations
  const newMessageIds = useRef(new Set<string>());
  if (messages.length > prevMessageCount.current) {
    const newMessages = messages.slice(prevMessageCount.current);
    for (const msg of newMessages) {
      newMessageIds.current.add(msg.id);
    }
    // Clear after animation
    setTimeout(() => {
      newMessageIds.current.clear();
    }, 500);
  }
  prevMessageCount.current = messages.length;

  // Track scroll position to determine if user is at bottom
  const handleScroll = useCallback(() => {
    const container = messagesContainerRef.current;
    if (!container) return;

    // Consider "at bottom" if within 50px of the bottom (accounts for rounding)
    const threshold = 50;
    const distanceFromBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight;
    isAtBottomRef.current = distanceFromBottom <= threshold;
  }, []);

  // Auto-scroll to bottom ONLY if user was already at bottom
  // biome-ignore lint/correctness/useExhaustiveDependencies: intentional trigger on message count change
  useEffect(() => {
    const container = messagesContainerRef.current;
    if (container && isAtBottomRef.current) {
      container.scrollTop = container.scrollHeight;
    }
  }, [messages.length]);

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      if (!inputValue.trim()) return;
      onSendMessage(inputValue.trim());
      setInputValue('');
    },
    [inputValue, onSendMessage]
  );

  // Build a map of tool_id -> result message for pairing
  const resultsByToolId = useMemo(() => {
    const map = new Map<string, ChatMessage>();
    for (const msg of messages) {
      if (msg.type === 'tool_result') {
        const toolId = msg.metadata?.tool_id as string | undefined;
        if (toolId) {
          map.set(toolId, msg);
        }
      }
    }
    return map;
  }, [messages]);

  // Group messages to collapse subagent work
  const messageGroups = useMemo(
    () => groupMessages(messages, resultsByToolId),
    [messages, resultsByToolId]
  );

  // Determine if we should show the thinking indicator
  // Show when agent is working and there's no visible activity (pending tool calls have spinners)
  const hasPendingToolCalls = messages.some(
    msg => msg.type === 'tool_call' && !resultsByToolId.has(msg.metadata?.tool_id as string)
  );
  const lastMessage = messages[messages.length - 1];
  const lastMessageAge = lastMessage ? Date.now() - lastMessage.timestamp.getTime() : Infinity;
  // Don't show thinking immediately after agent sends a text response
  const recentAgentText =
    lastMessage?.role === 'agent' && lastMessage?.type === 'text' && lastMessageAge < 1000;
  // Show thinking when: agent is working, no tools have spinners, and not just responded
  const showThinking = isAgentWorking && !hasPendingToolCalls && !recentAgentText;

  // Placeholder text based on agent state
  const getPlaceholder = () => {
    if (isAgentWorking) return 'Agent is working... you can still send messages';
    if (agentStatus === 'paused') return 'Agent is paused. Resume to continue...';
    if (agentStatus === 'completed') return 'Agent has completed. Start a new task...';
    if (agentStatus === 'failed') return 'Agent encountered an error...';
    return 'Send a message to the agent...';
  };

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
      {/* Messages - scrolls independently */}
      <div
        ref={messagesContainerRef}
        onScroll={handleScroll}
        className="flex-1 min-h-0 overflow-y-auto p-3 space-y-1.5"
      >
        {/* Empty state when no messages yet */}
        {messages.length === 0 && !isAgentWorking && <EmptyChatState agentName={agentName} />}

        {messageGroups.map((group, idx) => {
          if (group.kind === 'parallel_subagents') {
            return (
              <ParallelAgentsBlock
                key={`parallel-${group.subagents[0]?.taskCall.id ?? idx}`}
                subagents={group.subagents}
                resultsByToolId={group.resultsByToolId}
              />
            );
          }
          if (group.kind === 'subagent') {
            return (
              <SubagentBlock
                key={group.taskCall.id}
                taskCall={group.taskCall}
                taskResult={group.taskResult}
                nestedCalls={group.nestedCalls}
                resultsByToolId={group.resultsByToolId}
                pollingCalls={group.pollingCalls}
              />
            );
          }
          return (
            <ChatMessageComponent
              key={group.message.id}
              message={group.message}
              pairedResult={group.pairedResult}
              isNew={newMessageIds.current.has(group.message.id)}
            />
          );
        })}

        {/* Thinking indicator when agent is working with no recent output */}
        {showThinking && <ThinkingIndicator />}
      </div>

      {/* Input - always visible at bottom with enhanced styling */}
      <form
        onSubmit={handleSubmit}
        className="shrink-0 p-3 border-t border-sc-fg-subtle/20 bg-sc-bg-elevated"
      >
        <div
          className={`flex items-center gap-2 p-1 rounded-xl transition-all duration-300 ${
            isFocused
              ? 'bg-gradient-to-r from-sc-purple/10 via-sc-bg-base to-sc-cyan/10 ring-2 ring-sc-purple/30 shadow-lg shadow-sc-purple/10'
              : 'bg-sc-bg-base'
          }`}
        >
          <input
            type="text"
            value={inputValue}
            onChange={e => setInputValue(e.target.value)}
            onFocus={() => setIsFocused(true)}
            onBlur={() => setIsFocused(false)}
            placeholder={getPlaceholder()}
            className="flex-1 px-3 py-2 bg-transparent border-none text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:outline-none"
          />
          <button
            type="submit"
            disabled={!inputValue.trim()}
            className={`p-2.5 rounded-lg transition-all duration-200 ${
              inputValue.trim()
                ? 'bg-gradient-to-r from-sc-purple to-sc-purple/80 hover:from-sc-purple/90 hover:to-sc-purple/70 text-white shadow-lg shadow-sc-purple/30 hover:scale-105 active:scale-95'
                : 'bg-sc-fg-subtle/20 text-sc-fg-muted cursor-not-allowed'
            }`}
          >
            <Send width={16} height={16} />
          </button>
        </div>
      </form>
    </div>
  );
}

// =============================================================================
// Main Component
// =============================================================================

export function AgentChatPanel({ agent }: { agent: Agent }) {
  // Subscribe to real-time updates via WebSocket
  useAgentSubscription(agent.id);

  // Fetch messages from API (WebSocket will invalidate on updates)
  const { data: messagesData } = useAgentMessages(agent.id);
  const sendMessage = useSendAgentMessage();

  // Check if agent is actively working
  const isAgentWorking = ['initializing', 'working', 'resuming'].includes(agent.status);

  // Convert API messages to component format
  const messages: ChatMessage[] = useMemo(() => {
    if (!messagesData?.messages) return [];
    return messagesData.messages.map((msg: ApiMessage) => ({
      id: msg.id,
      role: msg.role,
      content: msg.content,
      timestamp: new Date(msg.timestamp),
      type: msg.type,
      metadata: msg.metadata,
    }));
  }, [messagesData?.messages]);

  const handleSendMessage = useCallback(
    (content: string) => {
      sendMessage.mutate({ id: agent.id, content });
    },
    [agent.id, sendMessage]
  );

  return (
    <div className="h-full flex flex-col bg-sc-bg-base rounded-lg border border-sc-fg-subtle/20 overflow-hidden shadow-xl shadow-sc-purple/5">
      <AgentHeader agent={agent} />
      <ChatPanel
        messages={messages}
        onSendMessage={handleSendMessage}
        isAgentWorking={isAgentWorking}
        agentName={agent.name}
        agentStatus={agent.status}
      />
    </div>
  );
}
