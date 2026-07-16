{{/*
Expand the name of the chart.
*/}}
{{- define "sibyl.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "sibyl.fullname" -}}
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
Create chart name and version as used by the chart label.
*/}}
{{- define "sibyl.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "sibyl.labels" -}}
helm.sh/chart: {{ include "sibyl.chart" . }}
{{ include "sibyl.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "sibyl.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sibyl.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Backend labels
*/}}
{{- define "sibyl.backend.labels" -}}
{{ include "sibyl.labels" . }}
app.kubernetes.io/component: backend
{{- end }}

{{/*
Backend selector labels
*/}}
{{- define "sibyl.backend.selectorLabels" -}}
{{ include "sibyl.selectorLabels" . }}
app.kubernetes.io/component: backend
{{- end }}

{{/*
Frontend labels
*/}}
{{- define "sibyl.frontend.labels" -}}
{{ include "sibyl.labels" . }}
app.kubernetes.io/component: frontend
{{- end }}

{{/*
Frontend selector labels
*/}}
{{- define "sibyl.frontend.selectorLabels" -}}
{{ include "sibyl.selectorLabels" . }}
app.kubernetes.io/component: frontend
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "sibyl.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "sibyl.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Backend image
*/}}
{{- define "sibyl.backend.image" -}}
{{- $tag := .Values.backend.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" .Values.backend.image.repository $tag -}}
{{- end }}

{{/*
Frontend image
*/}}
{{- define "sibyl.frontend.image" -}}
{{- $tag := .Values.frontend.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" .Values.frontend.image.repository $tag -}}
{{- end }}

{{/*
Worker labels
*/}}
{{- define "sibyl.worker.labels" -}}
{{ include "sibyl.labels" . }}
app.kubernetes.io/component: worker
{{- end }}

{{/*
Worker selector labels
*/}}
{{- define "sibyl.worker.selectorLabels" -}}
{{ include "sibyl.selectorLabels" . }}
app.kubernetes.io/component: worker
{{- end }}

{{/*
Fail fast when a non-corporate extra provider is configured without the explicit opt-in.
*/}}
{{- define "sibyl.validateOidcProviders" -}}
{{- range $provider := .Values.oidc.providers }}
{{- if not (trim (default "" $provider.organization_slug)) -}}
{{- fail "every OIDC provider requires organization_slug for an exact non-personal organization binding" -}}
{{- end -}}
{{- end -}}
{{- if not .Values.oidc.extra_providers_enabled -}}
{{- range $provider := .Values.oidc.providers }}
{{- $name := lower (default "" $provider.name) -}}
{{- $issuer := lower (default "" $provider.issuer) -}}
{{- if or (contains "github" $name) (contains "github.com" $issuer) (contains "google" $name) (contains "accounts.google.com" $issuer) -}}
{{- fail "oidc.extra_providers_enabled must be true before enabling GitHub or Google OIDC providers" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end }}
