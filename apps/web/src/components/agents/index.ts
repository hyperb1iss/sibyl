// Barrel exports for agents components

// Main components
export { ActivityFeed } from './activity-feed';
export { AgentChatPanel, type AgentChatPanelProps } from './agent-chat-panel';
export { ApprovalQueue } from './approval-queue';
export {
  ApprovalRequestMessage,
  type ApprovalRequestMessageProps,
} from './approval-request-message';
export { formatDuration, pickRandomPhrase, stripAnsi, THINKING_PHRASES } from './chat-constants';
// Tool registry - single source of truth for tool metadata
export {
  getToolEntry,
  getToolIcon,
  getToolStatus,
  hasCustomRenderer,
  isKnownTool,
  TOOL_REGISTRY,
  TOOLS,
  type ToolName,
} from './tool-registry';
// Chat utilities (for testing or custom implementations)
export { buildResultsMap, groupMessages } from './chat-grouping';
// Chat sub-components (for advanced customization)
export { AgentHeader, type AgentHeaderProps } from './chat-header';
export { type Attachment, ChatInput } from './chat-input';
export { ChatMessageComponent } from './chat-messages';
export { ChatPanel } from './chat-panel';
export { EmptyChatState, type EmptyChatStateProps, ThinkingIndicator } from './chat-states';
export { ParallelAgentsBlock, SubagentBlock } from './chat-subagent';
export { ToolMessage } from './chat-tool-message';
// Chat types (for consumers that need to work with chat messages)
export type {
  ChatMessage,
  ChatPanelProps,
  MessageGroup,
  PendingMessage,
  SubagentData,
  TextMessage,
  ToolCallMessage,
  ToolResultMessage,
} from './chat-types';
// Type guards and transformers
export {
  createPendingMessage,
  isTaskToolCall,
  isTextMessage,
  isToolCallMessage,
  isToolResultMessage,
  transformApiMessage,
  transformApiMessages,
} from './chat-types';
export { HealthMonitor } from './health-monitor';
export { SibylContextMessage } from './sibyl-context-message';
export { SpawnAgentDialog } from './spawn-agent-dialog';
// Tool renderers
export {
  BashToolRenderer,
  EditToolRenderer,
  GlobToolRenderer,
  GrepToolRenderer,
  ReadToolRenderer,
  ToolContentRenderer,
  WriteToolRenderer,
} from './tool-renderers';
export {
  type Question,
  type QuestionOption,
  UserQuestionMessage,
  type UserQuestionMessageProps,
} from './user-question-message';
// Hooks
export {
  type ExpandedSize,
  getExpandedClasses,
  useExpanded,
  type UseExpandedOptions,
  type UseExpandedReturn,
} from './use-expanded';
