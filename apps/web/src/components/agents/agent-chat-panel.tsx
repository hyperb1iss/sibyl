'use client';

/**
 * Agent Chat Panel - Main entry point for agent chat interface.
 *
 * This is a thin orchestrator that composes the chat components.
 * All logic is delegated to specialized modules:
 * - chat-panel.tsx: Main chat container
 * - chat-header.tsx: Agent header with controls
 * - chat-messages.tsx: Message rendering
 * - chat-tool-message.tsx: Tool call display
 * - chat-subagent.tsx: Subagent execution blocks
 * - chat-states.tsx: Loading and empty states
 * - chat-grouping.ts: Message grouping logic
 * - chat-types.ts: Shared types
 * - chat-constants.ts: Constants and helpers
 */

import { useCallback, useMemo } from 'react';
import type { Agent, AgentMessage as ApiMessage } from '@/lib/api';
import {
  useAgentMessages,
  useAgentSubscription,
  useSendAgentMessage,
  useStatusHints,
} from '@/lib/hooks';
import { AgentHeader } from './chat-header';
import { ChatPanel } from './chat-panel';
import type { ChatMessage } from './chat-types';

// =============================================================================
// AgentChatPanel
// =============================================================================

export interface AgentChatPanelProps {
  agent: Agent;
}

/** Main agent chat interface - orchestrates hooks and composes components */
export function AgentChatPanel({ agent }: AgentChatPanelProps) {
  // Subscribe to real-time updates via WebSocket
  useAgentSubscription(agent.id);

  // Fetch messages from API (WebSocket will invalidate on updates)
  const { data: messagesData } = useAgentMessages(agent.id);
  const sendMessage = useSendAgentMessage();

  // Tier 3 status hints from Haiku (via WebSocket)
  const statusHints = useStatusHints(agent.id);

  // Check if agent is actively working
  const isAgentWorking = ['initializing', 'working', 'resuming'].includes(agent.status);

  // Convert API messages to component format
  const messages: ChatMessage[] = useMemo(() => {
    if (!messagesData?.messages) return [];
    return messagesData.messages.map((msg: ApiMessage) => ({
      id: msg.id,
      role: msg.role as ChatMessage['role'],
      content: msg.content,
      timestamp: new Date(msg.timestamp),
      type: msg.type as ChatMessage['type'],
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
        statusHints={statusHints}
      />
    </div>
  );
}
