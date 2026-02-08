'use client';

import { FitAddon } from '@xterm/addon-fit';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { env } from 'next-dynenv';
import { useEffect, useMemo, useRef, useState } from 'react';

type SandboxConnectionState = 'connecting' | 'connected' | 'disconnected' | 'error';

export interface SandboxTerminalProps {
  sandboxId: string;
  className?: string;
  attachPath?: string;
  token?: string;
  initialCommand?: string;
  readOnly?: boolean;
  onConnectionChange?: (state: SandboxConnectionState) => void;
}

function toWsBaseUrl(): string {
  const explicitWs = env('NEXT_PUBLIC_WS_URL');
  if (explicitWs) {
    try {
      const url = new URL(explicitWs);
      return `${url.protocol}//${url.host}`;
    } catch {
      // Fall through to API URL / window location.
    }
  }

  const explicitApi = env('NEXT_PUBLIC_API_URL');
  if (explicitApi) {
    const apiUrl = new URL(explicitApi);
    const wsProtocol = apiUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${wsProtocol}//${apiUrl.host}`;
  }

  if (typeof window !== 'undefined' && window.location.hostname === 'localhost') {
    return 'ws://localhost:3334';
  }

  const protocol =
    typeof window !== 'undefined' && window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = typeof window !== 'undefined' ? window.location.host : 'localhost:3334';
  return `${protocol}//${host}`;
}

function appendToken(url: string, token?: string): string {
  if (!token) return url;
  const separator = url.includes('?') ? '&' : '?';
  return `${url}${separator}token=${encodeURIComponent(token)}`;
}

function normalizeMessageData(data: unknown): string {
  if (typeof data === 'string') return data;
  if (data instanceof ArrayBuffer) {
    return new TextDecoder().decode(data);
  }
  return '';
}

function statusClass(state: SandboxConnectionState): string {
  switch (state) {
    case 'connected':
      return 'text-sc-green';
    case 'connecting':
      return 'text-sc-yellow';
    case 'error':
      return 'text-sc-red';
    default:
      return 'text-sc-fg-subtle';
  }
}

export function SandboxTerminal({
  sandboxId,
  className,
  attachPath,
  token,
  initialCommand,
  readOnly = false,
  onConnectionChange,
}: SandboxTerminalProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const [connectionState, setConnectionState] = useState<SandboxConnectionState>('disconnected');

  const wsCandidates = useMemo(() => {
    const base = toWsBaseUrl();
    const defaultPaths = [
      `/api/sandboxes/${sandboxId}/attach`,
      `/api/sandbox/${sandboxId}/attach`,
      `/api/sandboxes/${sandboxId}/shell/ws`,
    ];
    const paths = attachPath ? [attachPath] : defaultPaths;
    return paths.map(path => appendToken(`${base}${path}`, token));
  }, [attachPath, sandboxId, token]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const terminal = new Terminal({
      convertEol: true,
      cursorBlink: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
      fontSize: 13,
      lineHeight: 1.25,
      theme: {
        background: '#101216',
        foreground: '#e7ecf3',
        cursor: '#80ffea',
      },
    });

    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(container);
    fitAddon.fit();
    terminal.focus();

    const onResize = () => {
      fitAddon.fit();
      const socket = socketRef.current;
      if (!socket || socket.readyState !== WebSocket.OPEN) return;

      socket.send(
        JSON.stringify({
          type: 'resize',
          cols: terminal.cols,
          rows: terminal.rows,
        })
      );
    };

    const setState = (state: SandboxConnectionState) => {
      setConnectionState(state);
      onConnectionChange?.(state);
    };

    let active = true;

    const connect = (index: number) => {
      if (!active || index >= wsCandidates.length) {
        setState('error');
        return;
      }

      setState('connecting');
      const socket = new WebSocket(wsCandidates[index]);
      socket.binaryType = 'arraybuffer';
      socketRef.current = socket;

      socket.onopen = () => {
        if (!active) return;
        setState('connected');
        onResize();

        if (initialCommand) {
          socket.send(`${initialCommand}\n`);
        }
      };

      socket.onmessage = event => {
        if (!active) return;

        if (event.data instanceof Blob) {
          void event.data.arrayBuffer().then(buffer => {
            if (!active) return;
            terminal.write(normalizeMessageData(buffer));
          });
          return;
        }

        terminal.write(normalizeMessageData(event.data));
      };

      socket.onerror = () => {
        if (!active) return;
        setState('error');
      };

      socket.onclose = () => {
        if (!active) return;
        if (index + 1 < wsCandidates.length) {
          connect(index + 1);
          return;
        }
        setState('disconnected');
      };
    };

    connect(0);

    const inputDisposable = readOnly
      ? undefined
      : terminal.onData(data => {
          const socket = socketRef.current;
          if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(data);
          }
        });

    window.addEventListener('resize', onResize);

    return () => {
      active = false;
      inputDisposable?.dispose();
      window.removeEventListener('resize', onResize);
      socketRef.current?.close();
      socketRef.current = null;
      terminal.dispose();
    };
  }, [initialCommand, onConnectionChange, readOnly, wsCandidates]);

  return (
    <div
      className={`rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-elevated ${className ?? ''}`}
    >
      <div className="flex items-center justify-between border-b border-sc-fg-subtle/20 px-3 py-2 text-xs">
        <span className="font-medium text-sc-fg-primary">Sandbox {sandboxId}</span>
        <span className={statusClass(connectionState)}>{connectionState}</span>
      </div>
      <div ref={containerRef} className="h-[380px] w-full p-2" />
    </div>
  );
}
