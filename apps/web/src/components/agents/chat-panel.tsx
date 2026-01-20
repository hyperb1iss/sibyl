'use client';

/**
 * Main chat panel component with messages and input.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { buildResultsMap, groupMessages } from './chat-grouping';
import { type Attachment, ChatInput } from './chat-input';
import { ChatMessageComponent } from './chat-messages';
import { EmptyChatState, ThinkingIndicator } from './chat-states';
import { ParallelAgentsBlock, SubagentBlock } from './chat-subagent';
import type { ChatPanelProps } from './chat-types';
import { isToolCallMessage } from './chat-types';

// =============================================================================
// ChatPanel
// =============================================================================

/** Main chat panel with messages list and input form */
/** Grace period before showing thinking indicator after agent text (ms) */
const THINKING_GRACE_PERIOD = 1200;

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
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const prevMessageCount = useRef(messages.length);
  const isAtBottomRef = useRef(true); // Track if user is scrolled to bottom

  // Track when grace period passes to show thinking indicator
  const [thinkingReady, setThinkingReady] = useState(false);
  const lastMessageIdRef = useRef<string | null>(null);

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

  // Track if user has manually scrolled up (indicating they want to read history)
  const userScrolledUp = useRef(false);
  const lastScrollTop = useRef(0);

  // Track scroll position to determine if user scrolled up intentionally
  const handleScroll = useCallback(() => {
    const container = messagesContainerRef.current;
    if (!container) return;

    const currentScrollTop = container.scrollTop;
    const distanceFromBottom = container.scrollHeight - currentScrollTop - container.clientHeight;

    // User is at bottom (within 100px threshold - more generous)
    const atBottom = distanceFromBottom <= 100;

    // Detect intentional scroll up (user scrolled up by > 50px)
    if (currentScrollTop < lastScrollTop.current - 50 && !atBottom) {
      userScrolledUp.current = true;
    }

    // Reset if user scrolls back to bottom
    if (atBottom) {
      userScrolledUp.current = false;
    }

    lastScrollTop.current = currentScrollTop;
    isAtBottomRef.current = atBottom;
  }, []);

  // Auto-scroll to bottom unless user has scrolled up to read history
  // biome-ignore lint/correctness/useExhaustiveDependencies: intentional trigger on message count change
  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;

    // Always scroll if user hasn't scrolled up, or if they're near the bottom
    if (!userScrolledUp.current || isAtBottomRef.current) {
      // Use smooth scroll for better UX, but fall back to instant if too far
      const distanceFromBottom =
        container.scrollHeight - container.scrollTop - container.clientHeight;

      if (distanceFromBottom < 500) {
        container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' });
      } else {
        container.scrollTop = container.scrollHeight;
      }
      userScrolledUp.current = false;
    }
  }, [messages.length, pendingMessages.length]);

  // Handle send from ChatInput
  const handleSend = useCallback(
    (message: string, _attachments: Attachment[]) => {
      onSendMessage(message);
    },
    [onSendMessage]
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
    msg => isToolCallMessage(msg) && !resultsByToolId.has(msg.tool.id)
  );
  const lastMessage = messages[messages.length - 1];
  const isLastMessageAgentText = lastMessage?.role === 'agent' && lastMessage?.kind === 'text';

  // Timer-based grace period for showing thinking indicator
  // Resets when last message changes, then fires after THINKING_GRACE_PERIOD
  useEffect(() => {
    const lastId = lastMessage?.id ?? null;

    // If message changed, reset thinking ready state
    if (lastId !== lastMessageIdRef.current) {
      lastMessageIdRef.current = lastId;
      setThinkingReady(false);
    }

    // Only start timer if agent is working and last message is agent text
    if (!isAgentWorking || !isLastMessageAgentText) {
      return;
    }

    const timer = setTimeout(() => {
      setThinkingReady(true);
    }, THINKING_GRACE_PERIOD);

    return () => clearTimeout(timer);
  }, [lastMessage?.id, isAgentWorking, isLastMessageAgentText]);

  // Show thinking when: agent is working, no tools have spinners, and grace period passed
  const showThinking =
    isAgentWorking && !hasPendingToolCalls && (thinkingReady || !isLastMessageAgentText);

  // Placeholder text - keep it simple, user doesn't care about internal state
  const placeholder = isAgentWorking ? 'Agent is working...' : 'Send a message...';

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

      {/* Input with paste/attachment support */}
      <ChatInput onSend={handleSend} placeholder={placeholder} />
    </div>
  );
}
