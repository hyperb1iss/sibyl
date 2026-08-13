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
Fail fast when a production-labelled release configures no JWT secret source,
puts the signing key in a plaintext ConfigMap, or switches MCP auth off.
Without SIBYL_JWT_SECRET the API starts with an empty signing key, the server
disables MCP auth under the default mcp_auth_mode=auto, and session cookies are
signed with an empty key. The chart must not auto-generate that secret in
production: a per-render value would rotate every upgrade and diverge between
the backend, worker, and bootstrap pods.

Keys are matched case-insensitively, and both SIBYL_JWT_SECRET and the
unprefixed JWT_SECRET alias count, because pydantic-settings resolves
environment variables case-insensitively and config.py falls back to the
unprefixed name. Either spelling in backend.env reaches the process while
sitting in the plaintext ConfigMap.
*/}}
{{- define "sibyl.validateProductionAuthSecret" -}}
{{- $env := default dict .Values.backend.env -}}
{{- $isProduction := false -}}
{{- $inlineJwtKeys := list -}}
{{- $authOff := false -}}
{{- range $key, $value := $env -}}
{{- $upperKey := upper $key -}}
{{- $normalized := trim (toString $value) -}}
{{- if and (eq $upperKey "SIBYL_ENVIRONMENT") (eq (lower $normalized) "production") -}}
{{- $isProduction = true -}}
{{- end -}}
{{- if and (has $upperKey (list "SIBYL_JWT_SECRET" "JWT_SECRET")) (not (empty $normalized)) -}}
{{- $inlineJwtKeys = append $inlineJwtKeys $key -}}
{{- end -}}
{{- if and (eq $upperKey "SIBYL_MCP_AUTH_MODE") (eq (lower $normalized) "off") -}}
{{- $authOff = true -}}
{{- end -}}
{{- end -}}
{{- $existing := trim (default "" .Values.backend.existingSecret) -}}
{{/*
Only interpolate operator-supplied names into the remediation commands when they
are RFC 1123 DNS subdomains. An unvalidated value lands inside a shell snippet a
reader is invited to paste, so `$(id)` would execute on their machine.
*/}}
{{- $dnsPattern := "^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$" -}}
{{- $secretName := "<your-secret-name>" -}}
{{- if empty $existing -}}
{{- $secretName = "sibyl-secrets" -}}
{{- else if and (le (len $existing) 253) (regexMatch $dnsPattern $existing) -}}
{{- $secretName = $existing -}}
{{- end -}}
{{- $namespace := "<your-namespace>" -}}
{{- if and (le (len .Release.Namespace) 253) (regexMatch $dnsPattern .Release.Namespace) -}}
{{- $namespace = .Release.Namespace -}}
{{- end -}}
{{- $release := "<your-release>" -}}
{{- if and (le (len .Release.Name) 253) (regexMatch $dnsPattern .Release.Name) -}}
{{- $release = .Release.Name -}}
{{- end -}}
{{/*
Split by install shape. `helm upgrade --set` without --reuse-values resets an
existing release to chart defaults, silently discarding every other override.
*/}}
{{- $remediation := printf "Create the Secret in the release namespace:\n  kubectl create secret generic %s --namespace %s \\\n    --from-literal=SIBYL_JWT_SECRET=\"$(openssl rand -hex 32)\" \\\n    --from-literal=SIBYL_SETTINGS_KEY=\"$(openssl rand -hex 32)\"\nThen, for a first install:\n  helm install %s charts/sibyl --namespace %s --set backend.existingSecret=%s\nFor a release that already exists, keep its current values or you will reset it to chart defaults:\n  helm upgrade %s charts/sibyl --namespace %s --reuse-values --set backend.existingSecret=%s\n  (or re-apply your values files with -f and add the same --set)" $secretName $namespace $release $namespace $secretName $release $namespace $secretName -}}
{{- if and $isProduction (empty $existing) -}}
{{- fail (printf "backend.existingSecret is required when backend.env.SIBYL_ENVIRONMENT is \"production\".\nWithout it SIBYL_JWT_SECRET is never set, so the API signs sessions with an empty key and MCP auth disables itself (mcp_auth_mode defaults to \"auto\", which enforces Bearer auth only when a JWT secret is present).\n%s\nSetting backend.env.SIBYL_JWT_SECRET or backend.env.JWT_SECRET does not satisfy this: every backend.env key is rendered into the plaintext %s-config ConfigMap, which is readable under broader RBAC than a Secret.\nFor a non-production trial set backend.env.SIBYL_ENVIRONMENT=development instead. The server then derives its own JWT secret per process, so sessions break on restart and across replicas, which is why that path is unfit for production." $remediation (include "sibyl.fullname" .)) -}}
{{- end -}}
{{- if and $isProduction (not (empty $inlineJwtKeys)) -}}
{{- fail (printf "backend.env.%s must not be used in production. Every backend.env key is rendered into the plaintext %s-config ConfigMap, so the signing key would be stored unencrypted and readable under broader RBAC than a Secret. The unprefixed JWT_SECRET alias is rejected for the same reason, because config.py falls back to it. Put SIBYL_JWT_SECRET in the Secret referenced by backend.existingSecret instead.\n%s" (join ", backend.env." $inlineJwtKeys) (include "sibyl.fullname" .) $remediation) -}}
{{- end -}}
{{- if and $isProduction $authOff -}}
{{- fail "backend.env.SIBYL_MCP_AUTH_MODE=off is forbidden in production. It serves every MCP tool unauthenticated regardless of the JWT secret. Use \"auto\" (enforce once a secret is set) or \"on\" (always enforce), or set backend.env.SIBYL_ENVIRONMENT=development for a local unauthenticated endpoint." -}}
{{- end -}}
{{- end }}
