import {
  Book,
  Code,
  Combine,
  Cube,
  EditPencil,
  Flash,
  Folder,
  Globe,
  Hashtag,
  Journal,
  LightBulb,
  List,
  Settings,
  Flare,
  Terminal,
} from 'iconoir-react';
import type { ComponentType, SVGProps } from 'react';
import type { EntityType } from '@/lib/constants';

type IconComponent = ComponentType<SVGProps<SVGSVGElement>>;

// Map entity types to Iconoir icons
const ENTITY_ICON_MAP: Record<EntityType, IconComponent> = {
  pattern: Combine,
  rule: Flash,
  template: EditPencil,
  tool: Settings,
  language: Code,
  topic: Hashtag,
  episode: Flare,
  knowledge_source: Book,
  config_file: Settings,
  slash_command: Terminal,
  task: List,
  project: Folder,
  source: Globe,
  document: Journal,
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
