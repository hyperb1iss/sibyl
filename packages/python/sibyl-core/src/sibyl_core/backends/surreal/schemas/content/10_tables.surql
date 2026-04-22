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
DEFINE INDEX IF NOT EXISTS idx_crawl_sources_url ON crawl_sources FIELDS url UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_crawl_sources_status ON crawl_sources FIELDS crawl_status;
DEFINE INDEX IF NOT EXISTS idx_crawl_sources_name_ft ON crawl_sources FIELDS name FULLTEXT ANALYZER title_analyzer BM25;

DEFINE TABLE IF NOT EXISTS crawled_documents SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON crawled_documents TYPE string;
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
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_source ON crawled_documents FIELDS source_id;
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_url ON crawled_documents FIELDS url UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_crawled_at ON crawled_documents FIELDS crawled_at;
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_title_ft ON crawled_documents FIELDS title FULLTEXT ANALYZER title_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_content_ft ON crawled_documents FIELDS content FULLTEXT ANALYZER content_analyzer BM25;

DEFINE TABLE IF NOT EXISTS document_chunks SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON document_chunks TYPE string;
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
DEFINE INDEX IF NOT EXISTS idx_document_chunks_document ON document_chunks FIELDS document_id;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_document_position ON document_chunks
    FIELDS document_id, chunk_index UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_content_ft ON document_chunks FIELDS content FULLTEXT ANALYZER content_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_embedding ON document_chunks FIELDS embedding
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32 EFC 150 M 12;

DEFINE TABLE IF NOT EXISTS raw_captures SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON raw_captures TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON raw_captures TYPE string;
DEFINE FIELD IF NOT EXISTS entity_id ON raw_captures TYPE option<string>;
DEFINE FIELD IF NOT EXISTS title ON raw_captures TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS raw_content ON raw_captures TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS entity_type ON raw_captures TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS tags ON raw_captures TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS metadata ON raw_captures TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS capture_surface ON raw_captures TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_by_user_id ON raw_captures TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON raw_captures TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_raw_captures_uuid ON raw_captures FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_org ON raw_captures FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_entity_id ON raw_captures FIELDS entity_id;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_created_at ON raw_captures FIELDS created_at;

DEFINE TABLE IF NOT EXISTS system_settings SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS key ON system_settings TYPE string;
DEFINE FIELD IF NOT EXISTS value ON system_settings TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS is_secret ON system_settings TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS description ON system_settings TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON system_settings TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON system_settings TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_system_settings_key ON system_settings FIELDS key UNIQUE;

DEFINE TABLE IF NOT EXISTS backup_settings SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON backup_settings TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON backup_settings TYPE string;
DEFINE FIELD IF NOT EXISTS enabled ON backup_settings TYPE bool DEFAULT true;
DEFINE FIELD IF NOT EXISTS schedule ON backup_settings TYPE string DEFAULT '0 2 * * *';
DEFINE FIELD IF NOT EXISTS retention_days ON backup_settings TYPE int DEFAULT 30;
DEFINE FIELD IF NOT EXISTS include_postgres ON backup_settings TYPE bool DEFAULT true;
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
DEFINE FIELD IF NOT EXISTS include_postgres ON backups TYPE bool DEFAULT true;
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
