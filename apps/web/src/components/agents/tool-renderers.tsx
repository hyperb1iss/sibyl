'use client';

import { useEffect, useState } from 'react';
import { codeToHtml } from 'shiki';
import { Code, EditPencil, Folder, Page, Search } from '@/components/ui/icons';
import { stripAnsi } from './chat-constants';

// SilkCircuit theme (shared with markdown.tsx)
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

// Infer language from file path
function getLanguageFromPath(filePath: string): string {
  const ext = filePath.split('.').pop()?.toLowerCase() || '';
  const langMap: Record<string, string> = {
    ts: 'typescript',
    tsx: 'tsx',
    js: 'javascript',
    jsx: 'jsx',
    py: 'python',
    rs: 'rust',
    go: 'go',
    rb: 'ruby',
    java: 'java',
    c: 'c',
    cpp: 'cpp',
    h: 'c',
    hpp: 'cpp',
    cs: 'csharp',
    swift: 'swift',
    kt: 'kotlin',
    scala: 'scala',
    php: 'php',
    sh: 'bash',
    bash: 'bash',
    zsh: 'bash',
    fish: 'fish',
    ps1: 'powershell',
    sql: 'sql',
    graphql: 'graphql',
    json: 'json',
    yaml: 'yaml',
    yml: 'yaml',
    toml: 'toml',
    xml: 'xml',
    html: 'html',
    css: 'css',
    scss: 'scss',
    less: 'less',
    md: 'markdown',
    mdx: 'mdx',
    dockerfile: 'dockerfile',
    makefile: 'makefile',
    vim: 'vim',
    lua: 'lua',
    r: 'r',
    julia: 'julia',
    zig: 'zig',
    nim: 'nim',
    ex: 'elixir',
    exs: 'elixir',
    erl: 'erlang',
    hrl: 'erlang',
    clj: 'clojure',
    cljs: 'clojure',
    hs: 'haskell',
    ml: 'ocaml',
    fs: 'fsharp',
    v: 'v',
    sol: 'solidity',
    prisma: 'prisma',
    proto: 'protobuf',
    tf: 'hcl',
    nix: 'nix',
  };
  return langMap[ext] || 'text';
}

// Highlighted code block component
function HighlightedCode({
  code,
  language,
  maxHeight = 300,
}: {
  code: string;
  language: string;
  maxHeight?: number;
}) {
  const [html, setHtml] = useState<string | null>(null);

  useEffect(() => {
    codeToHtml(code, { lang: language, theme: silkCircuitTheme })
      .then(setHtml)
      .catch(() => setHtml(null));
  }, [code, language]);

  if (html) {
    return (
      <div
        className="overflow-auto rounded-lg border border-sc-fg-subtle/20 [&>pre]:!bg-sc-bg-dark [&>pre]:p-3 [&>pre]:overflow-x-auto [&_code]:text-xs [&_code]:leading-relaxed"
        style={{ maxHeight }}
        // biome-ignore lint/security/noDangerouslySetInnerHtml: shiki output is safe
        dangerouslySetInnerHTML={{ __html: html }}
      />
    );
  }

  return (
    <pre
      className="overflow-auto rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-dark p-3 text-xs"
      style={{ maxHeight }}
    >
      <code className="font-mono text-sc-fg-primary leading-relaxed">{code}</code>
    </pre>
  );
}

// File path header component
function FilePathHeader({
  path,
  icon: Icon = Page,
  action,
}: {
  path: string;
  icon?: typeof Page;
  action?: string;
}) {
  // Show last 3 path segments
  const shortPath = path.split('/').slice(-3).join('/');
  const isFullPath = shortPath === path;

  return (
    <div className="flex items-center gap-2 px-2 py-1.5 bg-sc-bg-elevated/50 border-b border-sc-fg-subtle/10 rounded-t-lg">
      <Icon width={12} height={12} className="text-sc-purple shrink-0" />
      {action && <span className="text-[10px] text-sc-cyan font-medium uppercase">{action}</span>}
      <span className="text-xs font-mono text-sc-fg-muted truncate" title={path}>
        {!isFullPath && <span className="text-sc-fg-subtle">...</span>}
        {shortPath}
      </span>
    </div>
  );
}

// Read tool renderer - shows file content with syntax highlighting
export function ReadToolRenderer({
  input,
  result,
  isError,
}: {
  input: { file_path?: string; offset?: number; limit?: number };
  result?: string;
  isError?: boolean;
}) {
  const filePath = input.file_path || 'unknown';
  const language = getLanguageFromPath(filePath);

  if (isError || !result) {
    return (
      <div className="rounded-lg overflow-hidden">
        <FilePathHeader path={filePath} />
        <div className="p-3 bg-sc-red/5 text-sc-red text-xs">{result || 'Failed to read file'}</div>
      </div>
    );
  }

  // Show line numbers in header if offset is specified
  const lineInfo =
    input.offset || input.limit
      ? ` (lines ${input.offset || 1}-${(input.offset || 0) + (input.limit || 0)})`
      : '';

  return (
    <div className="rounded-lg overflow-hidden">
      <FilePathHeader path={filePath + lineInfo} />
      <HighlightedCode code={result} language={language} />
    </div>
  );
}

// Edit tool renderer - shows diff view
export function EditToolRenderer({
  input,
  result,
  isError,
}: {
  input: { file_path?: string; old_string?: string; new_string?: string; replace_all?: boolean };
  result?: string;
  isError?: boolean;
}) {
  const filePath = input.file_path || 'unknown';
  const language = getLanguageFromPath(filePath);
  const oldStr = input.old_string || '';
  const newStr = input.new_string || '';

  if (isError) {
    return (
      <div className="rounded-lg overflow-hidden">
        <FilePathHeader path={filePath} icon={EditPencil} action="edit" />
        <div className="p-3 bg-sc-red/5 text-sc-red text-xs">{result || 'Edit failed'}</div>
      </div>
    );
  }

  return (
    <div className="rounded-lg overflow-hidden">
      <FilePathHeader path={filePath} icon={EditPencil} action="edit" />
      <div className="grid grid-cols-2 gap-px bg-sc-fg-subtle/10">
        {/* Old content */}
        <div className="bg-sc-bg-base">
          <div className="px-2 py-1 text-[10px] font-medium text-sc-red bg-sc-red/10 border-b border-sc-red/20">
            - Removed
          </div>
          <HighlightedCode code={oldStr} language={language} maxHeight={200} />
        </div>
        {/* New content */}
        <div className="bg-sc-bg-base">
          <div className="px-2 py-1 text-[10px] font-medium text-sc-green bg-sc-green/10 border-b border-sc-green/20">
            + Added
          </div>
          <HighlightedCode code={newStr} language={language} maxHeight={200} />
        </div>
      </div>
      {input.replace_all && (
        <div className="px-2 py-1 text-[10px] text-sc-yellow bg-sc-yellow/10">
          Replaced all occurrences
        </div>
      )}
    </div>
  );
}

// Write tool renderer - shows what was written
export function WriteToolRenderer({
  input,
  result,
  isError,
}: {
  input: { file_path?: string; content?: string };
  result?: string;
  isError?: boolean;
}) {
  const filePath = input.file_path || 'unknown';
  const language = getLanguageFromPath(filePath);
  const content = input.content || '';

  if (isError) {
    return (
      <div className="rounded-lg overflow-hidden">
        <FilePathHeader path={filePath} icon={Page} action="write" />
        <div className="p-3 bg-sc-red/5 text-sc-red text-xs">{result || 'Write failed'}</div>
      </div>
    );
  }

  return (
    <div className="rounded-lg overflow-hidden">
      <FilePathHeader path={filePath} icon={Page} action="write" />
      <HighlightedCode code={content} language={language} />
    </div>
  );
}

// Bash tool renderer - shows command and output
export function BashToolRenderer({
  input,
  result,
  isError,
}: {
  input: { command?: string; description?: string };
  result?: string;
  isError?: boolean;
}) {
  const command = input.command || '';
  const description = input.description;
  // Strip ANSI escape codes from terminal output
  const cleanResult = result ? stripAnsi(result) : undefined;

  return (
    <div className="rounded-lg overflow-hidden">
      {/* Command header */}
      <div className="flex items-center gap-2 px-2 py-1.5 bg-sc-bg-elevated/50 border-b border-sc-fg-subtle/10 rounded-t-lg">
        <Code width={12} height={12} className="text-sc-cyan shrink-0" />
        <span className="text-[10px] text-sc-cyan font-medium uppercase">bash</span>
        {description && (
          <span className="text-[10px] text-sc-fg-subtle truncate">{description}</span>
        )}
      </div>

      {/* Command */}
      <div className="px-3 py-2 bg-sc-bg-dark border-b border-sc-fg-subtle/10">
        <code className="text-xs font-mono text-sc-purple">$ {command}</code>
      </div>

      {/* Output */}
      {cleanResult && (
        <pre
          className={`px-3 py-2 text-xs font-mono overflow-auto max-h-64 ${
            isError ? 'bg-sc-red/5 text-sc-red' : 'bg-sc-bg-dark text-sc-fg-primary'
          }`}
        >
          {cleanResult}
        </pre>
      )}
    </div>
  );
}

// Grep tool renderer - shows search results
export function GrepToolRenderer({
  input,
  result,
  isError,
}: {
  input: { pattern?: string; path?: string; type?: string };
  result?: string;
  isError?: boolean;
}) {
  const pattern = input.pattern || '';
  const searchPath = input.path || '.';

  return (
    <div className="rounded-lg overflow-hidden">
      <div className="flex items-center gap-2 px-2 py-1.5 bg-sc-bg-elevated/50 border-b border-sc-fg-subtle/10 rounded-t-lg">
        <Search width={12} height={12} className="text-sc-cyan shrink-0" />
        <span className="text-[10px] text-sc-cyan font-medium uppercase">grep</span>
        <code className="text-xs font-mono text-sc-purple truncate">{pattern}</code>
        <span className="text-[10px] text-sc-fg-subtle">in {searchPath}</span>
      </div>

      {result && (
        <pre
          className={`px-3 py-2 text-xs font-mono overflow-auto max-h-64 ${
            isError ? 'bg-sc-red/5 text-sc-red' : 'bg-sc-bg-dark text-sc-fg-primary'
          }`}
        >
          {result}
        </pre>
      )}
    </div>
  );
}

// Glob tool renderer - shows file list
export function GlobToolRenderer({
  input,
  result,
  isError,
}: {
  input: { pattern?: string; path?: string };
  result?: string;
  isError?: boolean;
}) {
  const pattern = input.pattern || '';

  // Parse file list from result
  const files = result?.split('\n').filter(Boolean) || [];

  return (
    <div className="rounded-lg overflow-hidden">
      <div className="flex items-center gap-2 px-2 py-1.5 bg-sc-bg-elevated/50 border-b border-sc-fg-subtle/10 rounded-t-lg">
        <Folder width={12} height={12} className="text-sc-cyan shrink-0" />
        <span className="text-[10px] text-sc-cyan font-medium uppercase">glob</span>
        <code className="text-xs font-mono text-sc-purple truncate">{pattern}</code>
        {files.length > 0 && (
          <span className="text-[10px] text-sc-fg-subtle ml-auto">
            {files.length} file{files.length !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {files.length > 0 ? (
        <div className="max-h-48 overflow-auto bg-sc-bg-dark">
          {files.map((file, i) => (
            <div
              key={file}
              className={`px-3 py-1 text-xs font-mono text-sc-fg-primary ${
                i % 2 === 0 ? 'bg-sc-bg-dark' : 'bg-sc-bg-elevated/30'
              }`}
            >
              {file}
            </div>
          ))}
        </div>
      ) : isError ? (
        <div className="p-3 bg-sc-red/5 text-sc-red text-xs">{result || 'No matches'}</div>
      ) : (
        <div className="p-3 text-sc-fg-subtle text-xs italic">No files matched</div>
      )}
    </div>
  );
}

// Main dispatcher - picks the right renderer based on tool name
export function ToolContentRenderer({
  toolName,
  input,
  result,
  isError,
}: {
  toolName: string;
  input?: Record<string, unknown>;
  result?: string;
  isError?: boolean;
}) {
  const safeInput = input || {};

  switch (toolName) {
    case 'Read':
      return (
        <ReadToolRenderer
          input={safeInput as { file_path?: string; offset?: number; limit?: number }}
          result={result}
          isError={isError}
        />
      );
    case 'Edit':
      return (
        <EditToolRenderer
          input={
            safeInput as {
              file_path?: string;
              old_string?: string;
              new_string?: string;
              replace_all?: boolean;
            }
          }
          result={result}
          isError={isError}
        />
      );
    case 'Write':
      return (
        <WriteToolRenderer
          input={safeInput as { file_path?: string; content?: string }}
          result={result}
          isError={isError}
        />
      );
    case 'Bash':
      return (
        <BashToolRenderer
          input={safeInput as { command?: string; description?: string }}
          result={result}
          isError={isError}
        />
      );
    case 'Grep':
      return (
        <GrepToolRenderer
          input={safeInput as { pattern?: string; path?: string; type?: string }}
          result={result}
          isError={isError}
        />
      );
    case 'Glob':
      return (
        <GlobToolRenderer
          input={safeInput as { pattern?: string; path?: string }}
          result={result}
          isError={isError}
        />
      );
    default:
      // Fallback - just show raw content
      if (!result) return null;
      return (
        <pre className="px-3 py-2 text-xs font-mono overflow-auto max-h-64 bg-sc-bg-dark text-sc-fg-primary rounded-lg">
          {result}
        </pre>
      );
  }
}
