import {
  Book,
  Circle,
  Code,
  CodeBrackets,
  Combine,
  Cube,
  EditPencil,
  Flare,
  Flash,
  Folder,
  Globe,
  Group,
  Hashtag,
  Journal,
  Label,
  List,
  MultiplePages,
  Page,
  Settings,
  Star,
  Terminal,
  WarningTriangle,
} from 'iconoir-react';
import type { ComponentType, SVGProps } from 'react';
import type { EntityType } from '@/lib/constants';

type IconComponent = ComponentType<SVGProps<SVGSVGElement>>;

// Map entity types to Iconoir icons
const ENTITY_ICON_MAP: Record<EntityType, IconComponent> = {
  pattern: Combine,
  rule: Flash,
  template: EditPencil,
  convention: Label,
  tool: Settings,
  language: Code,
  topic: Hashtag,
  episode: Flare,
  knowledge_source: Book,
  config_file: Settings,
  slash_command: Terminal,
  task: List,
  project: Folder,
  team: Group,
  epic: MultiplePages,
  error_pattern: WarningTriangle,
  milestone: Star,
  source: Globe,
  document: Journal,
  concept: Circle,
  file: Page,
  function: CodeBrackets,
};

// Fallback icon for unknown types
const DEFAULT_ICON = Cube;

interface EntityIconProps {
  type: string;
  size?: number;
  className?: string;
}

export function EntityIcon({ type, size = 14, className = '' }: EntityIconProps) {
  const Icon = ENTITY_ICON_MAP[type as EntityType] ?? DEFAULT_ICON;
  return <Icon width={size} height={size} className={className} />;
}

// Export the map for direct access if needed
export { ENTITY_ICON_MAP };
