'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { IconComponent } from '@/components/ui/icons';
import {
  Check,
  ChevronDown,
  ChevronUp,
  Code,
  EditPencil,
  FileText,
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
import type { Agent, AgentMessage as ApiMessage, FileChange } from '@/lib/api';
import {
  AGENT_STATUS_CONFIG,
  AGENT_TYPE_CONFIG,
  type AgentStatusType,
  type AgentTypeValue,
  formatDistanceToNow,
} from '@/lib/constants';
import {
  useAgentMessages,
  useAgentSubscription,
  useAgentWorkspace,
  usePauseAgent,
  useResumeAgent,
  useSendAgentMessage,
  useTerminateAgent,
} from '@/lib/hooks';

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

interface ChatMessage {
  id: string;
  role: 'agent' | 'user' | 'system';
  content: string;
  timestamp: Date;
  type?: 'text' | 'tool_call' | 'tool_result' | 'error';
  metadata?: Record<string, unknown>;
}

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
    <div className="flex items-center justify-between p-3 border-b border-sc-fg-subtle/20 bg-sc-bg-elevated">
      <div className="flex items-center gap-3">
        <span className="text-lg" style={{ color: typeConfig.color }}>
          {typeConfig.icon}
        </span>
        <div>
          <h2 className="text-sm font-medium text-sc-fg-primary">{agent.name}</h2>
          <div className="flex items-center gap-2 mt-0.5">
            <span
              className={`text-xs px-1.5 py-0.5 rounded ${statusConfig.bgClass} ${statusConfig.textClass}`}
            >
              {statusConfig.icon} {statusConfig.label}
            </span>
            {agent.last_heartbeat && (
              <span className="text-xs text-sc-fg-muted">
                {formatDistanceToNow(agent.last_heartbeat)}
              </span>
            )}
          </div>
        </div>
      </div>

      <div className="flex items-center gap-2">
        {isActive && (
          <button
            type="button"
            onClick={() => pauseAgent.mutate({ id: agent.id })}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-sc-yellow bg-sc-yellow/10 hover:bg-sc-yellow/20 rounded-lg transition-colors"
            disabled={pauseAgent.isPending}
          >
            <Pause width={14} height={14} />
            Pause
          </button>
        )}
        {isPaused && (
          <button
            type="button"
            onClick={() => resumeAgent.mutate(agent.id)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-sc-green bg-sc-green/10 hover:bg-sc-green/20 rounded-lg transition-colors"
            disabled={resumeAgent.isPending}
          >
            <Play width={14} height={14} />
            Resume
          </button>
        )}
        {!isTerminal && (
          <button
            type="button"
            onClick={() => terminateAgent.mutate({ id: agent.id })}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-sc-red bg-sc-red/10 hover:bg-sc-red/20 rounded-lg transition-colors"
            disabled={terminateAgent.isPending}
          >
            <Square width={14} height={14} />
            Stop
          </button>
        )}
      </div>
    </div>
  );
}

function ToolMessage({ message, isToolCall }: { message: ChatMessage; isToolCall: boolean }) {
  const [isExpanded, setIsExpanded] = useState(false);

  const iconName = message.metadata?.icon as string | undefined;
  const toolName = message.metadata?.tool_name as string | undefined;
  const isError = message.metadata?.is_error as boolean | undefined;
  const Icon = iconName ? TOOL_ICONS[iconName] : Code;

  // Get first line as preview, rest as expandable content
  const lines = message.content.split('\n');
  const preview = lines[0]?.slice(0, 100) || '';
  const hasMore = message.content.length > 100 || lines.length > 1;

  const borderClass = isToolCall
    ? 'border-sc-fg-subtle/20'
    : isError
      ? 'border-sc-red/20'
      : 'border-sc-green/20';

  const bgClass = isToolCall ? 'bg-sc-bg-elevated' : isError ? 'bg-sc-red/10' : 'bg-sc-green/10';

  return (
    <div
      className={`rounded-lg border ${borderClass} ${bgClass} font-mono text-xs overflow-hidden`}
    >
      {/* Clickable header - always visible */}
      <button
        type="button"
        onClick={() => hasMore && setIsExpanded(!isExpanded)}
        className={`w-full flex items-center gap-2 p-2 text-left ${hasMore ? 'hover:bg-sc-fg-subtle/5 cursor-pointer' : ''}`}
      >
        <Icon
          width={14}
          height={14}
          className={isToolCall ? 'text-sc-purple' : isError ? 'text-sc-red' : 'text-sc-green'}
        />
        <span
          className={
            isToolCall ? 'text-sc-purple font-medium' : isError ? 'text-sc-red' : 'text-sc-green'
          }
        >
          {isToolCall ? toolName || 'Tool' : isError ? 'Error' : 'Success'}
        </span>
        <span className="text-sc-fg-muted truncate flex-1">{preview}</span>
        {hasMore && (
          <span className="text-sc-fg-subtle shrink-0">
            {isExpanded ? (
              <ChevronUp width={14} height={14} />
            ) : (
              <ChevronDown width={14} height={14} />
            )}
          </span>
        )}
      </button>

      {/* Expandable content */}
      {isExpanded && (
        <div className="border-t border-sc-fg-subtle/10 p-2 max-h-64 overflow-y-auto">
          <pre className="text-sc-fg-primary whitespace-pre-wrap break-words">
            {message.content}
          </pre>
        </div>
      )}

      {/* Timestamp */}
      <div className="px-2 pb-1.5 text-[10px] text-sc-fg-muted">
        {message.timestamp.toLocaleTimeString()}
      </div>
    </div>
  );
}

function ChatMessage({ message }: { message: ChatMessage }) {
  const isAgent = message.role === 'agent';
  const isSystem = message.role === 'system';
  const isToolCall = message.type === 'tool_call';
  const isToolResult = message.type === 'tool_result';

  // Use collapsible component for tool messages
  if (isToolCall || isToolResult) {
    return (
      <div className="max-w-[90%]">
        <ToolMessage message={message} isToolCall={isToolCall} />
      </div>
    );
  }

  // Regular text messages
  return (
    <div className={`flex gap-3 ${isAgent ? '' : 'flex-row-reverse'}`}>
      <div
        className={`
          max-w-[85%] rounded-lg p-3
          ${isSystem ? 'bg-sc-bg-highlight text-sc-fg-muted text-center w-full' : ''}
          ${isAgent ? 'bg-sc-purple/10 border border-sc-purple/20' : ''}
          ${message.role === 'user' ? 'bg-sc-cyan/10 border border-sc-cyan/20' : ''}
        `}
      >
        <p className="text-sm text-sc-fg-primary whitespace-pre-wrap">{message.content}</p>
        <p className="text-xs text-sc-fg-muted mt-1.5">{message.timestamp.toLocaleTimeString()}</p>
      </div>
    </div>
  );
}

function ChatPanel({
  messages,
  onSendMessage,
}: {
  messages: ChatMessage[];
  onSendMessage: (content: string) => void;
}) {
  const [inputValue, setInputValue] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when new messages arrive
  // biome-ignore lint/correctness/useExhaustiveDependencies: intentional trigger on message count change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
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

  return (
    <div className="flex flex-col h-full">
      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.map(msg => (
          <ChatMessage key={msg.id} message={msg} />
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <form
        onSubmit={handleSubmit}
        className="p-3 border-t border-sc-fg-subtle/20 bg-sc-bg-elevated"
      >
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={inputValue}
            onChange={e => setInputValue(e.target.value)}
            placeholder="Send a message to the agent..."
            className="flex-1 px-3 py-2 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10"
          />
          <button
            type="submit"
            disabled={!inputValue.trim()}
            className="p-2 bg-sc-purple hover:bg-sc-purple/80 disabled:bg-sc-fg-subtle/20 disabled:text-sc-fg-muted text-white rounded-lg transition-colors"
          >
            <Send width={16} height={16} />
          </button>
        </div>
      </form>
    </div>
  );
}

function WorkspacePanel({ files }: { files: FileChange[] }) {
  const [activeTab, setActiveTab] = useState<'files' | 'terminal'>('files');
  const [selectedFile, setSelectedFile] = useState<string | null>(null);

  return (
    <div className="flex flex-col h-full">
      {/* Tabs */}
      <div className="flex items-center gap-1 p-2 border-b border-sc-fg-subtle/20 bg-sc-bg-elevated">
        <button
          type="button"
          onClick={() => setActiveTab('files')}
          className={`
            flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg transition-colors
            ${activeTab === 'files' ? 'bg-sc-purple/20 text-sc-purple' : 'text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight'}
          `}
        >
          <FileText width={14} height={14} />
          Files ({files.length})
        </button>
        <button
          type="button"
          onClick={() => setActiveTab('terminal')}
          className={`
            flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg transition-colors
            ${activeTab === 'terminal' ? 'bg-sc-purple/20 text-sc-purple' : 'text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight'}
          `}
        >
          <Code width={14} height={14} />
          Terminal
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden">
        {activeTab === 'files' ? (
          <div className="flex h-full">
            {/* File list */}
            <div className="w-48 border-r border-sc-fg-subtle/20 overflow-y-auto">
              {files.map(file => (
                <button
                  key={file.path}
                  type="button"
                  onClick={() => setSelectedFile(file.path)}
                  className={`
                    w-full text-left px-3 py-2 text-xs font-mono flex items-center gap-2 hover:bg-sc-bg-highlight transition-colors
                    ${selectedFile === file.path ? 'bg-sc-purple/10 text-sc-purple' : 'text-sc-fg-muted'}
                  `}
                >
                  <span
                    className={`
                      w-1.5 h-1.5 rounded-full shrink-0
                      ${file.status === 'added' ? 'bg-sc-green' : ''}
                      ${file.status === 'modified' ? 'bg-sc-yellow' : ''}
                      ${file.status === 'deleted' ? 'bg-sc-red' : ''}
                    `}
                  />
                  <span className="truncate">{file.path.split('/').pop()}</span>
                </button>
              ))}
            </div>

            {/* Diff view */}
            <div className="flex-1 overflow-auto p-4">
              {selectedFile ? (
                <div className="font-mono text-xs">
                  <div className="text-sc-fg-muted mb-2">{selectedFile}</div>
                  <div className="bg-sc-bg-base rounded-lg p-4 border border-sc-fg-subtle/20">
                    <p className="text-sc-fg-muted italic">
                      Diff view coming soon. Connect to agent workspace API for real-time file
                      changes.
                    </p>
                  </div>
                </div>
              ) : (
                <div className="flex items-center justify-center h-full text-sc-fg-muted text-sm">
                  Select a file to view changes
                </div>
              )}
            </div>
          </div>
        ) : (
          <div className="h-full p-4 font-mono text-xs bg-sc-bg-base text-sc-fg-muted">
            <p className="text-sc-fg-subtle italic">
              Terminal output will stream here. Connect to agent workspace API for real-time
              terminal output.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

// =============================================================================
// Main Component
// =============================================================================

export function AgentChatPanel({ agent }: { agent: Agent }) {
  const [dividerPosition, setDividerPosition] = useState(50); // Percentage
  const dividerRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Subscribe to real-time updates via WebSocket
  useAgentSubscription(agent.id);

  // Fetch messages and workspace from API (WebSocket will invalidate these on updates)
  const { data: messagesData } = useAgentMessages(agent.id);
  const { data: workspaceData } = useAgentWorkspace(agent.id);
  const sendMessage = useSendAgentMessage();

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

  // Get workspace files
  const files: FileChange[] = useMemo(() => {
    return workspaceData?.files ?? [];
  }, [workspaceData?.files]);

  const handleSendMessage = useCallback(
    (content: string) => {
      sendMessage.mutate({ id: agent.id, content });
    },
    [agent.id, sendMessage]
  );

  // Handle divider drag
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const container = containerRef.current;
    if (!container) return;

    const handleMouseMove = (moveEvent: MouseEvent) => {
      const rect = container.getBoundingClientRect();
      const newPosition = ((moveEvent.clientX - rect.left) / rect.width) * 100;
      setDividerPosition(Math.max(30, Math.min(70, newPosition)));
    };

    const handleMouseUp = () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  }, []);

  return (
    <div className="flex-1 flex flex-col bg-sc-bg-base rounded-lg border border-sc-fg-subtle/20 overflow-hidden">
      <AgentHeader agent={agent} />

      <div ref={containerRef} className="flex-1 flex overflow-hidden">
        {/* Chat Panel */}
        <div
          style={{ width: `${dividerPosition}%` }}
          className="flex-shrink-0 border-r border-sc-fg-subtle/20"
        >
          <ChatPanel messages={messages} onSendMessage={handleSendMessage} />
        </div>

        {/* Divider */}
        <div
          ref={dividerRef}
          onMouseDown={handleMouseDown}
          className="w-1 bg-sc-fg-subtle/20 hover:bg-sc-purple/50 cursor-col-resize transition-colors"
        />

        {/* Workspace Panel */}
        <div style={{ width: `${100 - dividerPosition}%` }} className="flex-shrink-0">
          <WorkspacePanel files={files} />
        </div>
      </div>
    </div>
  );
}
