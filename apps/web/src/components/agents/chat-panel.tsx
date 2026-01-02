'use client';

/**
 * Main chat panel component with messages and input.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Send } from '@/components/ui/icons';
import { buildResultsMap, groupMessages } from './chat-grouping';
import { ChatMessageComponent } from './chat-messages';
import { EmptyChatState, ThinkingIndicator } from './chat-states';
import { ParallelAgentsBlock, SubagentBlock } from './chat-subagent';
import type { ChatPanelProps } from './chat-types';

// =============================================================================
// ChatPanel
// =============================================================================

/** Main chat panel with messages list and input form */
export function ChatPanel({
  messages,
  pendingMessages,
  onSendMessage,
  onCancelPending,
  onEditPending,
  isAgentWorking,
  agentName,
  agentStatus,
  statusHints,
}: ChatPanelProps) {
  const [inputValue, setInputValue] = useState('');
  const [isFocused, setIsFocused] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const prevMessageCount = useRef(messages.length);
  const isAtBottomRef = useRef(true); // Track if user is scrolled to bottom

  // Check if agent is in a terminal state (spinners should stop)
  const isAgentTerminal = ['completed', 'failed', 'terminated'].includes(agentStatus);

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
  }, [messages.length, pendingMessages.length]);

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
  const resultsByToolId = useMemo(() => buildResultsMap(messages), [messages]);

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
    if (agentStatus === 'paused') return 'Agent is paused. Click ▶ to resume...';
    if (agentStatus === 'completed') return 'Session ended. Send a message to continue...';
    if (agentStatus === 'failed') return 'Agent failed. Send a message to retry...';
    if (agentStatus === 'terminated') return 'Session stopped. Send a message to continue...';
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
                isAgentTerminal={isAgentTerminal}
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
                isAgentTerminal={isAgentTerminal}
              />
            );
          }
          return (
            <ChatMessageComponent
              key={group.message.id}
              message={group.message}
              pairedResult={group.pairedResult}
              isNew={newMessageIds.current.has(group.message.id)}
              statusHints={statusHints}
            />
          );
        })}

        {/* Pending/queued user messages */}
        {pendingMessages.length > 0 && (
          <div className="space-y-1.5">
            {pendingMessages.map(msg => (
              <div key={msg.id} className="flex justify-end animate-slide-up">
                <div className="max-w-[85%] flex flex-col items-end gap-1">
                  {editingId === msg.id ? (
                    /* Edit mode - biome-ignore lint/a11y/noAutofocus: intentional focus */
                    <div className="flex items-center gap-1.5 w-full">
                      <input
                        type="text"
                        value={editValue}
                        onChange={e => setEditValue(e.target.value)}
                        onKeyDown={e => {
                          if (e.key === 'Enter' && editValue.trim()) {
                            onEditPending(msg.id, editValue.trim());
                            setEditingId(null);
                          } else if (e.key === 'Escape') {
                            setEditingId(null);
                          }
                        }}
                        // biome-ignore lint/a11y/noAutofocus: need focus on edit activation
                        autoFocus
                        className="flex-1 px-2 py-1 text-sm bg-sc-bg-base border border-sc-purple/50 rounded-lg text-sc-fg-primary focus:outline-none focus-visible:outline-none"
                      />
                      <button
                        type="button"
                        onClick={() => {
                          if (editValue.trim()) {
                            onEditPending(msg.id, editValue.trim());
                          }
                          setEditingId(null);
                        }}
                        className="p-1 text-sc-green hover:bg-sc-green/10 rounded"
                        title="Save"
                      >
                        ✓
                      </button>
                      <button
                        type="button"
                        onClick={() => setEditingId(null)}
                        className="p-1 text-sc-fg-muted hover:bg-sc-fg-subtle/10 rounded"
                        title="Cancel"
                      >
                        ✕
                      </button>
                    </div>
                  ) : (
                    /* Display mode */
                    <div className="group relative px-3 py-2 rounded-xl bg-sc-purple/20 text-sc-fg-primary text-sm border border-sc-purple/30 border-dashed">
                      {msg.content}
                      {/* Edit/cancel controls */}
                      <div className="absolute -left-14 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-0.5">
                        <button
                          type="button"
                          onClick={() => {
                            setEditingId(msg.id);
                            setEditValue(msg.content);
                          }}
                          className="p-1 text-sc-fg-muted hover:text-sc-cyan hover:bg-sc-cyan/10 rounded text-xs"
                          title="Edit"
                        >
                          ✎
                        </button>
                        <button
                          type="button"
                          onClick={() => onCancelPending(msg.id)}
                          className="p-1 text-sc-fg-muted hover:text-sc-red hover:bg-sc-red/10 rounded text-xs"
                          title="Cancel"
                        >
                          ✕
                        </button>
                      </div>
                    </div>
                  )}
                  <span className="text-[10px] text-sc-yellow flex items-center gap-1 animate-pulse">
                    <span className="w-1.5 h-1.5 rounded-full bg-sc-yellow" />
                    Queued
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}

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
            className="flex-1 px-3 py-2 bg-transparent border-none text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:outline-none focus-visible:outline-none"
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
