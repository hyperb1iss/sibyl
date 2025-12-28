/**
 * WebSocket client for realtime updates from Sibyl backend.
 */

import { env } from 'next-dynenv';

export type WebSocketEventType =
  | 'entity_created'
  | 'entity_updated'
  | 'entity_deleted'
  | 'search_complete'
  | 'crawl_started'
  | 'crawl_progress'
  | 'crawl_complete'
  | 'health_update'
  | 'heartbeat'
  | 'connection_status';

export interface WebSocketMessage {
  event: WebSocketEventType;
  data: Record<string, unknown>;
  timestamp: string;
}

export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected' | 'reconnecting';

type EventHandler = (data: Record<string, unknown>) => void;

class WebSocketClient {
  private ws: WebSocket | null = null;
  private handlers: Map<WebSocketEventType, Set<EventHandler>> = new Map();
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 10;
  private reconnectDelay = 1000;
  private _status: ConnectionStatus = 'disconnected';
  private statusDebounceTimer: ReturnType<typeof setTimeout> | null = null;
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
    console.log('[Sibyl WS] Connecting to:', wsUrl);

    this.ws = new WebSocket(wsUrl);

    this.ws.onopen = () => {
      console.log('[Sibyl WS] Connected');
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

        this.dispatch(message.event, message.data);
      } catch (e) {
        console.error('[Sibyl WS] Failed to parse message:', e);
      }
    };

    this.ws.onclose = () => {
      console.log('[Sibyl WS] Disconnected');
      this.setStatus('disconnected');
      this.attemptReconnect();
    };

    this.ws.onerror = () => {
      // WebSocket errors don't expose details (browser security).
      // onclose handles reconnection - no need to log here.
    };
  }

  disconnect(): void {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  private attemptReconnect(): void {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      // Not an error - just stop trying. UI shows offline state.
      return;
    }

    this.reconnectAttempts++;
    const delay = Math.min(this.reconnectDelay * 2 ** (this.reconnectAttempts - 1), 30000);

    console.log(`[Sibyl WS] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);
    this.setStatus('reconnecting');

    setTimeout(() => this.connect(), delay);
  }

  on(event: WebSocketEventType, handler: EventHandler): () => void {
    let handlers = this.handlers.get(event);
    if (!handlers) {
      handlers = new Set();
      this.handlers.set(event, handlers);
    }
    handlers.add(handler);

    // Return unsubscribe function
    return () => {
      this.handlers.get(event)?.delete(handler);
    };
  }

  off(event: WebSocketEventType, handler: EventHandler): void {
    this.handlers.get(event)?.delete(handler);
  }

  private dispatch(event: WebSocketEventType, data: Record<string, unknown>): void {
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
