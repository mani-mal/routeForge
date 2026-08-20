{{/*
Chart name, DNS-1123 safe.
*/}}
{{- define "routeforge.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified release name (unused for the fixed service names below, but kept
for labels / any release-scoped resource).
*/}}
{{- define "routeforge.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "routeforge.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels applied to every object. Includes the HPE ezua labels so the
platform recognises this as a vendor-imported service (the Kyverno policy also
enforces hpe-ezua/type on Pods; we set it here too, belt-and-suspenders).
*/}}
{{- define "routeforge.labels" -}}
helm.sh/chart: {{ include "routeforge.chart" . }}
app.kubernetes.io/name: {{ include "routeforge.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
hpe-ezua/app: {{ .Release.Name }}
hpe-ezua/type: vendor-service
{{- end -}}

{{/*
Name of the Secret holding all demo/app credentials — either a user-supplied
existing Secret or the chart-created one. Never a values-embedded literal on the
production path (see values.yaml credentials block).
*/}}
{{- define "routeforge.secretName" -}}
{{- if .Values.credentials.existingSecret -}}
{{- .Values.credentials.existingSecret -}}
{{- else -}}
{{- printf "routeforge-creds" -}}
{{- end -}}
{{- end -}}

{{/*
Name of the image pull Secret. Either the chart-created one (from
imageCredentials) or the first entry the user references in imagePullSecrets.
Returns empty string when neither is set (public registry / air-gap mirror).
*/}}
{{- define "routeforge.imagePullSecretName" -}}
{{- if .Values.imageCredentials.enabled -}}
{{- printf "routeforge-registry" -}}
{{- end -}}
{{- end -}}

{{/*
imagePullSecrets block for a Pod spec. Merges the chart-created registry secret
(if imageCredentials.enabled) with any user-referenced secrets.
*/}}
{{- define "routeforge.imagePullSecrets" -}}
{{- $secrets := list -}}
{{- if .Values.imageCredentials.enabled -}}
{{- $secrets = append $secrets (dict "name" "routeforge-registry") -}}
{{- end -}}
{{- range .Values.imagePullSecrets -}}
{{- $secrets = append $secrets . -}}
{{- end -}}
{{- if $secrets -}}
imagePullSecrets:
{{ toYaml $secrets }}
{{- end -}}
{{- end -}}

{{/*
Fully-qualified routeForge api/ui image references. When image.registry is set
it is prefixed (Harbor path); otherwise the repository is used bare.
*/}}
{{- define "routeforge.apiImage" -}}
{{- $reg := .Values.image.registry | default "" -}}
{{- $repo := .Values.image.api.repository -}}
{{- $tag := .Values.image.api.tag | default .Chart.AppVersion -}}
{{- if $reg -}}
{{- printf "%s/%s:%s" (trimSuffix "/" $reg) $repo $tag -}}
{{- else -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}
{{- end -}}

{{- define "routeforge.uiImage" -}}
{{- $reg := .Values.image.registry | default "" -}}
{{- $repo := .Values.image.ui.repository -}}
{{- $tag := .Values.image.ui.tag | default .Chart.AppVersion -}}
{{- if $reg -}}
{{- printf "%s/%s:%s" (trimSuffix "/" $reg) $repo $tag -}}
{{- else -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}
{{- end -}}

{{/*
Third-party (Langfuse stack) image ref. Prepends thirdPartyRegistry when set so
air-gapped installs can point every dependency at a mirror; otherwise uses the
public ref verbatim. Call as (include "routeforge.depImage" (dict "root" . "image" "postgres:16")).
*/}}
{{- define "routeforge.depImage" -}}
{{- $reg := .root.Values.thirdPartyRegistry | default "" -}}
{{- if $reg -}}
{{- printf "%s/%s" (trimSuffix "/" $reg) .image -}}
{{- else -}}
{{- .image -}}
{{- end -}}
{{- end -}}

{{/*
Browser-reachable Langfuse base URL, derived from the Langfuse VirtualService
endpoint (PCAI substitutes ${DOMAIN_NAME}/${RELEASE_NAME}/${NAMESPACE} in the
values file at import time, so this resolves to the real https host). Used for
NEXTAUTH_URL and the UI's "open Langfuse trace" deep-link.
*/}}
{{- define "routeforge.langfusePublicUrl" -}}
{{- printf "https://%s" .Values.langfuse.virtualService.endpoint -}}
{{- end -}}
