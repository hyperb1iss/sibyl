import type { BaseMetadata } from './shared';
import type { SourceMetadata } from './sources';
import type { EpicMetadata, ProjectMetadata, TaskMetadata } from './work-items';

export type EntityMetadataMap = {
  task: TaskMetadata;
  source: SourceMetadata;
  project: ProjectMetadata;
  epic: EpicMetadata;
  // Generic entities use base metadata
  pattern: BaseMetadata;
  procedure: BaseMetadata;
  episode: BaseMetadata;
  rule: BaseMetadata;
  template: BaseMetadata;
  tool: BaseMetadata;
  topic: BaseMetadata;
  document: BaseMetadata;
};
