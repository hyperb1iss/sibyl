'use client';

import * as Dialog from '@radix-ui/react-dialog';
import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { FileText, Globe, Loader2, Plus, Upload, X } from '@/components/ui/icons';
import { api } from '@/lib/api';

interface AddSourceDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onSubmitUrl: (data: UrlSourceData) => Promise<void>;
  onSubmitFile?: (file: File, metadata: FileMetadata) => Promise<void>;
  isSubmitting?: boolean;
}

export interface UrlSourceData {
  url: string;
  name: string;
  description: string;
  crawlDepth: number;
  tags: string[];
  includePatterns: string[];
  excludePatterns: string[];
}

export interface FileMetadata {
  name: string;
  description: string;
  tags: string[];
}

type TabType = 'url' | 'file';

const SUGGESTED_TAGS = [
  'documentation',
  'api',
  'tutorial',
  'reference',
  'guide',
  'sdk',
  'framework',
  'library',
];

export function AddSourceDialog({
  isOpen,
  onClose,
  onSubmitUrl,
  onSubmitFile,
  isSubmitting = false,
}: AddSourceDialogProps) {
  const [activeTab, setActiveTab] = useState<TabType>('url');

  // URL form state
  const [url, setUrl] = useState('');
  const [urlName, setUrlName] = useState('');
  const [urlDescription, setUrlDescription] = useState('');
  const [crawlDepth, setCrawlDepth] = useState(2);
  const [urlTags, setUrlTags] = useState<string[]>([]);
  const [tagInput, setTagInput] = useState('');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [includePatterns, setIncludePatterns] = useState('');
  const [excludePatterns, setExcludePatterns] = useState('');
  const [isFetchingTitle, setIsFetchingTitle] = useState(false);
  const [userEditedName, setUserEditedName] = useState(false);
  const previewAbortRef = useRef<AbortController | null>(null);

  // File form state
  const [file, setFile] = useState<File | null>(null);
  const [fileName, setFileName] = useState('');
  const [fileDescription, setFileDescription] = useState('');
  const [fileTags, setFileTags] = useState<string[]>([]);
  const [isDragging, setIsDragging] = useState(false);

  const resetForm = useCallback(() => {
    setUrl('');
    setUrlName('');
    setUrlDescription('');
    setCrawlDepth(2);
    setUrlTags([]);
    setTagInput('');
    setShowAdvanced(false);
    setIncludePatterns('');
    setExcludePatterns('');
    setIsFetchingTitle(false);
    setUserEditedName(false);
    previewAbortRef.current?.abort();
    setFile(null);
    setFileName('');
    setFileDescription('');
    setFileTags([]);
  }, []);

  const handleClose = useCallback(() => {
    resetForm();
    onClose();
  }, [onClose, resetForm]);

  const handleAddTag = useCallback(
    (tag: string, isFile = false) => {
      const trimmed = tag.trim().toLowerCase();
      if (!trimmed) return;

      if (isFile) {
        if (!fileTags.includes(trimmed)) {
          setFileTags([...fileTags, trimmed]);
        }
      } else {
        if (!urlTags.includes(trimmed)) {
          setUrlTags([...urlTags, trimmed]);
        }
      }
      setTagInput('');
    },
    [urlTags, fileTags]
  );

  const handleRemoveTag = useCallback(
    (tag: string, isFile = false) => {
      if (isFile) {
        setFileTags(fileTags.filter(t => t !== tag));
      } else {
        setUrlTags(urlTags.filter(t => t !== tag));
      }
    },
    [urlTags, fileTags]
  );

  const handleUrlSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;

    const name = urlName.trim() || new URL(url).hostname;

    await onSubmitUrl({
      url: url.trim(),
      name,
      description: urlDescription.trim(),
      crawlDepth,
      tags: urlTags,
      includePatterns: includePatterns
        .split('\n')
        .map(p => p.trim())
        .filter(Boolean),
      excludePatterns: excludePatterns
        .split('\n')
        .map(p => p.trim())
        .filter(Boolean),
    });

    handleClose();
  };

  const handleFileSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file || !onSubmitFile) return;

    const name = fileName.trim() || file.name;

    await onSubmitFile(file, {
      name,
      description: fileDescription.trim(),
      tags: fileTags,
    });

    handleClose();
  };

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);

    const droppedFile = e.dataTransfer.files[0];
    if (droppedFile) {
      setFile(droppedFile);
      setFileName(droppedFile.name.replace(/\.[^.]+$/, ''));
    }
  }, []);

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFile = e.target.files?.[0];
    if (selectedFile) {
      setFile(selectedFile);
      setFileName(selectedFile.name.replace(/\.[^.]+$/, ''));
    }
  }, []);

  // Fetch remote page title when URL changes (debounced)
  useEffect(() => {
    // Skip if user manually edited the name
    if (userEditedName || !url) return;

    // Quick validation
    try {
      new URL(url);
    } catch {
      return;
    }

    // Debounce the fetch
    const timer = setTimeout(async () => {
      // Cancel any pending request
      previewAbortRef.current?.abort();
      previewAbortRef.current = new AbortController();

      setIsFetchingTitle(true);
      try {
        const preview = await api.sources.preview(url);
        // Only update if we got a good suggested name
        if (preview.suggested_name) {
          setUrlName(preview.suggested_name);
        }
      } catch {
        // Fallback to domain-based name
        try {
          const parsed = new URL(url);
          setUrlName(parsed.hostname);
        } catch {
          // Invalid URL
        }
      } finally {
        setIsFetchingTitle(false);
      }
    }, 500);

    return () => clearTimeout(timer);
  }, [url, userEditedName]);

  // Handle URL input change
  const handleUrlChange = useCallback((value: string) => {
    setUrl(value);
  }, []);

  // Handle name input change (marks as user-edited)
  const handleNameChange = useCallback((value: string) => {
    setUrlName(value);
    if (value) {
      setUserEditedName(true);
    }
  }, []);

  return (
    <Dialog.Root open={isOpen} onOpenChange={open => !open && handleClose()}>
      <AnimatePresence>
        {isOpen && (
          <Dialog.Portal forceMount>
            <Dialog.Overlay asChild>
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50"
              />
            </Dialog.Overlay>

            <Dialog.Content asChild>
              <motion.div
                initial={{ opacity: 0, scale: 0.95, y: 20 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.95, y: 20 }}
                transition={{ duration: 0.2, ease: 'easeOut' }}
                className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-xl max-h-[85vh] overflow-hidden bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-2xl shadow-2xl shadow-black/40"
              >
                {/* Header */}
                <div className="flex items-center justify-between p-5 border-b border-sc-fg-subtle/10">
                  <Dialog.Title className="text-lg font-semibold text-sc-fg-primary flex items-center gap-2">
                    <Plus width={20} height={20} className="text-sc-purple" />
                    Add Knowledge Source
                  </Dialog.Title>
                  <Dialog.Close asChild>
                    <button
                      type="button"
                      className="p-1.5 rounded-lg text-sc-fg-subtle hover:text-sc-fg-primary hover:bg-sc-bg-highlight transition-colors"
                    >
                      <X width={18} height={18} />
                    </button>
                  </Dialog.Close>
                </div>

                {/* Tabs */}
                <div className="flex border-b border-sc-fg-subtle/10">
                  <button
                    type="button"
                    onClick={() => setActiveTab('url')}
                    className={`flex-1 flex items-center justify-center gap-2 px-4 py-3 text-sm font-medium transition-colors relative ${
                      activeTab === 'url'
                        ? 'text-sc-purple'
                        : 'text-sc-fg-muted hover:text-sc-fg-primary'
                    }`}
                  >
                    <Globe width={16} height={16} />
                    Crawl Website
                    {activeTab === 'url' && (
                      <motion.div
                        layoutId="activeTab"
                        className="absolute bottom-0 left-0 right-0 h-0.5 bg-sc-purple"
                      />
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => setActiveTab('file')}
                    disabled={!onSubmitFile}
                    className={`flex-1 flex items-center justify-center gap-2 px-4 py-3 text-sm font-medium transition-colors relative ${
                      activeTab === 'file'
                        ? 'text-sc-cyan'
                        : 'text-sc-fg-muted hover:text-sc-fg-primary'
                    } ${!onSubmitFile ? 'opacity-50 cursor-not-allowed' : ''}`}
                  >
                    <Upload width={16} height={16} />
                    Upload Document
                    {activeTab === 'file' && (
                      <motion.div
                        layoutId="activeTab"
                        className="absolute bottom-0 left-0 right-0 h-0.5 bg-sc-cyan"
                      />
                    )}
                  </button>
                </div>

                {/* Content */}
                <div className="p-5 overflow-y-auto max-h-[calc(85vh-140px)]">
                  <AnimatePresence mode="wait">
                    {activeTab === 'url' ? (
                      <motion.form
                        key="url"
                        initial={{ opacity: 0, x: -20 }}
                        animate={{ opacity: 1, x: 0 }}
                        exit={{ opacity: 0, x: 20 }}
                        transition={{ duration: 0.15 }}
                        onSubmit={handleUrlSubmit}
                        className="space-y-4"
                      >
                        {/* URL Input */}
                        <div>
                          <label
                            htmlFor="source-url"
                            className="block text-sm font-medium text-sc-fg-muted mb-2"
                          >
                            Website URL <span className="text-sc-coral">*</span>
                          </label>
                          <input
                            id="source-url"
                            type="url"
                            value={url}
                            onChange={e => handleUrlChange(e.target.value)}
                            placeholder="https://docs.example.com"
                            required
                            className="w-full px-4 py-2.5 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-xl text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-1 focus:ring-sc-purple/30 transition-colors"
                          />
                        </div>

                        {/* Name */}
                        <div>
                          <label
                            htmlFor="source-name"
                            className="block text-sm font-medium text-sc-fg-muted mb-2"
                          >
                            Display Name
                            {isFetchingTitle && (
                              <span className="ml-2 text-sc-purple text-xs">
                                <Loader2
                                  width={12}
                                  height={12}
                                  className="inline animate-spin mr-1"
                                />
                                Fetching title...
                              </span>
                            )}
                          </label>
                          <input
                            id="source-name"
                            type="text"
                            value={urlName}
                            onChange={e => handleNameChange(e.target.value)}
                            placeholder={
                              isFetchingTitle
                                ? 'Fetching from website...'
                                : 'Auto-generated from page title'
                            }
                            className="w-full px-4 py-2.5 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-xl text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-1 focus:ring-sc-purple/30 transition-colors"
                          />
                        </div>

                        {/* Description */}
                        <div>
                          <label
                            htmlFor="source-description"
                            className="block text-sm font-medium text-sc-fg-muted mb-2"
                          >
                            Description
                          </label>
                          <textarea
                            id="source-description"
                            value={urlDescription}
                            onChange={e => setUrlDescription(e.target.value)}
                            placeholder="What is this documentation about?"
                            rows={2}
                            className="w-full px-4 py-2.5 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-xl text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-1 focus:ring-sc-purple/30 transition-colors resize-none"
                          />
                        </div>

                        {/* Crawl Depth */}
                        <div>
                          <label
                            htmlFor="crawl-depth"
                            className="block text-sm font-medium text-sc-fg-muted mb-2"
                          >
                            Crawl Depth:{' '}
                            <span className="text-sc-purple font-bold">{crawlDepth}</span>
                          </label>
                          <input
                            id="crawl-depth"
                            type="range"
                            min={1}
                            max={5}
                            value={crawlDepth}
                            onChange={e => setCrawlDepth(Number(e.target.value))}
                            className="w-full h-2 bg-sc-bg-dark rounded-lg appearance-none cursor-pointer accent-sc-purple"
                          />
                          <div className="flex justify-between text-xs text-sc-fg-subtle mt-1">
                            <span>1 (shallow)</span>
                            <span>5 (deep)</span>
                          </div>
                        </div>

                        {/* Tags */}
                        <div>
                          <label
                            htmlFor="source-tags"
                            className="block text-sm font-medium text-sc-fg-muted mb-2"
                          >
                            Tags
                          </label>
                          <div className="flex flex-wrap gap-1.5 mb-2">
                            {urlTags.map(tag => (
                              <span
                                key={tag}
                                className="inline-flex items-center gap-1 px-2.5 py-1 bg-sc-purple/15 text-sc-purple text-xs rounded-full border border-sc-purple/30"
                              >
                                {tag}
                                <button
                                  type="button"
                                  onClick={() => handleRemoveTag(tag)}
                                  className="hover:text-white transition-colors"
                                >
                                  <X width={12} height={12} />
                                </button>
                              </span>
                            ))}
                          </div>
                          <div className="flex gap-2">
                            <input
                              id="source-tags"
                              type="text"
                              value={tagInput}
                              onChange={e => setTagInput(e.target.value)}
                              onKeyDown={e => {
                                if (e.key === 'Enter') {
                                  e.preventDefault();
                                  handleAddTag(tagInput);
                                }
                              }}
                              placeholder="Add a tag..."
                              className="flex-1 px-3 py-2 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-lg text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none"
                            />
                          </div>
                          {/* Suggested tags */}
                          <div className="flex flex-wrap gap-1.5 mt-2">
                            {SUGGESTED_TAGS.filter(t => !urlTags.includes(t)).map(tag => (
                              <button
                                key={tag}
                                type="button"
                                onClick={() => handleAddTag(tag)}
                                className="px-2 py-0.5 text-xs text-sc-fg-subtle border border-sc-fg-subtle/20 rounded-full hover:border-sc-purple/30 hover:text-sc-purple transition-colors"
                              >
                                + {tag}
                              </button>
                            ))}
                          </div>
                        </div>

                        {/* Advanced Options Toggle */}
                        <button
                          type="button"
                          onClick={() => setShowAdvanced(!showAdvanced)}
                          className="text-sm text-sc-fg-subtle hover:text-sc-fg-muted transition-colors"
                        >
                          {showAdvanced ? '▼' : '▶'} Advanced Options
                        </button>

                        {/* Advanced Options */}
                        <AnimatePresence>
                          {showAdvanced && (
                            <motion.div
                              initial={{ height: 0, opacity: 0 }}
                              animate={{ height: 'auto', opacity: 1 }}
                              exit={{ height: 0, opacity: 0 }}
                              className="space-y-4 overflow-hidden"
                            >
                              <div>
                                <label
                                  htmlFor="include-patterns"
                                  className="block text-sm font-medium text-sc-fg-muted mb-2"
                                >
                                  Include Patterns (one per line, regex)
                                </label>
                                <textarea
                                  id="include-patterns"
                                  value={includePatterns}
                                  onChange={e => setIncludePatterns(e.target.value)}
                                  placeholder="/docs/.*&#10;/api/.*"
                                  rows={2}
                                  className="w-full px-3 py-2 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-lg text-sm font-mono text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none resize-none"
                                />
                              </div>
                              <div>
                                <label
                                  htmlFor="exclude-patterns"
                                  className="block text-sm font-medium text-sc-fg-muted mb-2"
                                >
                                  Exclude Patterns (one per line, regex)
                                </label>
                                <textarea
                                  id="exclude-patterns"
                                  value={excludePatterns}
                                  onChange={e => setExcludePatterns(e.target.value)}
                                  placeholder="/blog/.*&#10;/changelog/.*"
                                  rows={2}
                                  className="w-full px-3 py-2 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-lg text-sm font-mono text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none resize-none"
                                />
                              </div>
                            </motion.div>
                          )}
                        </AnimatePresence>

                        {/* Submit */}
                        <div className="flex justify-end gap-3 pt-2">
                          <button
                            type="button"
                            onClick={handleClose}
                            className="px-4 py-2 text-sm text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
                          >
                            Cancel
                          </button>
                          <button
                            type="submit"
                            disabled={!url.trim() || isSubmitting}
                            className="flex items-center gap-2 px-5 py-2.5 bg-sc-purple hover:bg-sc-purple/80 text-white rounded-xl font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed shadow-lg shadow-sc-purple/25"
                          >
                            {isSubmitting ? (
                              <>
                                <Loader2 width={16} height={16} className="animate-spin" />
                                Starting...
                              </>
                            ) : (
                              <>
                                <Globe width={16} height={16} />
                                Start Crawl
                              </>
                            )}
                          </button>
                        </div>
                      </motion.form>
                    ) : (
                      <motion.form
                        key="file"
                        initial={{ opacity: 0, x: 20 }}
                        animate={{ opacity: 1, x: 0 }}
                        exit={{ opacity: 0, x: -20 }}
                        transition={{ duration: 0.15 }}
                        onSubmit={handleFileSubmit}
                        className="space-y-4"
                      >
                        {/* File Drop Zone */}
                        <div
                          onDragOver={e => {
                            e.preventDefault();
                            setIsDragging(true);
                          }}
                          onDragLeave={() => setIsDragging(false)}
                          onDrop={handleDrop}
                          className={`relative border-2 border-dashed rounded-xl p-8 text-center transition-colors ${
                            isDragging
                              ? 'border-sc-cyan bg-sc-cyan/10'
                              : file
                                ? 'border-sc-green bg-sc-green/5'
                                : 'border-sc-fg-subtle/30 hover:border-sc-cyan/50'
                          }`}
                        >
                          <input
                            type="file"
                            onChange={handleFileSelect}
                            accept=".pdf,.doc,.docx,.txt,.md,.html,.htm"
                            className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
                          />
                          {file ? (
                            <div className="space-y-2">
                              <FileText width={40} height={40} className="mx-auto text-sc-green" />
                              <p className="text-sm font-medium text-sc-fg-primary">{file.name}</p>
                              <p className="text-xs text-sc-fg-subtle">
                                {(file.size / 1024).toFixed(1)} KB
                              </p>
                              <button
                                type="button"
                                onClick={() => setFile(null)}
                                className="text-xs text-sc-coral hover:underline"
                              >
                                Remove
                              </button>
                            </div>
                          ) : (
                            <div className="space-y-2">
                              <Upload
                                width={40}
                                height={40}
                                className="mx-auto text-sc-fg-subtle"
                              />
                              <p className="text-sm text-sc-fg-muted">
                                Drop a file here or click to browse
                              </p>
                              <p className="text-xs text-sc-fg-subtle">
                                PDF, DOC, DOCX, TXT, MD, HTML
                              </p>
                            </div>
                          )}
                        </div>

                        {/* Name */}
                        <div>
                          <label
                            htmlFor="file-name"
                            className="block text-sm font-medium text-sc-fg-muted mb-2"
                          >
                            Display Name
                          </label>
                          <input
                            id="file-name"
                            type="text"
                            value={fileName}
                            onChange={e => setFileName(e.target.value)}
                            placeholder="Auto-generated from filename"
                            className="w-full px-4 py-2.5 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-xl text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-cyan focus:outline-none focus:ring-1 focus:ring-sc-cyan/30 transition-colors"
                          />
                        </div>

                        {/* Description */}
                        <div>
                          <label
                            htmlFor="file-description"
                            className="block text-sm font-medium text-sc-fg-muted mb-2"
                          >
                            Description
                          </label>
                          <textarea
                            id="file-description"
                            value={fileDescription}
                            onChange={e => setFileDescription(e.target.value)}
                            placeholder="What is this document about?"
                            rows={2}
                            className="w-full px-4 py-2.5 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-xl text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-cyan focus:outline-none focus:ring-1 focus:ring-sc-cyan/30 transition-colors resize-none"
                          />
                        </div>

                        {/* Tags */}
                        <div>
                          <label
                            htmlFor="file-tags"
                            className="block text-sm font-medium text-sc-fg-muted mb-2"
                          >
                            Tags
                          </label>
                          <div className="flex flex-wrap gap-1.5 mb-2">
                            {fileTags.map(tag => (
                              <span
                                key={tag}
                                className="inline-flex items-center gap-1 px-2.5 py-1 bg-sc-cyan/15 text-sc-cyan text-xs rounded-full border border-sc-cyan/30"
                              >
                                {tag}
                                <button
                                  type="button"
                                  onClick={() => handleRemoveTag(tag, true)}
                                  className="hover:text-white transition-colors"
                                >
                                  <X width={12} height={12} />
                                </button>
                              </span>
                            ))}
                          </div>
                          <input
                            id="file-tags"
                            type="text"
                            placeholder="Add a tag and press Enter..."
                            onKeyDown={e => {
                              if (e.key === 'Enter') {
                                e.preventDefault();
                                handleAddTag(e.currentTarget.value, true);
                                e.currentTarget.value = '';
                              }
                            }}
                            className="w-full px-3 py-2 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-lg text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-cyan focus:outline-none"
                          />
                        </div>

                        {/* Submit */}
                        <div className="flex justify-end gap-3 pt-2">
                          <button
                            type="button"
                            onClick={handleClose}
                            className="px-4 py-2 text-sm text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
                          >
                            Cancel
                          </button>
                          <button
                            type="submit"
                            disabled={!file || isSubmitting}
                            className="flex items-center gap-2 px-5 py-2.5 bg-sc-cyan hover:bg-sc-cyan/80 text-sc-bg-dark rounded-xl font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed shadow-lg shadow-sc-cyan/25"
                          >
                            {isSubmitting ? (
                              <>
                                <Loader2 width={16} height={16} className="animate-spin" />
                                Uploading...
                              </>
                            ) : (
                              <>
                                <Upload width={16} height={16} />
                                Upload Document
                              </>
                            )}
                          </button>
                        </div>
                      </motion.form>
                    )}
                  </AnimatePresence>
                </div>
              </motion.div>
            </Dialog.Content>
          </Dialog.Portal>
        )}
      </AnimatePresence>
    </Dialog.Root>
  );
}
