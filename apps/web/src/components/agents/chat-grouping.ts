/**
 * Pure functions for grouping and organizing chat messages.
 * No React dependencies - fully testable logic.
 */

import type { ChatMessage, MessageGroup, SubagentData } from './chat-types';

// =============================================================================
// Results Map Builder
// =============================================================================

/** Build a map of tool_id -> result message for pairing tool calls with results */
export function buildResultsMap(messages: ChatMessage[]): Map<string, ChatMessage> {
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
}

// =============================================================================
// Message Grouping
// =============================================================================

/** Time window for detecting parallel agent spawns */
const PARALLEL_THRESHOLD_MS = 2000;

/**
 * Group messages to collapse subagent work using parent_tool_use_id.
 *
 * This function:
 * 1. Identifies Task tool calls and their nested messages
 * 2. Detects parallel agent spawns (within 2 second window)
 * 3. Groups messages appropriately for rendering
 */
export function groupMessages(
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

  // Detect parallel agents (Task calls within threshold of each other)
  const parallelGroups = detectParallelGroups(taskCalls);

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

/**
 * Detect parallel agent spawns (Task calls within threshold of each other).
 * Returns array of parallel groups, where each group has 1+ tasks.
 */
function detectParallelGroups(taskCalls: ChatMessage[]): ChatMessage[][] {
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

  return parallelGroups;
}
