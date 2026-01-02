/**
 * WebSocket client for realtime updates from Sibyl backend.
 */

import { env } from 'next-dynenv';

// =============================================================================
// Event Payload Types (Discriminated Unions)
// =============================================================================

/** Entity mutation event payloads */
export interface EntityEventPayload {
  id: string;
  entity_type?: string;
  type?: string; // Alternative field name from backend
}

/** Connection status payload */
export interface ConnectionStatusPayload {
  status: ConnectionStatus;
}

/** Crawl lifecycle payloads */
export interface CrawlStartedPayload {
  source_id: string;
}

export interface CrawlProgressPayload {
  source_id: string;
  source_name?: string;
  pages_crawled?: number;
  max_pages?: number;
  current_url?: string;
  percentage?: number;
  documents_crawled?: number;
  documents_stored?: number;
  chunks_created?: number;
  chunks_added?: number;
  errors?: number;
}

export interface CrawlCompletePayload {
  source_id: string;
  error?: string;
}

/** Agent event payloads */
export interface AgentStatusPayload {
  agent_id: string;
  status: string;
  reason?: string;
}

export interface AgentMessagePayload {
  agent_id: string;
  message_num: number;
  role: string;
  type: string;
  content?: string;
  preview?: string;
  blocks?: unknown[];
  icon?: string;
  tool_name?: string;
  tool_id?: string;
  is_error?: boolean;
  usage?: { input_tokens: number; output_tokens: number };
  cost_usd?: number;
  timestamp?: string;
}

export interface AgentWorkspacePayload {
  agent_id: string;
  worktree_path?: string;
  branch?: string;
  modified_files?: string[];
}

/** Approval event payload */
export interface ApprovalResponsePayload {
  approval_id: string;
  agent_id?: string;
  action: 'approve' | 'deny' | 'edit';
  message?: string;
}

/** Map event types to their payload types */
export interface WebSocketEventPayloadMap {
  entity_created: EntityEventPayload;
  entity_updated: EntityEventPayload;
  entity_deleted: EntityEventPayload;
  search_complete: Record<string, unknown>;
  crawl_started: CrawlStartedPayload;
  crawl_progress: CrawlProgressPayload;
  crawl_complete: CrawlCompletePayload;
  health_update: Record<string, unknown>;
  heartbeat: Record<string, unknown>;
  connection_status: ConnectionStatusPayload;
  agent_status: AgentStatusPayload;
  agent_message: AgentMessagePayload;
  agent_workspace: AgentWorkspacePayload;
  approval_response: ApprovalResponsePayload;
}

export type WebSocketEventType = keyof WebSocketEventPayloadMap;

export interface WebSocketMessage<T extends WebSocketEventType = WebSocketEventType> {
  event: T;
  data: WebSocketEventPayloadMap[T];
  timestamp: string;
}

export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected' | 'reconnecting';

/** Type-safe event handler */
type EventHandler<T extends WebSocketEventType = WebSocketEventType> = (
  data: WebSocketEventPayloadMap[T]
) => void;

class WebSocketClient {
  private ws: WebSocket | null = null;
  // Store handlers with loose typing internally; type safety enforced by on() signature
  private handlers: Map<WebSocketEventType, Set<EventHandler<WebSocketEventType>>> = new Map();
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 10;
  private reconnectDelay = 1000;
  private _status: ConnectionStatus = 'disconnected';
  private statusDebounceTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pendingStatus: ConnectionStatus | null = null;
  private readonly STATUS_DEBOUNCE_MS = 100;

  get status(): ConnectionStatus {
    return this._status;
  }

  private getWebSocketUrl(): string {
    // Check for explicit WS URL first
    const wsUrl = env('NEXT_PUBLIC_WS_URL');
    if (wsUrl) {
      return wsUrl;
    }

    // Check for API URL and derive WS URL from it
    const apiUrl = env('NEXT_PUBLIC_API_URL');
    if (apiUrl) {
      const url = new URL(apiUrl);
      const wsProtocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      return `${wsProtocol}//${url.host}/api/ws`;
    }

    // Development default: connect directly to backend
    if (typeof window !== 'undefined' && window.location.hostname === 'localhost') {
      return 'ws://localhost:3334/api/ws';
    }

    // Fallback: derive from current location (production)
    const protocol =
      typeof window !== 'undefined' && window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = typeof window !== 'undefined' ? window.location.host : 'localhost:3334';
    return `${protocol}//${host}/api/ws`;
  }

  private setStatus(status: ConnectionStatus): void {
    // Skip if status hasn't changed
    if (this._status === status) return;

    this._status = status;

    // Debounce status dispatches to prevent UI flickering
    // "connected" is dispatched immediately; others are debounced
    if (status === 'connected') {
      // Clear any pending debounce and dispatch immediately
      if (this.statusDebounceTimer) {
        clearTimeout(this.statusDebounceTimer);
        this.statusDebounceTimer = null;
        this.pendingStatus = null;
      }
      this.dispatch('connection_status', { status });
    } else {
      this.pendingStatus = status;
      if (!this.statusDebounceTimer) {
        this.statusDebounceTimer = setTimeout(() => {
          if (this.pendingStatus) {
            this.dispatch('connection_status', { status: this.pendingStatus });
          }
          this.statusDebounceTimer = null;
          this.pendingStatus = null;
        }, this.STATUS_DEBOUNCE_MS);
      }
    }
  }

  connect(): void {
    if (this.ws?.readyState === WebSocket.OPEN) return;

    this.setStatus('connecting');

    // Connect directly to backend WS endpoint, not through Next.js rewrites
    // In development: ws://localhost:3334/api/ws
    // In production: Use NEXT_PUBLIC_WS_URL or derive from API URL
    const wsUrl = this.getWebSocketUrl();
    this.ws = new WebSocket(wsUrl);

    this.ws.onopen = () => {
      this.reconnectAttempts = 0;
      this.setStatus('connected');
    };

    this.ws.onmessage = event => {
      try {
        const message = JSON.parse(event.data) as WebSocketMessage;

        // Respond to server heartbeat to keep connection alive
        if (message.event === 'heartbeat') {
          this.send('pong');
          return;
        }

        // Type assertion: backend guarantees event/data pairs match
        this.dispatch(
          message.event,
          message.data as WebSocketEventPayloadMap[typeof message.event]
        );
      } catch (e) {
        console.error('[Sibyl WS] Failed to parse message:', e);
      }
    };

    this.ws.onclose = () => {
      this.setStatus('disconnected');
      this.attemptReconnect();
    };

    this.ws.onerror = () => {
      // WebSocket errors don't expose details (browser security).
      // onclose handles reconnection - no need to log here.
    };
  }

  disconnect(): void {
    // Clear reconnect timer to prevent zombie reconnections
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }

    // Clear status debounce timer
    if (this.statusDebounceTimer) {
      clearTimeout(this.statusDebounceTimer);
      this.statusDebounceTimer = null;
      this.pendingStatus = null;
    }

    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  /** Full cleanup - clears all timers and handlers */
  destroy(): void {
    this.disconnect();
    this.handlers.clear();
    this.reconnectAttempts = 0;
  }

  private attemptReconnect(): void {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      // Not an error - just stop trying. UI shows offline state.
      return;
    }

    this.reconnectAttempts++;
    const delay = Math.min(this.reconnectDelay * 2 ** (this.reconnectAttempts - 1), 30000);
    this.setStatus('reconnecting');

    // Store timer ID so it can be cleared on disconnect()
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  /** Subscribe to a typed WebSocket event */
  on<T extends WebSocketEventType>(event: T, handler: EventHandler<T>): () => void {
    let handlers = this.handlers.get(event);
    if (!handlers) {
      handlers = new Set();
      this.handlers.set(event, handlers);
    }
    // Cast needed due to Map's loose internal typing
    handlers.add(handler as EventHandler<WebSocketEventType>);

    // Return unsubscribe function
    return () => this.off(event, handler);
  }

  off<T extends WebSocketEventType>(event: T, handler: EventHandler<T>): void {
    const handlers = this.handlers.get(event);
    handlers?.delete(handler as EventHandler<WebSocketEventType>);
    // Clean up empty Sets to prevent Map accumulation
    if (handlers?.size === 0) {
      this.handlers.delete(event);
    }
  }

  private dispatch<T extends WebSocketEventType>(event: T, data: WebSocketEventPayloadMap[T]): void {
    this.handlers.get(event)?.forEach(handler => {
      try {
        handler(data);
      } catch (e) {
        console.error(`[Sibyl WS] Handler error for ${event}:`, e);
      }
    });
  }

  send(type: string, data: Record<string, unknown> = {}): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type, ...data }));
    }
  }

  ping(): void {
    this.send('ping');
  }
}

// Singleton instance
export const wsClient = new WebSocketClient();
