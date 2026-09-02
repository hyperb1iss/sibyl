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
{{- $endpoint := include "sibyl-surrealdb.endpoint" . -}}
{{- if hasPrefix "ws://" $endpoint -}}
{{- $endpoint = printf "http://%s" (trimPrefix "ws://" $endpoint) -}}
{{- else if hasPrefix "wss://" $endpoint -}}
{{- $endpoint = printf "https://%s" (trimPrefix "wss://" $endpoint) -}}
{{- end -}}
{{- $endpoint = trimSuffix "/rpc" $endpoint -}}
{{- if not (or (hasPrefix "http://" $endpoint) (hasPrefix "https://" $endpoint)) -}}
{{- fail (printf "connection endpoint %q is not usable by the ops jobs: they speak the HTTP API, so the endpoint must be http(s) (ws/wss are normalized automatically)" $endpoint) -}}
{{- end -}}
{{- $endpoint -}}
{{- end }}
