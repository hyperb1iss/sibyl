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

{{/*
Fail fast when a production-labelled release configures no JWT secret source.
Without SIBYL_JWT_SECRET the API starts with an empty signing key, the server
disables MCP auth under the default mcp_auth_mode=auto, and session cookies are
signed with an empty key. The chart must not auto-generate that secret in
production: a per-render value would rotate every upgrade and diverge between
the backend, worker, and bootstrap pods.
*/}}
{{- define "sibyl.validateProductionAuthSecret" -}}
{{- $env := default dict .Values.backend.env -}}
{{- $environment := lower (trim (default "" (get $env "SIBYL_ENVIRONMENT"))) -}}
{{- $inlineJwt := trim (default "" (get $env "SIBYL_JWT_SECRET")) -}}
{{- $existing := trim (default "" .Values.backend.existingSecret) -}}
{{- if and (eq $environment "production") (empty $existing) (empty $inlineJwt) -}}
{{- fail "backend.existingSecret is required when backend.env.SIBYL_ENVIRONMENT is \"production\".\nWithout it SIBYL_JWT_SECRET is never set, so the API signs sessions with an empty key and MCP auth disables itself (mcp_auth_mode defaults to \"auto\", which enforces Bearer auth only when a JWT secret is present).\nCreate the secret, then point the chart at it:\n  kubectl create secret generic sibyl-secrets \\\n    --from-literal=SIBYL_JWT_SECRET=\"$(openssl rand -hex 32)\" \\\n    --from-literal=SIBYL_SETTINGS_KEY=\"$(openssl rand -hex 32)\"\n  helm install sibyl charts/sibyl --set backend.existingSecret=sibyl-secrets\nFor a non-production trial set backend.env.SIBYL_ENVIRONMENT=development instead, which lets the server generate a development JWT secret on its own." -}}
{{- end -}}
{{- end }}
