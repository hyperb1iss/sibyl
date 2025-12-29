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
          // Headings
          h1: ({ children }) => (
            <h1 className="text-2xl font-bold text-sc-fg-primary mt-6 mb-4 first:mt-0">
              {children}
            </h1>
          ),
          h2: ({ children }) => (
            <h2 className="text-xl font-bold text-sc-fg-primary mt-5 mb-3 first:mt-0">
              {children}
            </h2>
          ),
          h3: ({ children }) => (
            <h3 className="text-lg font-semibold text-sc-fg-primary mt-4 mb-2 first:mt-0">
              {children}
            </h3>
          ),
          h4: ({ children }) => (
            <h4 className="text-base font-semibold text-sc-fg-primary mt-3 mb-2 first:mt-0">
              {children}
            </h4>
          ),

          // Paragraphs
          p: ({ children }) => (
            <p className="text-sc-fg-muted leading-relaxed mb-4 last:mb-0">{children}</p>
          ),

          // Lists
          ul: ({ children }) => (
            <ul className="list-disc list-inside text-sc-fg-muted mb-4 space-y-1">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="list-decimal list-inside text-sc-fg-muted mb-4 space-y-1">{children}</ol>
          ),
          li: ({ children }) => <li className="leading-relaxed">{children}</li>,

          // Links
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sc-cyan hover:text-sc-purple underline underline-offset-2 transition-colors"
            >
              {children}
            </a>
          ),

          // Blockquotes
          blockquote: ({ children }) => (
            <blockquote className="border-l-4 border-sc-purple/50 pl-4 py-1 my-4 text-sc-fg-muted italic bg-sc-purple/5 rounded-r-lg">
              {children}
            </blockquote>
          ),

          // Code - both inline and block
          code: CodeBlock,

          // Pre wrapper (shiki handles its own pre, but this catches non-highlighted)
          pre: ({ children }) => <>{children}</>,

          // Horizontal rules
          hr: () => <hr className="my-6 border-sc-fg-subtle/30" />,

          // Tables
          table: ({ children }) => (
            <div className="overflow-x-auto my-4">
              <table className="w-full border-collapse text-sm">{children}</table>
            </div>
          ),
          thead: ({ children }) => (
            <thead className="border-b border-sc-fg-subtle/30">{children}</thead>
          ),
          tbody: ({ children }) => <tbody>{children}</tbody>,
          tr: ({ children }) => <tr className="border-b border-sc-fg-subtle/10">{children}</tr>,
          th: ({ children }) => (
            <th className="px-3 py-2 text-left text-sc-fg-primary font-semibold">{children}</th>
          ),
          td: ({ children }) => <td className="px-3 py-2 text-sc-fg-muted">{children}</td>,

          // Strong & emphasis
          strong: ({ children }) => (
            <strong className="font-semibold text-sc-fg-primary">{children}</strong>
          ),
          em: ({ children }) => <em className="italic text-sc-fg-muted">{children}</em>,

          // Strikethrough
          del: ({ children }) => <del className="line-through text-sc-fg-subtle">{children}</del>,

          // Images (native img for external markdown content)
          img: ({ src, alt }) => (
            <img
              src={src}
              alt={alt || ''}
              className="rounded-xl my-4 max-w-full border border-sc-fg-subtle/20"
            />
          ),

          // Task lists (GFM)
          input: ({ checked }) => (
            <input type="checkbox" checked={checked} readOnly className="mr-2 accent-sc-purple" />
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
