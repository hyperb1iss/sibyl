'use client';

import { ArrowRight, Brain, CheckSquare, Search, Sparkles } from 'lucide-react';

interface WelcomeStepProps {
  onNext: () => void;
  onSkip: () => void;
}

export function WelcomeStep({ onNext, onSkip }: WelcomeStepProps) {
  return (
    <div className="p-5">
      {/* Hero */}
      <div className="text-center mb-6">
        <div className="relative inline-flex items-center justify-center mb-4">
          <div className="absolute w-20 h-20 rounded-full bg-sc-purple/10 animate-pulse" />
          <div className="absolute w-16 h-16 rounded-full bg-sc-purple/15 animate-pulse delay-75" />
          <div className="relative inline-flex items-center justify-center w-14 h-14 rounded-full bg-gradient-to-br from-sc-purple to-sc-coral text-white">
            <Sparkles className="w-7 h-7" />
          </div>
        </div>
        <h1 className="text-2xl font-bold text-sc-fg-primary mb-2">
          Welcome to <span className="text-sc-purple">Sibyl</span>
        </h1>
        <p className="text-sc-fg-muted">Your AI-powered knowledge graph for dev teams</p>
      </div>

      {/* Feature highlights */}
      <div className="grid grid-cols-3 gap-3 mb-6">
        <FeatureCard
          icon={<Brain className="w-5 h-5" />}
          title="Knowledge Graph"
          description="Connect patterns and learnings"
          accent="purple"
        />
        <FeatureCard
          icon={<CheckSquare className="w-5 h-5" />}
          title="Task Tracking"
          description="Smart workflow management"
          accent="cyan"
        />
        <FeatureCard
          icon={<Search className="w-5 h-5" />}
          title="Semantic Search"
          description="Find by meaning, not keywords"
          accent="green"
        />
      </div>

      {/* Actions */}
      <div className="flex items-center justify-between pt-4 border-t border-sc-fg-subtle/10">
        <button
          type="button"
          onClick={onSkip}
          className="text-sc-fg-subtle hover:text-sc-fg-muted transition-colors text-sm"
        >
          Skip for now
        </button>
        <button
          type="button"
          onClick={onNext}
          className="flex items-center gap-2 px-5 py-2.5 bg-sc-purple hover:bg-sc-purple/80 text-white rounded-xl font-medium transition-colors shadow-lg shadow-sc-purple/25"
        >
          Get Started
          <ArrowRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}

function FeatureCard({
  icon,
  title,
  description,
  accent = 'purple',
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  accent?: 'purple' | 'cyan' | 'green';
}) {
  const colors = {
    purple: {
      card: 'border-sc-purple/20 hover:border-sc-purple/40',
      icon: 'bg-sc-purple/15 text-sc-purple',
    },
    cyan: {
      card: 'border-sc-cyan/20 hover:border-sc-cyan/40',
      icon: 'bg-sc-cyan/15 text-sc-cyan',
    },
    green: {
      card: 'border-sc-green/20 hover:border-sc-green/40',
      icon: 'bg-sc-green/15 text-sc-green',
    },
  };

  return (
    <div
      className={`p-3 rounded-xl bg-sc-bg-dark border transition-all duration-200 text-center ${colors[accent].card}`}
    >
      <div
        className={`inline-flex items-center justify-center w-10 h-10 rounded-full mb-2 ${colors[accent].icon}`}
      >
        {icon}
      </div>
      <h3 className="font-medium text-sc-fg-primary text-sm mb-0.5">{title}</h3>
      <p className="text-sc-fg-subtle text-xs">{description}</p>
    </div>
  );
}
