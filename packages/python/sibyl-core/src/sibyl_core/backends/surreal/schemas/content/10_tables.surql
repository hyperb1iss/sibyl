DEFINE TABLE IF NOT EXISTS crawl_sources SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON crawl_sources TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON crawl_sources TYPE string;
DEFINE FIELD IF NOT EXISTS name ON crawl_sources TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS url ON crawl_sources TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS source_type ON crawl_sources TYPE string DEFAULT 'website';
DEFINE FIELD IF NOT EXISTS description ON crawl_sources TYPE option<string>;
DEFINE FIELD IF NOT EXISTS crawl_depth ON crawl_sources TYPE int DEFAULT 2;
DEFINE FIELD IF NOT EXISTS include_patterns ON crawl_sources TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS exclude_patterns ON crawl_sources TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS respect_robots ON crawl_sources TYPE bool DEFAULT true;
DEFINE FIELD IF NOT EXISTS crawl_status ON crawl_sources TYPE string DEFAULT 'pending';
DEFINE FIELD IF NOT EXISTS current_job_id ON crawl_sources TYPE option<string>;
DEFINE FIELD IF NOT EXISTS last_crawled_at ON crawl_sources TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS last_error ON crawl_sources TYPE option<string>;
DEFINE FIELD IF NOT EXISTS document_count ON crawl_sources TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS chunk_count ON crawl_sources TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS total_tokens ON crawl_sources TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS tags ON crawl_sources TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS categories ON crawl_sources TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS favicon_url ON crawl_sources TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON crawl_sources TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON crawl_sources TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_crawl_sources_uuid ON crawl_sources FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_crawl_sources_org ON crawl_sources FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_crawl_sources_org_url ON crawl_sources FIELDS organization_id, url UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_crawl_sources_status ON crawl_sources FIELDS crawl_status;
DEFINE INDEX IF NOT EXISTS idx_crawl_sources_name_ft ON crawl_sources FIELDS name FULLTEXT ANALYZER title_analyzer BM25;

DEFINE TABLE IF NOT EXISTS crawled_documents SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON crawled_documents TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON crawled_documents TYPE option<string> DEFAULT '';
DEFINE FIELD IF NOT EXISTS source_id ON crawled_documents TYPE string;
DEFINE FIELD IF NOT EXISTS url ON crawled_documents TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS title ON crawled_documents TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS raw_content ON crawled_documents TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS content ON crawled_documents TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS content_hash ON crawled_documents TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS parent_url ON crawled_documents TYPE option<string>;
DEFINE FIELD IF NOT EXISTS section_path ON crawled_documents TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS depth ON crawled_documents TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS language ON crawled_documents TYPE option<string>;
DEFINE FIELD IF NOT EXISTS word_count ON crawled_documents TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS token_count ON crawled_documents TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS has_code ON crawled_documents TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS is_index ON crawled_documents TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS headings ON crawled_documents TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS links ON crawled_documents TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS code_languages ON crawled_documents TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS crawled_at ON crawled_documents TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS http_status ON crawled_documents TYPE option<int>;
DEFINE FIELD IF NOT EXISTS created_at ON crawled_documents TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON crawled_documents TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_crawled_documents_uuid ON crawled_documents FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_org_source ON crawled_documents
    FIELDS organization_id, source_id;
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_source ON crawled_documents FIELDS source_id;
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_source_url ON crawled_documents FIELDS source_id, url UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_crawled_at ON crawled_documents FIELDS crawled_at;
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_title_ft ON crawled_documents FIELDS title FULLTEXT ANALYZER title_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_content_ft ON crawled_documents FIELDS content FULLTEXT ANALYZER content_analyzer BM25;

DEFINE TABLE IF NOT EXISTS document_chunks SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON document_chunks TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON document_chunks TYPE option<string> DEFAULT '';
DEFINE FIELD IF NOT EXISTS source_id ON document_chunks TYPE option<string> DEFAULT '';
DEFINE FIELD IF NOT EXISTS document_id ON document_chunks TYPE string;
DEFINE FIELD IF NOT EXISTS chunk_index ON document_chunks TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS chunk_type ON document_chunks TYPE string DEFAULT 'text';
DEFINE FIELD IF NOT EXISTS content ON document_chunks TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS context ON document_chunks TYPE option<string>;
DEFINE FIELD IF NOT EXISTS token_count ON document_chunks TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS start_char ON document_chunks TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS end_char ON document_chunks TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS heading_path ON document_chunks TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS embedding ON document_chunks TYPE option<array<float, {EMBEDDING_DIM}>>;
DEFINE FIELD IF NOT EXISTS language ON document_chunks TYPE option<string>;
DEFINE FIELD IF NOT EXISTS is_complete ON document_chunks TYPE bool DEFAULT true;
DEFINE FIELD IF NOT EXISTS has_entities ON document_chunks TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS entity_ids ON document_chunks TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS created_at ON document_chunks TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON document_chunks TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_document_chunks_uuid ON document_chunks FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_org_source ON document_chunks
    FIELDS organization_id, source_id;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_org_source_entities ON document_chunks
    FIELDS organization_id, source_id, has_entities;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_org_document ON document_chunks
    FIELDS organization_id, document_id;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_document ON document_chunks FIELDS document_id;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_document_position ON document_chunks
    FIELDS document_id, chunk_index UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_content_ft ON document_chunks FIELDS content FULLTEXT ANALYZER content_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_embedding ON document_chunks FIELDS embedding
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32 EFC 150 M 12;

DEFINE TABLE IF NOT EXISTS raw_captures SCHEMAFULL CHANGEFEED 7d;
DEFINE FIELD IF NOT EXISTS uuid ON raw_captures TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON raw_captures TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON raw_captures TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS principal_id ON raw_captures TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS memory_scope ON raw_captures TYPE string DEFAULT 'private';
DEFINE FIELD IF NOT EXISTS scope_key ON raw_captures TYPE option<string>;
DEFINE FIELD IF NOT EXISTS agent_id ON raw_captures TYPE option<string>;
DEFINE FIELD IF NOT EXISTS project_id ON raw_captures TYPE option<string>;
DEFINE FIELD IF NOT EXISTS review_state ON raw_captures TYPE string DEFAULT 'pending';
DEFINE FIELD IF NOT EXISTS entity_id ON raw_captures TYPE option<string>;
DEFINE FIELD IF NOT EXISTS title ON raw_captures TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS raw_content ON raw_captures TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS entity_type ON raw_captures TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS tags ON raw_captures TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS embedding ON raw_captures TYPE option<array<float, {EMBEDDING_DIM}>>;
DEFINE FIELD IF NOT EXISTS metadata ON raw_captures TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS provenance ON raw_captures TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS capture_surface ON raw_captures TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_by_user_id ON raw_captures TYPE option<string>;
DEFINE FIELD IF NOT EXISTS captured_at ON raw_captures TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS deleted_at ON raw_captures TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS purge_after ON raw_captures TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS created_at ON raw_captures TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_raw_captures_uuid ON raw_captures FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_org ON raw_captures FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_source ON raw_captures FIELDS source_id;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_principal ON raw_captures FIELDS principal_id;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_scope ON raw_captures FIELDS organization_id, memory_scope, scope_key;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_agent ON raw_captures FIELDS organization_id, agent_id;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_project ON raw_captures FIELDS organization_id, project_id;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_review ON raw_captures FIELDS organization_id, review_state;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_purge_after ON raw_captures FIELDS purge_after;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_entity_id ON raw_captures FIELDS entity_id;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_org_dedupe ON raw_captures FIELDS organization_id, metadata.dedupe_key;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_title_ft ON raw_captures FIELDS title FULLTEXT ANALYZER title_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_content_ft ON raw_captures FIELDS raw_content FULLTEXT ANALYZER content_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_embedding ON raw_captures FIELDS embedding
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32 EFC 150 M 12;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_captured_at ON raw_captures FIELDS captured_at;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_created_at ON raw_captures FIELDS created_at;

DEFINE TABLE IF NOT EXISTS api_idempotency_records SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON api_idempotency_records TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON api_idempotency_records TYPE string;
DEFINE FIELD IF NOT EXISTS principal_id ON api_idempotency_records TYPE string;
DEFINE FIELD IF NOT EXISTS idempotency_key ON api_idempotency_records TYPE string;
DEFINE FIELD IF NOT EXISTS method ON api_idempotency_records TYPE string;
DEFINE FIELD IF NOT EXISTS path ON api_idempotency_records TYPE string;
DEFINE FIELD IF NOT EXISTS request_hash ON api_idempotency_records TYPE string;
DEFINE FIELD IF NOT EXISTS response_status_code ON api_idempotency_records TYPE int;
DEFINE FIELD IF NOT EXISTS response_body ON api_idempotency_records TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS created_at ON api_idempotency_records TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_api_idempotency_records_uuid ON api_idempotency_records FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_api_idempotency_records_scope ON api_idempotency_records
    FIELDS organization_id, principal_id, method, path, idempotency_key UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_api_idempotency_records_created_at ON api_idempotency_records FIELDS created_at;

DEFINE TABLE IF NOT EXISTS source_imports SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON source_imports TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON source_imports TYPE string;
DEFINE FIELD IF NOT EXISTS principal_id ON source_imports TYPE string;
DEFINE FIELD IF NOT EXISTS adapter_name ON source_imports TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS adapter_version ON source_imports TYPE option<string>;
DEFINE FIELD IF NOT EXISTS source_uri ON source_imports TYPE option<string>;
DEFINE FIELD IF NOT EXISTS source_identity ON source_imports TYPE option<string>;
DEFINE FIELD IF NOT EXISTS source_version ON source_imports TYPE option<string>;
DEFINE FIELD IF NOT EXISTS privacy_class ON source_imports TYPE option<string>;
DEFINE FIELD IF NOT EXISTS target_memory_scope ON source_imports TYPE option<string>;
DEFINE FIELD IF NOT EXISTS target_scope_key ON source_imports TYPE option<string>;
DEFINE FIELD IF NOT EXISTS status ON source_imports TYPE string DEFAULT 'pending';
DEFINE FIELD IF NOT EXISTS checkpoint ON source_imports TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS options ON source_imports TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS policy_context ON source_imports TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS counters ON source_imports TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS raw_memory_ids ON source_imports TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS source_ids ON source_imports TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS dedupe_keys ON source_imports TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS duplicate_dedupe_keys ON source_imports TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS skipped_records ON source_imports TYPE array<object> DEFAULT [];
DEFINE FIELD IF NOT EXISTS errors ON source_imports TYPE array<object> DEFAULT [];
DEFINE FIELD IF NOT EXISTS raw_memory_by_source_id ON source_imports TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS batch_size ON source_imports TYPE int DEFAULT 100;
DEFINE FIELD IF NOT EXISTS promotion_preview_approved ON source_imports TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS created_at ON source_imports TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON source_imports TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS completed_at ON source_imports TYPE option<datetime>;

DEFINE INDEX IF NOT EXISTS idx_source_imports_uuid ON source_imports FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_source_imports_org ON source_imports FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_source_imports_principal ON source_imports FIELDS organization_id, principal_id;
DEFINE INDEX IF NOT EXISTS idx_source_imports_status ON source_imports FIELDS organization_id, status;

DEFINE TABLE IF NOT EXISTS content_changefeed_cursors SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON content_changefeed_cursors TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON content_changefeed_cursors TYPE string;
DEFINE FIELD IF NOT EXISTS table_name ON content_changefeed_cursors TYPE string;
DEFINE FIELD IF NOT EXISTS consumer_name ON content_changefeed_cursors TYPE string;
DEFINE FIELD IF NOT EXISTS versionstamp ON content_changefeed_cursors TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS metadata ON content_changefeed_cursors TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS created_at ON content_changefeed_cursors TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON content_changefeed_cursors TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_content_changefeed_cursors_uuid
    ON content_changefeed_cursors FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_content_changefeed_cursors_org_consumer
    ON content_changefeed_cursors FIELDS organization_id, table_name, consumer_name UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_content_changefeed_cursors_updated_at
    ON content_changefeed_cursors FIELDS updated_at;

DEFINE TABLE IF NOT EXISTS derived_from SCHEMAFULL TYPE RELATION IN raw_captures OUT source_imports ENFORCED;
DEFINE FIELD IF NOT EXISTS uuid ON derived_from TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON derived_from TYPE string;
DEFINE FIELD IF NOT EXISTS raw_memory_id ON derived_from TYPE string;
DEFINE FIELD IF NOT EXISTS source_import_id ON derived_from TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON derived_from TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON derived_from TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_derived_from_uuid ON derived_from FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_derived_from_org_raw ON derived_from FIELDS organization_id, raw_memory_id;
DEFINE INDEX IF NOT EXISTS idx_derived_from_org_import ON derived_from FIELDS organization_id, source_import_id;

DEFINE TABLE IF NOT EXISTS chunk_of SCHEMAFULL TYPE RELATION IN document_chunks OUT crawled_documents ENFORCED;
DEFINE FIELD IF NOT EXISTS uuid ON chunk_of TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON chunk_of TYPE string;
DEFINE FIELD IF NOT EXISTS chunk_id ON chunk_of TYPE string;
DEFINE FIELD IF NOT EXISTS document_id ON chunk_of TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON chunk_of TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON chunk_of TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_chunk_of_uuid ON chunk_of FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_chunk_of_org_chunk ON chunk_of FIELDS organization_id, chunk_id;
DEFINE INDEX IF NOT EXISTS idx_chunk_of_org_document ON chunk_of FIELDS organization_id, document_id;

DEFINE TABLE IF NOT EXISTS supersedes SCHEMAFULL TYPE RELATION IN raw_captures OUT raw_captures ENFORCED;
DEFINE FIELD IF NOT EXISTS uuid ON supersedes TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON supersedes TYPE string;
DEFINE FIELD IF NOT EXISTS raw_memory_id ON supersedes TYPE string;
DEFINE FIELD IF NOT EXISTS superseded_raw_memory_id ON supersedes TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON supersedes TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON supersedes TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_supersedes_uuid ON supersedes FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_supersedes_org_raw ON supersedes FIELDS organization_id, raw_memory_id;
DEFINE INDEX IF NOT EXISTS idx_supersedes_org_superseded ON supersedes FIELDS organization_id, superseded_raw_memory_id;

DEFINE TABLE IF NOT EXISTS system_settings SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS key ON system_settings TYPE string;
DEFINE FIELD IF NOT EXISTS value ON system_settings TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS is_secret ON system_settings TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS description ON system_settings TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON system_settings TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON system_settings TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_system_settings_key ON system_settings FIELDS key UNIQUE;

DEFINE TABLE IF NOT EXISTS telemetry_rollups SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON telemetry_rollups TYPE string;
DEFINE FIELD IF NOT EXISTS bucket_key ON telemetry_rollups TYPE string;
DEFINE FIELD IF NOT EXISTS bucket_start ON telemetry_rollups TYPE datetime;
DEFINE FIELD IF NOT EXISTS window_seconds ON telemetry_rollups TYPE int DEFAULT 900;
DEFINE FIELD IF NOT EXISTS uptime_seconds ON telemetry_rollups TYPE number DEFAULT 0;
DEFINE FIELD IF NOT EXISTS summaries ON telemetry_rollups TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS trends ON telemetry_rollups TYPE array<object> DEFAULT [];
DEFINE FIELD IF NOT EXISTS metrics ON telemetry_rollups TYPE array<object> DEFAULT [];
DEFINE FIELD IF NOT EXISTS recent_events ON telemetry_rollups TYPE array<object> DEFAULT [];
DEFINE FIELD IF NOT EXISTS created_at ON telemetry_rollups TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON telemetry_rollups TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_telemetry_rollups_uuid ON telemetry_rollups FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_telemetry_rollups_bucket_key ON telemetry_rollups FIELDS bucket_key UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_telemetry_rollups_bucket_start ON telemetry_rollups FIELDS bucket_start;

DEFINE TABLE IF NOT EXISTS backup_settings SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON backup_settings TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON backup_settings TYPE string;
DEFINE FIELD IF NOT EXISTS enabled ON backup_settings TYPE bool DEFAULT true;
DEFINE FIELD IF NOT EXISTS schedule ON backup_settings TYPE string DEFAULT '0 2 * * *';
DEFINE FIELD IF NOT EXISTS retention_days ON backup_settings TYPE int DEFAULT 30;
DEFINE FIELD IF NOT EXISTS include_database_dump ON backup_settings TYPE option<bool>;
DEFINE FIELD IF NOT EXISTS include_graph ON backup_settings TYPE bool DEFAULT true;
DEFINE FIELD IF NOT EXISTS last_backup_at ON backup_settings TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS last_backup_id ON backup_settings TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON backup_settings TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON backup_settings TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_backup_settings_uuid ON backup_settings FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_backup_settings_org ON backup_settings FIELDS organization_id UNIQUE;

DEFINE TABLE IF NOT EXISTS backups SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON backups TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON backups TYPE string;
DEFINE FIELD IF NOT EXISTS backup_id ON backups TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS status ON backups TYPE string DEFAULT 'pending';
DEFINE FIELD IF NOT EXISTS job_id ON backups TYPE option<string>;
DEFINE FIELD IF NOT EXISTS filename ON backups TYPE option<string>;
DEFINE FIELD IF NOT EXISTS file_path ON backups TYPE option<string>;
DEFINE FIELD IF NOT EXISTS size_bytes ON backups TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS include_database_dump ON backups TYPE option<bool>;
DEFINE FIELD IF NOT EXISTS include_graph ON backups TYPE bool DEFAULT true;
DEFINE FIELD IF NOT EXISTS entity_count ON backups TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS relationship_count ON backups TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS started_at ON backups TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS completed_at ON backups TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS duration_seconds ON backups TYPE number DEFAULT 0;
DEFINE FIELD IF NOT EXISTS error ON backups TYPE option<string>;
DEFINE FIELD IF NOT EXISTS triggered_by ON backups TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_by_user_id ON backups TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON backups TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON backups TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_backups_uuid ON backups FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_backups_org ON backups FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_backups_backup_id ON backups FIELDS backup_id UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_backups_status ON backups FIELDS status;
