'use client';

import { type ComponentPropsWithoutRef, useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { codeToHtml } from 'shiki';

interface MarkdownProps {
  content: string;
  className?: string;
}

// SilkCircuit-inspired shiki theme
const silkCircuitTheme = {
  name: 'silk-circuit',
  type: 'dark' as const,
  colors: {
    'editor.background': '#12101a',
    'editor.foreground': '#f8f8f2',
  },
  tokenColors: [
    {
      scope: ['comment', 'punctuation.definition.comment'],
      settings: { foreground: '#5a5470', fontStyle: 'italic' },
    },
    { scope: ['string', 'string.quoted'], settings: { foreground: '#50fa7b' } },
    { scope: ['constant.numeric', 'constant.language'], settings: { foreground: '#ff6ac1' } },
    { scope: ['keyword', 'storage.type', 'storage.modifier'], settings: { foreground: '#e135ff' } },
    { scope: ['entity.name.function', 'support.function'], settings: { foreground: '#80ffea' } },
    {
      scope: ['entity.name.class', 'entity.name.type', 'support.class'],
      settings: { foreground: '#f1fa8c' },
    },
    { scope: ['variable', 'variable.other'], settings: { foreground: '#f8f8f2' } },
    { scope: ['variable.parameter'], settings: { foreground: '#ffb86c' } },
    { scope: ['constant.other', 'entity.name.tag'], settings: { foreground: '#ff6ac1' } },
    { scope: ['entity.other.attribute-name'], settings: { foreground: '#50fa7b' } },
    { scope: ['punctuation', 'meta.brace'], settings: { foreground: '#8b85a0' } },
    { scope: ['keyword.operator'], settings: { foreground: '#ff6ac1' } },
    { scope: ['support.type.property-name'], settings: { foreground: '#80ffea' } },
    { scope: ['meta.object-literal.key'], settings: { foreground: '#80ffea' } },
  ],
};

// Extended props from react-markdown
interface CodeBlockProps extends ComponentPropsWithoutRef<'code'> {
  inline?: boolean;
  node?: { tagName?: string };
}

// Async code block with shiki highlighting
function CodeBlock({ className, children, inline, ...props }: CodeBlockProps) {
  const [html, setHtml] = useState<string | null>(null);
  const match = /language-(\w+)/.exec(className || '');
  const lang = match?.[1] || 'text';
  const code = String(children).replace(/\n$/, '');

  // Check if this is a code block (not inline)
  // react-markdown passes inline=true for inline code, or we check if code has newlines
  const isBlock = inline === false || (!inline && code.includes('\n'));

  useEffect(() => {
    // Only attempt highlighting for blocks (with or without language)
    if (!isBlock) return;

    codeToHtml(code, {
      lang: match ? lang : 'text',
      theme: silkCircuitTheme,
    })
      .then(setHtml)
      .catch(() => setHtml(null));
  }, [code, lang, match, isBlock]);

  // Inline code (no newlines, not in pre block)
  if (!isBlock) {
    return (
      <code
        className="px-1.5 py-0.5 rounded bg-sc-bg-elevated text-sc-coral font-mono text-[0.9em] border border-sc-fg-subtle/20"
        {...props}
      >
        {children}
      </code>
    );
  }

  // Block code - show highlighted or fallback
  if (html) {
    return (
      <div className="relative group my-4">
        {match && (
          <div className="absolute top-2 right-2 text-[10px] font-mono text-sc-fg-subtle uppercase opacity-0 group-hover:opacity-100 transition-opacity">
            {lang}
          </div>
        )}
        <div
          className="overflow-x-auto rounded-xl border border-sc-fg-subtle/20 [&>pre]:!bg-sc-bg-dark [&>pre]:p-4 [&>pre]:overflow-x-auto [&_code]:text-sm [&_code]:leading-relaxed"
          // biome-ignore lint/security/noDangerouslySetInnerHtml: shiki output is safe
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </div>
    );
  }

  // Fallback while loading
  return (
    <div className="relative group my-4">
      {match && (
        <div className="absolute top-2 right-2 text-[10px] font-mono text-sc-fg-subtle uppercase">
          {lang}
        </div>
      )}
      <pre className="overflow-x-auto rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-dark p-4">
        <code className="text-sm font-mono text-sc-fg-primary leading-relaxed">{code}</code>
      </pre>
    </div>
  );
}

export function Markdown({ content, className = '' }: MarkdownProps) {
  if (!content) return null;

  return (
    <div className={`prose-silk ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Headings - gradient accents for that electric feel
          h1: ({ children }) => (
            <h1 className="text-2xl font-bold bg-gradient-to-r from-sc-purple via-sc-fg-primary to-sc-cyan bg-clip-text text-transparent mt-6 mb-4 first:mt-0">
              {children}
            </h1>
          ),
          h2: ({ children }) => (
            <h2 className="text-xl font-bold text-sc-purple mt-5 mb-3 first:mt-0">{children}</h2>
          ),
          h3: ({ children }) => (
            <h3 className="text-lg font-semibold text-sc-cyan mt-4 mb-2 first:mt-0">{children}</h3>
          ),
          h4: ({ children }) => (
            <h4 className="text-base font-semibold text-sc-fg-secondary mt-3 mb-2 first:mt-0">
              {children}
            </h4>
          ),

          // Paragraphs - softer, more readable
          p: ({ children }) => (
            <p className="text-sc-fg-secondary leading-relaxed mb-3 last:mb-0">{children}</p>
          ),

          // Lists - colored markers, better spacing
          ul: ({ children }) => (
            <ul className="mb-4 space-y-1.5 pl-5 [&>li]:relative [&>li]:before:content-['•'] [&>li]:before:absolute [&>li]:before:-left-4 [&>li]:before:text-sc-purple [&>li]:before:font-bold">
              {children}
            </ul>
          ),
          ol: ({ children }) => (
            <ol className="mb-4 space-y-1.5 pl-5 list-decimal marker:text-sc-cyan marker:font-semibold">
              {children}
            </ol>
          ),
          li: ({ children }) => (
            <li className="text-sc-fg-secondary leading-relaxed [&>p]:inline [&>p]:m-0">
              {children}
            </li>
          ),

          // Links - cyan glow
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sc-cyan hover:text-sc-purple transition-colors decoration-sc-cyan/50 hover:decoration-sc-purple underline underline-offset-2"
            >
              {children}
            </a>
          ),

          // Blockquotes - purple accent with glow
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-sc-purple pl-4 py-2 my-4 text-sc-fg-muted italic bg-gradient-to-r from-sc-purple/10 to-transparent rounded-r-lg">
              {children}
            </blockquote>
          ),

          // Code - both inline and block
          code: CodeBlock,

          // Pre wrapper (shiki handles its own pre, but this catches non-highlighted)
          pre: ({ children }) => <>{children}</>,

          // Horizontal rules - gradient line
          hr: () => (
            <hr className="my-6 border-0 h-px bg-gradient-to-r from-transparent via-sc-purple/50 to-transparent" />
          ),

          // Tables - glass morphism style
          table: ({ children }) => (
            <div className="overflow-x-auto my-4 rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-elevated/50">
              <table className="w-full border-collapse text-sm">{children}</table>
            </div>
          ),
          thead: ({ children }) => (
            <thead className="bg-sc-purple/10 border-b border-sc-purple/20">{children}</thead>
          ),
          tbody: ({ children }) => (
            <tbody className="divide-y divide-sc-fg-subtle/10">{children}</tbody>
          ),
          tr: ({ children }) => (
            <tr className="hover:bg-sc-fg-subtle/5 transition-colors">{children}</tr>
          ),
          th: ({ children }) => (
            <th className="px-3 py-2.5 text-left text-sc-purple font-semibold text-xs uppercase tracking-wider">
              {children}
            </th>
          ),
          td: ({ children }) => <td className="px-3 py-2.5 text-sc-fg-secondary">{children}</td>,

          // Strong - coral accent for emphasis
          strong: ({ children }) => (
            <strong className="font-semibold text-sc-coral">{children}</strong>
          ),
          em: ({ children }) => <em className="italic text-sc-fg-muted">{children}</em>,

          // Strikethrough
          del: ({ children }) => <del className="line-through text-sc-fg-subtle">{children}</del>,

          // Images
          img: ({ src, alt }) => (
            <img
              src={src}
              alt={alt || ''}
              className="rounded-xl my-4 max-w-full border border-sc-purple/20 shadow-lg shadow-sc-purple/5"
            />
          ),

          // Task lists (GFM) - purple checkboxes
          input: ({ checked }) => (
            <input
              type="checkbox"
              checked={checked}
              readOnly
              className="mr-2 accent-sc-purple rounded"
            />
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
