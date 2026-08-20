import { describe, expect, it } from 'vitest';
import type { RAGChunkResult } from '@/lib/api/search';
import { render, screen } from '@/test/utils';
import { DocChunkResult } from './doc-result';

const baseChunk: RAGChunkResult = {
  chunk_id: 'chunk-1',
  document_id: 'doc-1',
  source_id: 'source-1',
  source_name: 'Docs',
  url: 'https://docs.example.com/auth',
  title: 'Auth Guide',
  content: 'plain authentication content',
  context: null,
  snippet: null,
  similarity: 0.82,
  chunk_type: 'text',
  chunk_index: 0,
  heading_path: ['Auth'],
  language: null,
};

describe('DocChunkResult', () => {
  it('renders highlighted snippets without injecting markup text', () => {
    const { container } = render(
      <DocChunkResult
        result={{
          ...baseChunk,
          snippet: 'plain <mark>authentication</mark> content',
        }}
      />
    );

    expect(screen.getByText('authentication')).toBeInTheDocument();
    expect(container).not.toHaveTextContent('<mark>');
    expect(container.querySelector('mark')).toHaveTextContent('authentication');
  });
});
