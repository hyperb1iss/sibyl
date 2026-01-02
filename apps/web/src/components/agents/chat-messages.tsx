'use client';

/**
 * Chat message rendering component.
 */

import { Markdown } from '@/components/ui/markdown';
import { ToolMessage } from './chat-tool-message';
import type { ChatMessageComponentProps } from './chat-types';

// =============================================================================
// ChatMessageComponent
// =============================================================================

/** Renders individual chat messages based on role and type */
export function ChatMessageComponent({
  message,
  pairedResult,
  isNew = false,
  statusHints,
}: ChatMessageComponentProps) {
  const isAgent = message.role === 'agent';
  const isSystem = message.role === 'system';
  const isToolCall = message.type === 'tool_call';

  // Tool calls render with their paired result
  if (isToolCall) {
    const toolId = message.metadata?.tool_id as string | undefined;
    const tier3Hint = toolId ? statusHints?.get(toolId) : undefined;
    return (
      <ToolMessage message={message} result={pairedResult} isNew={isNew} tier3Hint={tier3Hint} />
    );
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
