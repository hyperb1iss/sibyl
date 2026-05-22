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
  value: {{ include "sibyl-surrealdb.endpoint" . | quote }}
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
