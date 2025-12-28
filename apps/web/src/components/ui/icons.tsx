/**
 * SilkCircuit Icon System
 *
 * Centralized icon exports with consistent styling.
 * Using Iconoir icons with electric neon aesthetic.
 */

import {
  Activity,
  Archive,
  ArrowLeft,
  ArrowRight,
  Book,
  Calendar,
  Check,
  CheckCircle,
  Circle,
  Clock,
  Code,
  Collapse,
  Combine,
  Copy,
  Cube,
  Dashboard,
  Database,
  Download,
  Edit,
  EditPencil,
  Expand,
  Eye,
  Filter,
  FireFlame,
  Flash,
  Folder,
  GitBranch,
  Github,
  GitPullRequest,
  Globe,
  GraphUp,
  Group,
  Hashtag,
  InfoCircle,
  KanbanBoard,
  KeyCommand,
  Label,
  Link,
  List,
  Menu as MenuIcon,
  MinusCircle,
  MoreHoriz,
  MoreVert,
  NavArrowDown,
  NavArrowRight,
  NavArrowUp,
  Network,
  OpenNewWindow,
  Page,
  Pause,
  Play,
  Plus,
  PlusCircle,
  RefreshDouble,
  Restart,
  Search,
  Send,
  Settings,
  Sort,
  SortDown,
  SortUp,
  Sparks,
  Square,
  Star,
  Trash,
  Undo,
  Upload,
  User,
  ViewGrid,
  WarningCircle,
  WarningTriangle,
  Wifi,
  WifiOff,
  Xmark,
  XmarkCircle,
  ZoomIn,
  ZoomOut,
} from 'iconoir-react';
import type { ComponentType, SVGProps } from 'react';

// Icon component type for Iconoir
export type IconComponent = ComponentType<SVGProps<SVGSVGElement>>;

// Re-export all icons with consistent naming
export {
  Search,
  KeyCommand as Command,
  Dashboard as LayoutDashboard,
  KanbanBoard as FolderKanban,
  List as ListTodo,
  Book as BookOpen,
  Network,
  Cube as Boxes,
  RefreshDouble as RefreshCw,
  Plus,
  NavArrowRight as ChevronRight,
  NavArrowDown as ChevronDown,
  NavArrowUp as ChevronUp,
  Check,
  Xmark as X,
  WarningCircle as AlertCircle,
  RefreshDouble as Loader2,
  Flash as Zap,
  Circle,
  Circle as CircleDot,
  CheckCircle as CircleCheck,
  Pause as CirclePause,
  XmarkCircle as CircleX,
  Clock,
  Archive,
  Sparks as Sparkles,
  Activity,
  Wifi,
  WifiOff,
  Settings,
  OpenNewWindow as ExternalLink,
  Github,
  Globe,
  Page as FileText,
  Folder,
  ArrowLeft,
  ArrowRight,
  MoreHoriz as MoreHorizontal,
  Edit,
  Trash as Trash2,
  Eye,
  Link,
  Copy,
  Download,
  Upload,
  Filter,
  NavArrowUp as SortAsc,
  NavArrowDown as SortDesc,
  Calendar,
  User,
  Group as Users,
  Label as Tag,
  Hashtag as Hash,
  Star,
  FireFlame as Flame,
  GraphUp as BarChart3,
  GraphUp as TrendingUp,
  InfoCircle,
  MenuIcon as Menu,
  Sort as ArrowUpDown,
  SortDown as ArrowDownAZ,
  WarningTriangle as AlertTriangle,
  Database,
  GitBranch,
  Play,
  Combine as Layers,
  CheckCircle as CheckCircle2,
  Circle as Target,
  MoreVert as MoreVertical,
  XmarkCircle as StopCircle,
  Undo as RotateCcw,
  EditPencil as Pencil,
  Send,
  GitPullRequest,
  ViewGrid as Grid3X3,
  List as LayoutList,
  Restart,
  MoreVert,
  Undo,
  EditPencil,
  ViewGrid,
  // Zoom/View controls
  ZoomIn,
  ZoomOut,
  Expand as Maximize2,
  Collapse as Minimize2,
  PlusCircle,
  MinusCircle,
  ZoomIn as Focus, // Using ZoomIn for focus/fit-to-view
  // Direct exports (same name in both)
  Sparks,
  Flash,
  Pause,
  Xmark,
  Cube,
  Code,
  Book,
  List,
  Page,
  KanbanBoard,
  Dashboard,
  RefreshDouble,
  NavArrowDown,
  NavArrowRight,
  NavArrowUp,
  MoreHoriz,
  OpenNewWindow,
  WarningCircle,
  WarningTriangle,
  XmarkCircle,
  CheckCircle,
  Trash,
  Label,
  Group,
  Sort,
  SortDown,
  SortUp,
  Square,
  Combine,
};

// =============================================================================
// Navigation Icons
// =============================================================================

export const NAV_ICONS = {
  dashboard: Dashboard,
  projects: KanbanBoard,
  tasks: List,
  sources: Book,
  graph: Network,
  entities: Cube,
  search: Search,
} as const;

// =============================================================================
// Status Icons with SilkCircuit colors
// =============================================================================

export const STATUS_ICONS = {
  backlog: Circle,
  todo: Circle,
  doing: RefreshDouble,
  blocked: Pause,
  review: Clock,
  done: CheckCircle,
  archived: Archive,
} as const;

export const PRIORITY_ICONS = {
  critical: FireFlame,
  high: Flash,
  medium: Star,
  low: Circle,
  someday: Clock,
} as const;

// =============================================================================
// Icon wrapper with consistent sizing
// =============================================================================

interface IconProps {
  icon: IconComponent;
  size?: 'xs' | 'sm' | 'md' | 'lg';
  className?: string;
}

const ICON_SIZES = {
  xs: 12,
  sm: 14,
  md: 16,
  lg: 20,
} as const;

export function Icon({ icon: IconComponent, size = 'md', className = '' }: IconProps) {
  return <IconComponent width={ICON_SIZES[size]} height={ICON_SIZES[size]} className={className} />;
}

// =============================================================================
// Animated status indicator
// =============================================================================

interface StatusIndicatorProps {
  status: 'connected' | 'connecting' | 'disconnected';
  showLabel?: boolean;
}

export function StatusIndicator({ status, showLabel = true }: StatusIndicatorProps) {
  const config = {
    connected: {
      icon: Wifi,
      label: 'Live',
      className: 'text-sc-green',
      glow: 'shadow-[0_0_8px_rgba(80,250,123,0.8)]',
      bg: 'bg-sc-green/5 border-sc-green/20',
    },
    connecting: {
      icon: RefreshDouble,
      label: 'Syncing',
      className: 'text-sc-yellow animate-spin',
      glow: '',
      bg: 'bg-sc-yellow/5 border-sc-yellow/20',
    },
    disconnected: {
      icon: WifiOff,
      label: 'Offline',
      className: 'text-sc-red',
      glow: 'shadow-[0_0_8px_rgba(255,99,99,0.6)]',
      bg: 'bg-sc-red/5 border-sc-red/20',
    },
  };

  const { icon: IconComp, label, className, bg } = config[status];

  return (
    <div
      className={`
        flex items-center gap-2 px-3 py-1.5 rounded-full
        text-xs font-medium tracking-wide uppercase
        border transition-all duration-500
        ${bg} ${className}
      `}
    >
      <IconComp width={14} height={14} />
      {showLabel && <span className="hidden sm:inline">{label}</span>}
    </div>
  );
}
