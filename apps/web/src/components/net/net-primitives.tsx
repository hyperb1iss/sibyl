'use client';

import { useState } from 'react';
import { initialsFor, toneIndexFor } from '@/components/team/person-avatar';
import {
  BODY_PREVIEW_LINES,
  bodyPreview,
  bodySegments,
  channelParts,
  controlLabel,
  controlName,
} from './net-format';

/** A channel name with its `@` or `#` set back, so the name itself leads. */
export function ChannelName({ channel, className = '' }: { channel: string; className?: string }) {
  const { sigil, name } = channelParts(channel);
  return (
    <span className={`min-w-0 truncate ${className}`}>
      {sigil && <span className="mr-px font-normal text-sc-fg-muted">{sigil}</span>}
      {name}
    </span>
  );
}

// The Team page's person tints, flat: same hue per person on both pages.
const AUTHOR_TONES = [
  'border-sc-purple/35 bg-sc-purple/12 text-sc-purple',
  'border-sc-cyan/35 bg-sc-cyan/12 text-sc-cyan',
  'border-sc-coral/35 bg-sc-coral/12 text-sc-coral',
  'border-sc-green/35 bg-sc-green/12 text-sc-green',
  'border-sc-yellow/40 bg-sc-yellow/12 text-sc-yellow',
] as const;

/** Initials on the author's tint. Telling two authors apart at a glance is how a loop shows. */
export function AuthorMark({
  name,
  seed,
  size = 'md',
}: {
  name: string;
  seed: string;
  size?: 'sm' | 'md';
}) {
  return (
    <span
      aria-hidden="true"
      className={`inline-flex shrink-0 items-center justify-center rounded-full border font-semibold ${
        size === 'sm' ? 'h-5 w-5 text-[8px]' : 'h-7 w-7 text-[10px]'
      } ${AUTHOR_TONES[toneIndexFor(seed)]}`}
    >
      {initialsFor(name)}
    </span>
  );
}

/** A client-asserted agent label such as `claude-code`. */
export function AgentLabel({ agent, session }: { agent: string; session?: string | null }) {
  return (
    <span
      title={
        session ? `Agent label from the client. Session ${session}` : 'Agent label from the client'
      }
      className="inline-flex max-w-[12rem] items-center rounded border border-sc-fg-subtle/30 bg-sc-bg-highlight px-1.5 font-mono text-[10.5px] leading-[1.125rem] text-sc-fg-secondary"
    >
      <span className="truncate">{agent}</span>
    </span>
  );
}

function PlainText({ text }: { text: string }) {
  return (
    <>
      {bodySegments(text).map((segment, index) =>
        segment.kind === 'text' ? (
          // Segments are positional slices of one string, so the index is stable.
          <span key={index}>{segment.text}</span>
        ) : (
          <abbr
            key={index}
            title={`Control character ${controlLabel(segment.code)}`}
            className="mx-px inline-block rounded-sm border border-sc-yellow/30 bg-sc-yellow/10 px-1 align-[0.1em] font-mono text-[0.68em] leading-normal font-semibold text-sc-yellow no-underline"
          >
            {controlName(segment.code)}
          </abbr>
        )
      )}
    </>
  );
}

/**
 * A message body as plain text: newlines kept, nothing interpreted, hidden
 * control characters shown as marks. Long bodies open collapsed.
 */
export function MessageBody({ body }: { body: string }) {
  const preview = bodyPreview(body);
  const [expanded, setExpanded] = useState(false);
  const collapsed = preview !== null && !expanded;

  return (
    <div className="min-w-0">
      <p className="whitespace-pre-wrap text-sm leading-relaxed text-sc-fg-primary [overflow-wrap:anywhere]">
        <PlainText text={collapsed ? preview.preview : body} />
        {collapsed && <span className="text-sc-fg-muted">{' …'}</span>}
      </p>
      {preview && (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded(value => !value)}
          className="mt-1 rounded text-xs font-medium text-sc-cyan hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
        >
          {expanded
            ? 'Show less'
            : preview.lines > BODY_PREVIEW_LINES
              ? `Show all ${preview.lines.toLocaleString('en-US')} lines`
              : 'Show the whole message'}
        </button>
      )}
    </div>
  );
}

/** A command to copy into a terminal, in the app's code style. */
export function CommandLine({ command }: { command: string }) {
  return (
    <pre className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base py-2 pr-3 pl-7 -indent-4 text-left font-mono text-xs leading-relaxed whitespace-pre-wrap text-sc-fg-secondary [overflow-wrap:anywhere]">
      <span aria-hidden="true" className="select-none text-sc-fg-muted">
        ${' '}
      </span>
      <code>{command}</code>
    </pre>
  );
}
