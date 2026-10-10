{{/*
Expand the chart name.
*/}}
{{- define "sibyl-surrealdb.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "sibyl-surrealdb.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Mirror the upstream surrealdb.fullname helper for dependency resources.
*/}}
{{- define "sibyl-surrealdb.upstreamFullname" -}}
{{- if .Values.surrealdb.fullnameOverride }}
{{- .Values.surrealdb.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default "surrealdb" .Values.surrealdb.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "sibyl-surrealdb.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "sibyl-surrealdb.labels" -}}
helm.sh/chart: {{ include "sibyl-surrealdb.chart" . }}
{{ include "sibyl-surrealdb.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "sibyl-surrealdb.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sibyl-surrealdb.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "sibyl-surrealdb.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "sibyl-surrealdb.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "sibyl-surrealdb.endpoint" -}}
{{- if .Values.connection.endpoint }}
{{- .Values.connection.endpoint }}
{{- else }}
{{- printf "%s://%s:%v" .Values.connection.scheme (include "sibyl-surrealdb.upstreamFullname" .) (.Values.surrealdb.service.port | default 8000) }}
{{- end }}
{{- end }}

{{- define "sibyl-surrealdb.credentialsSecretName" -}}
{{- default (printf "%s-root" (include "sibyl-surrealdb.upstreamFullname" .)) .Values.connection.existingSecret }}
{{- end }}

{{- define "sibyl-surrealdb.sourcePvcName" -}}
{{- default (include "sibyl-surrealdb.upstreamFullname" .) .Values.snapshot.persistentVolumeClaimName }}
{{- end }}

{{- define "sibyl-surrealdb.validateIdentifier" -}}
{{- $value := .value | toString -}}
{{- $field := .field | toString -}}
{{- if not (regexMatch "^[A-Za-z_][A-Za-z0-9_]*$" $value) -}}
{{- fail (printf "%s must be a SurrealDB identifier matching ^[A-Za-z_][A-Za-z0-9_]*$: %q" $field $value) -}}
{{- end -}}
{{- end }}

{{- define "sibyl-surrealdb.validateDatabases" -}}
{{- range $index, $item := .Values.databases }}
{{- include "sibyl-surrealdb.validateIdentifier" (dict "field" (printf "databases[%d].namespace" $index) "value" $item.namespace) }}
{{- include "sibyl-surrealdb.validateIdentifier" (dict "field" (printf "databases[%d].database" $index) "value" $item.database) }}
{{- end }}
{{- range $index, $item := .Values.restoreDrill.fixtureChecks }}
{{- include "sibyl-surrealdb.validateIdentifier" (dict "field" (printf "restoreDrill.fixtureChecks[%d].namespace" $index) "value" $item.namespace) }}
{{- include "sibyl-surrealdb.validateIdentifier" (dict "field" (printf "restoreDrill.fixtureChecks[%d].database" $index) "value" $item.database) }}
{{- include "sibyl-surrealdb.validateIdentifier" (dict "field" (printf "restoreDrill.fixtureChecks[%d].table" $index) "value" $item.table) }}
{{- end }}
{{- end }}

{{- define "sibyl-surrealdb.surrealEnv" -}}
- name: SURREAL_ENDPOINT
  value: {{ include "sibyl-surrealdb.httpEndpoint" . | quote }}
- name: SURREAL_AUTH_LEVEL
  value: {{ .Values.connection.authLevel | quote }}
- name: SURREAL_USER
  value: {{ .Values.connection.username | quote }}
- name: SURREAL_PASS
  valueFrom:
    secretKeyRef:
      name: {{ include "sibyl-surrealdb.credentialsSecretName" . }}
      key: {{ .Values.connection.passwordKey | quote }}
{{- end }}

{{- define "sibyl-surrealdb.surrealImage" -}}
{{- printf "%s:%s" (.Values.surrealdb.image.repository | default "surrealdb/surrealdb") (.Values.surrealdb.image.tag | default .Chart.AppVersion) }}
{{- end }}

{{/*
Utility image for the operational jobs. The surreal image cannot host
them: it is distroless, so /bin/sh, curl, and jq do not exist there.
*/}}
{{- define "sibyl-surrealdb.opsImage" -}}
{{- printf "%s:%s" .Values.opsImage.repository .Values.opsImage.tag }}
{{- end }}

{{/*
HTTP form of the connection endpoint for the ops jobs. The old CLI
accepted ws/wss endpoints, but /sql and /export are HTTP, so ws maps
to http and wss to https (SurrealDB serves both protocols on one
port) and a trailing /rpc is dropped. Anything else non-HTTP fails
the render instead of failing at runtime inside a hook Job.
*/}}
{{- define "sibyl-surrealdb.httpEndpoint" -}}
{{- $endpoint := include "sibyl-surrealdb.endpoint" . | trim -}}
{{- $lowered := lower $endpoint -}}
{{- if hasPrefix "ws://" $lowered -}}
{{- $endpoint = printf "http://%s" (substr 5 (len $endpoint) $endpoint) -}}
{{- else if hasPrefix "wss://" $lowered -}}
{{- $endpoint = printf "https://%s" (substr 6 (len $endpoint) $endpoint) -}}
{{- else if hasPrefix "http://" $lowered -}}
{{- $endpoint = printf "http://%s" (substr 7 (len $endpoint) $endpoint) -}}
{{- else if hasPrefix "https://" $lowered -}}
{{- $endpoint = printf "https://%s" (substr 8 (len $endpoint) $endpoint) -}}
{{- else -}}
{{- fail (printf "connection endpoint %q is not usable by the ops jobs: they speak the HTTP API, so the endpoint must be http(s) (ws/wss are normalized automatically)" $endpoint) -}}
{{- end -}}
{{- /* Canonicalize the path: the jobs append /sql and /export, so a
trailing slash or an /rpc segment (with or without its own trailing
slash) would build /rpc//sql-style URLs. */ -}}
{{- $endpoint = regexReplaceAll "/+$" $endpoint "" -}}
{{- $endpoint = trimSuffix "/rpc" $endpoint -}}
{{- $endpoint = regexReplaceAll "/+$" $endpoint "" -}}
{{- $endpoint -}}
{{- end }}

{{/*
A shell hook carried in an env value or in args. The kubelet expands
$(NAME) references and collapses $$ to $ in both, so every $ is doubled
here and the hook reaches the shell exactly as written in values.
*/}}
{{- define "sibyl-surrealdb.hookEnvValue" -}}
{{- . | toString | replace "$" "$$" | quote -}}
{{- end }}

{{/*
Every request the ops jobs make is bounded, so a hung server cannot hold
a Job, and under concurrencyPolicy Forbid every later one, forever.
*/}}
{{- define "sibyl-surrealdb.httpEnv" -}}
- name: SIBYL_HTTP_CONNECT_TIMEOUT
  value: {{ .Values.jobDefaults.http.connectTimeoutSeconds | toString | quote }}
- name: SIBYL_HTTP_MAX_TIME
  value: {{ .Values.jobDefaults.http.maxTimeSeconds | toString | quote }}
{{- end }}

{{- define "sibyl-surrealdb.validateWholeNumber" -}}
{{- if not (regexMatch "^[0-9]+$" (toString .value)) -}}
{{- fail (printf "%s must be a whole number, got %q" .field (toString .value)) -}}
{{- end -}}
{{- end }}

{{- define "sibyl-surrealdb.validateOps" -}}
{{- if not (regexMatch "^[A-Za-z0-9][A-Za-z0-9_-]*$" (toString .Values.export.filePrefix)) -}}
{{- fail (printf "export.filePrefix must match ^[A-Za-z0-9][A-Za-z0-9_-]*$ so run directories can be matched exactly, got %q" (toString .Values.export.filePrefix)) -}}
{{- end -}}
{{- include "sibyl-surrealdb.validateWholeNumber" (dict "field" "jobDefaults.http.connectTimeoutSeconds" "value" .Values.jobDefaults.http.connectTimeoutSeconds) -}}
{{- include "sibyl-surrealdb.validateWholeNumber" (dict "field" "jobDefaults.http.maxTimeSeconds" "value" .Values.jobDefaults.http.maxTimeSeconds) -}}
{{- include "sibyl-surrealdb.validateWholeNumber" (dict "field" "export.backoffLimit" "value" .Values.export.backoffLimit) -}}
{{- include "sibyl-surrealdb.validateWholeNumber" (dict "field" "export.activeDeadlineSeconds" "value" .Values.export.activeDeadlineSeconds) -}}
{{- include "sibyl-surrealdb.validateWholeNumber" (dict "field" "restoreDrill.backoffLimit" "value" .Values.restoreDrill.backoffLimit) -}}
{{- include "sibyl-surrealdb.validateWholeNumber" (dict "field" "restoreDrill.activeDeadlineSeconds" "value" .Values.restoreDrill.activeDeadlineSeconds) -}}
{{- include "sibyl-surrealdb.validateWholeNumber" (dict "field" "restoreDrill.rowDrift.rows" "value" .Values.restoreDrill.rowDrift.rows) -}}
{{- if not (regexMatch "^[0-9]+(\\.[0-9]+)?$" (toString .Values.restoreDrill.rowDrift.percent)) -}}
{{- fail (printf "restoreDrill.rowDrift.percent must be a number from 0 to 100, got %q" (toString .Values.restoreDrill.rowDrift.percent)) -}}
{{- end -}}
{{- if gt (float64 .Values.restoreDrill.rowDrift.percent) 100.0 -}}
{{- fail (printf "restoreDrill.rowDrift.percent must be a number from 0 to 100, got %v" .Values.restoreDrill.rowDrift.percent) -}}
{{- end -}}
{{- end }}
