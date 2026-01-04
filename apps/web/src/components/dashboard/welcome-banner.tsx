'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { ConnectClaudeModal } from '@/components/dashboard/connect-claude-modal';
import {
  ArrowRight,
  BookOpen,
  Network,
  Search,
  Sparks,
  Sparkles,
  Xmark,
} from '@/components/ui/icons';
import { useSetupStatus } from '@/lib/hooks';

/** Minimum entities before automatically hiding the welcome banner */
const WELCOME_BANNER_ENTITY_THRESHOLD = 10;

/** localStorage key for banner dismissal state */
const BANNER_DISMISSED_KEY = 'sibyl:welcome-banner-dismissed';

interface WelcomeBannerProps {
  totalEntities: number;
  onDismiss?: () => void;
}

export function WelcomeBanner({ totalEntities, onDismiss }: WelcomeBannerProps) {
  const [isDismissed, setIsDismissed] = useState(false);
  const [showConnectModal, setShowConnectModal] = useState(false);
  const { data: setupStatus } = useSetupStatus({
    validateKeys: false,
    enabled: totalEntities === 0,
  });

  // Load dismissal state from localStorage on mount
  useEffect(() => {
    const dismissed = localStorage.getItem(BANNER_DISMISSED_KEY);
    if (dismissed === 'true') {
      setIsDismissed(true);
    }
  }, []);

  // Don't show if dismissed or user has sufficient entities
  if (isDismissed || totalEntities > WELCOME_BANNER_ENTITY_THRESHOLD) {
    return null;
  }

  const handleDismiss = () => {
    setIsDismissed(true);
    localStorage.setItem(BANNER_DISMISSED_KEY, 'true');
    onDismiss?.();
  };

  const isNewUser = totalEntities === 0;
  const openaiReady = setupStatus?.openai_valid === true;
  const anthropicReady = setupStatus?.anthropic_valid === true;
  const apisReady = openaiReady && anthropicReady;

  return (
    <div className="relative bg-gradient-to-r from-sc-purple/10 via-sc-cyan/5 to-sc-coral/10 border border-sc-purple/20 rounded-xl sm:rounded-2xl p-4 sm:p-6 mb-4 sm:mb-6 animate-fade-in overflow-hidden">
      {/* Background glow effect */}
      <div className="absolute -top-20 -right-20 w-40 h-40 bg-sc-purple/10 rounded-full blur-3xl" />
      <div className="absolute -bottom-10 -left-10 w-32 h-32 bg-sc-cyan/10 rounded-full blur-2xl" />

      {/* Dismiss button */}
      <button
        type="button"
        onClick={handleDismiss}
        className="absolute top-3 right-3 p-1.5 rounded-lg text-sc-fg-subtle hover:text-sc-fg-primary hover:bg-sc-bg-highlight/50 transition-colors"
        aria-label="Dismiss welcome banner"
      >
        <Xmark width={16} height={16} />
      </button>

      <div className="relative">
        {/* Header */}
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 sm:w-12 sm:h-12 rounded-xl bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral flex items-center justify-center shadow-lg shadow-sc-purple/30">
            <Sparkles width={20} height={20} className="text-white sm:w-6 sm:h-6" />
          </div>
          <div>
            <h2 className="text-lg sm:text-xl font-bold text-sc-fg-primary">
              {isNewUser ? 'Welcome to Sibyl!' : 'Getting Started'}
            </h2>
            <p className="text-xs sm:text-sm text-sc-fg-muted">
              {isNewUser
                ? "Your knowledge graph is ready. Let's add some knowledge."
                : `You have ${totalEntities} entities. Keep building!`}
            </p>
          </div>
        </div>

        {/* Getting Started Steps */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-4 mb-4">
          {/* Step 1: Connect Claude Code */}
          <div className="bg-sc-bg-base/60 backdrop-blur-sm rounded-lg p-3 sm:p-4 border border-sc-fg-subtle/10">
            <div className="flex items-center gap-2 mb-2">
              <div className="w-6 h-6 rounded-full bg-sc-purple/20 flex items-center justify-center text-xs font-bold text-sc-purple">
                1
              </div>
              <span className="text-sm font-medium text-sc-fg-primary">Connect Claude Code</span>
            </div>
            <p className="text-xs text-sc-fg-muted mb-3">
              Add Sibyl as an MCP server to enable AI-powered knowledge management.
            </p>
            <button
              type="button"
              onClick={() => setShowConnectModal(true)}
              className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-sc-purple/10 border border-sc-purple/20 text-xs font-medium text-sc-purple hover:bg-sc-purple/20 transition-colors"
            >
              <Sparks width={14} height={14} />
              Connect
            </button>
          </div>

          {/* Step 2: Add a Source */}
          <div className="bg-sc-bg-base/60 backdrop-blur-sm rounded-lg p-3 sm:p-4 border border-sc-fg-subtle/10">
            <div className="flex items-center gap-2 mb-2">
              <div className="w-6 h-6 rounded-full bg-sc-cyan/20 flex items-center justify-center text-xs font-bold text-sc-cyan">
                2
              </div>
              <span className="text-sm font-medium text-sc-fg-primary">Add Documentation</span>
            </div>
            <p className="text-xs text-sc-fg-muted mb-3">
              Import docs, wikis, or websites to build your knowledge base.
            </p>
            <Link
              href="/sources"
              className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-sc-cyan/10 border border-sc-cyan/20 text-xs font-medium text-sc-cyan hover:bg-sc-cyan/20 transition-colors"
            >
              <BookOpen width={14} height={14} />
              Add Source
            </Link>
          </div>

          {/* Step 3: Search & Explore */}
          <div className="bg-sc-bg-base/60 backdrop-blur-sm rounded-lg p-3 sm:p-4 border border-sc-fg-subtle/10">
            <div className="flex items-center gap-2 mb-2">
              <div className="w-6 h-6 rounded-full bg-sc-coral/20 flex items-center justify-center text-xs font-bold text-sc-coral">
                3
              </div>
              <span className="text-sm font-medium text-sc-fg-primary">Search & Explore</span>
            </div>
            <p className="text-xs text-sc-fg-muted mb-3">
              Semantic search finds answers across your entire knowledge base.
            </p>
            <Link
              href="/search"
              className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-sc-coral/10 border border-sc-coral/20 text-xs font-medium text-sc-coral hover:bg-sc-coral/20 transition-colors"
            >
              <Search width={14} height={14} />
              Try Search
            </Link>
          </div>
        </div>

        {/* Status indicators */}
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <div className="flex items-center gap-1.5">
            <div
              className={`w-2 h-2 rounded-full ${apisReady ? 'bg-sc-green shadow-[0_0_6px_rgba(80,250,123,0.6)]' : 'bg-sc-fg-subtle'}`}
            />
            <span className="text-sc-fg-muted">
              {apisReady ? 'API keys configured' : 'API keys need setup'}
            </span>
          </div>
          <Link
            href="/graph"
            className="flex items-center gap-1.5 text-sc-purple hover:text-sc-purple/80 transition-colors"
          >
            <Network width={12} height={12} />
            <span>View Graph</span>
            <ArrowRight width={12} height={12} />
          </Link>
        </div>
      </div>

      {/* Claude Code Connection Modal */}
      <ConnectClaudeModal open={showConnectModal} onOpenChange={setShowConnectModal} />
    </div>
  );
}
