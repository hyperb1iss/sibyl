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
  ClipboardCheck,
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
  FastArrowRight,
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
  Heart,
  HelpCircle,
  InfoCircle,
  KanbanBoard,
  Key,
  KeyCommand,
  Label,
  LightBulb,
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
  Square,
  Star,
  Timer,
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
  Activity,
  Archive,
  ArrowLeft,
  ArrowRight,
  Book as BookOpen,
  Book,
  Calendar,
  Check,
  CheckCircle as CircleCheck,
  CheckCircle as CheckCircle2,
  CheckCircle,
  Circle,
  Circle as CircleDot,
  Circle as Target,
  ClipboardCheck,
  Clock,
  Code,
  Collapse as Minimize2,
  Combine as Layers,
  Combine,
  Copy,
  Cube as Boxes,
  Cube,
  Dashboard as LayoutDashboard,
  Dashboard,
  Database,
  Download,
  Edit,
  EditPencil as Pencil,
  EditPencil,
  Expand as Maximize2,
  Eye,
  FastArrowRight,
  Filter,
  FireFlame as Flame,
  Flash as Zap,
  // Direct exports (same name in both)
  Flash,
  Folder,
  GitBranch,
  Github,
  GitPullRequest,
  Globe,
  GraphUp as BarChart3,
  GraphUp as TrendingUp,
  Group as Users,
  Group,
  Hashtag as Hash,
  Heart,
  HelpCircle,
  InfoCircle,
  KanbanBoard as FolderKanban,
  KanbanBoard,
  // Setup wizard icons
  Key,
  KeyCommand as Command,
  Label as Tag,
  Label,
  LightBulb,
  Link,
  List as ListTodo,
  List as LayoutList,
  List,
  MenuIcon as Menu,
  MinusCircle,
  MoreHoriz as MoreHorizontal,
  MoreHoriz,
  MoreVert as MoreVertical,
  MoreVert,
  NavArrowDown as ChevronDown,
  NavArrowDown as SortDesc,
  NavArrowDown,
  NavArrowRight as ChevronRight,
  NavArrowRight,
  NavArrowUp as ChevronUp,
  NavArrowUp as SortAsc,
  NavArrowUp,
  Network,
  OpenNewWindow as ExternalLink,
  OpenNewWindow,
  Page as FileText,
  Page,
  Pause as CirclePause,
  Pause,
  Play,
  Plus,
  PlusCircle,
  RefreshDouble as RefreshCw,
  RefreshDouble as Loader2,
  RefreshDouble,
  Restart,
  Search,
  Send,
  Settings,
  Sort as ArrowUpDown,
  Sort,
  SortDown as ArrowDownAZ,
  SortDown,
  SortUp,
  Square,
  Star,
  // Orchestration icons
  Timer,
  Trash as Trash2,
  Trash,
  Undo as RotateCcw,
  Undo,
  Upload,
  User,
  ViewGrid as Grid3X3,
  ViewGrid,
  WarningCircle as AlertCircle,
  WarningCircle,
  WarningTriangle as AlertTriangle,
  WarningTriangle,
  Wifi,
  WifiOff,
  Xmark as X,
  Xmark,
  XmarkCircle as CircleX,
  XmarkCircle as StopCircle,
  XmarkCircle,
  // Zoom/View controls
  ZoomIn,
  ZoomIn as Focus, // Using ZoomIn for focus/fit-to-view
  ZoomOut,
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
