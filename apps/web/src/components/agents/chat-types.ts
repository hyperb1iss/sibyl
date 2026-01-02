/**
 * Shared types for the agent chat system.
 */

// =============================================================================
// Message Types
// =============================================================================

/** Internal representation of chat messages */
export interface ChatMessage {
  id: string;
  role: 'agent' | 'user' | 'system';
  content: string;
  timestamp: Date;
  type?: 'text' | 'tool_call' | 'tool_result' | 'error' | 'sibyl_context';
  metadata?: ChatMessageMetadata;
}

/** Common metadata fields on chat messages */
export interface ChatMessageMetadata {
  icon?: string;
  tool_name?: string;
  tool_id?: string;
  is_error?: boolean;
  parent_tool_use_id?: string;
  input?: Record<string, unknown>;
  full_content?: string;
  subagent_type?: string;
  run_in_background?: boolean;
  task_id?: string;
  status?: 'running' | 'completed' | 'failed';
  duration_ms?: number;
  total_cost_usd?: number;
  [key: string]: unknown;
}

// =============================================================================
// Subagent Types
// =============================================================================

/** Subagent data for rendering nested agent execution */
export interface SubagentData {
  taskCall: ChatMessage;
  taskResult?: ChatMessage;
  nestedCalls: ChatMessage[];
  pollingCalls?: ChatMessage[]; // TaskOutput calls polling this background agent
  lastPollStatus?: 'running' | 'completed' | 'failed';
}

// =============================================================================
// Message Grouping Types
// =============================================================================

/** Grouped message types for rendering - discriminated union */
export type MessageGroup =
  | {
      kind: 'message';
      message: ChatMessage;
      pairedResult?: ChatMessage;
    }
  | {
      kind: 'subagent';
      taskCall: ChatMessage;
      taskResult?: ChatMessage;
      nestedCalls: ChatMessage[];
      resultsByToolId: Map<string, ChatMessage>;
      pollingCalls?: ChatMessage[];
    }
  | {
      kind: 'parallel_subagents';
      subagents: SubagentData[];
      resultsByToolId: Map<string, ChatMessage>;
    };

// =============================================================================
// Component Props Types
// =============================================================================

export interface ToolMessageProps {
  message: ChatMessage;
  result?: ChatMessage;
  isNew?: boolean;
  tier3Hint?: string;
}

export interface SubagentBlockProps {
  taskCall: ChatMessage;
  taskResult?: ChatMessage;
  nestedCalls: ChatMessage[];
  resultsByToolId: Map<string, ChatMessage>;
  pollingCalls?: ChatMessage[];
  /** When true, treat pending tasks as interrupted (parent agent terminated) */
  isAgentTerminal?: boolean;
}

export interface ParallelAgentsBlockProps {
  subagents: SubagentData[];
  resultsByToolId: Map<string, ChatMessage>;
  /** When true, treat pending tasks as interrupted (parent agent terminated) */
  isAgentTerminal?: boolean;
}

export interface ChatMessageComponentProps {
  message: ChatMessage;
  pairedResult?: ChatMessage;
  isNew?: boolean;
  statusHints?: Map<string, string>;
}

export interface ChatPanelProps {
  messages: ChatMessage[];
  pendingMessages: ChatMessage[];
  onSendMessage: (content: string) => void;
  onCancelPending: (id: string) => void;
  onEditPending: (id: string, newContent: string) => void;
  isAgentWorking: boolean;
  agentName: string;
  agentStatus: string;
  statusHints: Map<string, string>;
}
